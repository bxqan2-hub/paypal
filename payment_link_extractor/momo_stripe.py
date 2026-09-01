from __future__ import annotations

"""Stripe MoMo confirmation chain; no PayPal or GoPay protocol imports."""

import os
import secrets
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .config import DEFAULT_STRIPE_PK, STRIPE_VERSION_BASE, STRIPE_VERSION_FULL
from .errors import ProtocolError
from .momo_checkout import json_payload
from .momo_transport import momo_request_headers


MOMO_STRIPE_RUNTIME_VERSION = (
    os.getenv("OPLL_MOMO_STRIPE_RUNTIME_VERSION", "").strip()
    or "939d686cd5"
)


def _payable_amount_minor(checkout: dict[str, Any]) -> int:
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    due = total.get("total") if isinstance(total.get("total"), dict) else {}
    raw = due.get("minorUnitsAmount")
    if raw in (None, ""):
        raw = checkout.get("payable_amount_minor")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        try:
            parsed = float(str(raw).strip())
        except (TypeError, ValueError):
            return 0
        value = int(parsed) if parsed.is_integer() else 0
    return value if value >= 0 else 0


def _stripe_fingerprint_id() -> str:
    """Match Stripe.js's UUID-plus-hex fingerprint shape observed in HAR."""
    return f"{uuid.uuid4()}{secrets.token_hex(3)}"


def _attribution_fields(checkout: dict[str, Any], *, source: str) -> dict[str, str]:
    session_id = str(
        checkout.get("stripe_client_session_id")
        or checkout.get("stripe_js_id")
        or uuid.uuid4()
    )
    checkout["stripe_client_session_id"] = session_id
    fields = {
        "client_session_id": session_id,
        "merchant_integration_source": source,
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "2021",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
    }
    return fields


def _stripe_error_code(response: Any) -> str:
    try:
        payload = response.json() or {}
    except Exception:
        return ""
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or error.get("type") or "").strip()
        param = str(error.get("param") or "").strip()
        if code and param:
            return f"{code};param={param}"
    return code


def _find_client_secret(value: Any) -> str:
    if isinstance(value, dict):
        direct = str(value.get("client_secret") or "").strip()
        if direct:
            return direct
        for child in value.values():
            found = _find_client_secret(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_client_secret(child)
            if found:
                return found
    return ""
    return ""


def _find_customer_id(value: Any) -> str:
    """Extract Stripe's customer id from Elements' nested customer object."""
    if isinstance(value, str):
        candidate = value.strip()
        return candidate if candidate.startswith("cus_") else ""
    if isinstance(value, dict):
        # Elements returns customer.customer_session.customer; inspect this
        # path before generic id keys so unrelated config/session ids are not
        # sent as the Stripe customer.
        for key in ("customer", "customer_session", "customerSession"):
            found = _find_customer_id(value.get(key))
            if found:
                return found
        for key in ("id", "customer_id", "customerId"):
            found = _find_customer_id(value.get(key))
            if found:
                return found
        for child in value.values():
            found = _find_customer_id(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_customer_id(child)
            if found:
                return found
    return ""


def _session_cookie_value(session: Any, name: str) -> str:
    target = str(name or "").strip()
    headers = getattr(session, "headers", None)
    cookie_header = str(headers.get("Cookie") or "") if headers is not None else ""
    for part in cookie_header.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key.strip() == target:
            return value.strip()
    jar = getattr(session, "cookies", None)
    if jar is not None:
        try:
            value = jar.get(target)
        except Exception:
            value = ""
        if value:
            return str(value).strip()
    return ""


def _set_session_cookie(session: Any, name: str, value: str) -> None:
    if not session or not value:
        return
    headers = getattr(session, "headers", None)
    if headers is not None:
        current = str(headers.get("Cookie") or "")
        parts = [item.strip() for item in current.split(";") if item.strip()]
        replaced = False
        for index, item in enumerate(parts):
            if item.split("=", 1)[0].strip() == name:
                parts[index] = f"{name}={value}"
                replaced = True
                break
        if not replaced:
            parts.append(f"{name}={value}")
        headers["Cookie"] = "; ".join(parts)
    jar = getattr(session, "cookies", None)
    if jar is not None:
        try:
            jar.set(name, value, domain=".chatgpt.com", path="/")
        except Exception:
            try:
                jar.set(name, value)
            except Exception:
                pass


def synchronize_momo_stripe_ids(
    chatgpt_session: Any, stripe_session: Any, checkout: dict[str, Any]
) -> None:
    """Keep Stripe fingerprint cookies and form fields on one flow identity."""
    values: dict[str, str] = {}
    for name in ("__stripe_mid", "__stripe_sid"):
        value = _session_cookie_value(chatgpt_session, name) or _session_cookie_value(
            stripe_session, name
        )
        if value:
            values[name] = value
    # Keep a single in-memory fingerprint when a fresh browser has not yet
    # received Stripe cookies.  The complete VN HAR carries 42-character
    # ``__stripe_mid``/``__stripe_sid`` values, so preserve that shape.
    values.setdefault("__stripe_mid", _stripe_fingerprint_id())
    values.setdefault("__stripe_sid", _stripe_fingerprint_id())
    for name, value in values.items():
        _set_session_cookie(chatgpt_session, name, value)
    checkout["stripe_muid"] = values["__stripe_mid"]
    checkout["stripe_sid"] = values["__stripe_sid"]


def _post(session: Any, url: str, stage: str, data: dict[str, Any]) -> dict[str, Any]:
    response = session.request("POST", url, data=data, timeout=30, headers={"Content-Type": "application/x-www-form-urlencoded"})
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        status = int(response.status_code)
        code = _stripe_error_code(response)
        suffix = f", code={code}" if code else ""
        raise ProtocolError(status, f"{stage} failed (HTTP {status}{suffix})")
    return json_payload(response, stage)


def elements_session(session: Any, checkout: dict[str, Any]) -> dict[str, Any]:
    amount = _payable_amount_minor(checkout)
    key = str(checkout.get("publishable_key") or DEFAULT_STRIPE_PK)
    params: dict[str, Any] = {
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": "vnd",
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "link",
        "deferred_intent[payment_method_types][2]": "momo",
        "currency": "vnd",
        "key": key,
        "_stripe_version": STRIPE_VERSION_BASE,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": str(checkout.get("stripe_js_id") or uuid.uuid4()),
        "locale": "vi-VN",
        "browser_timezone": os.getenv("OPLL_MOMO_BROWSER_TIMEZONE", "").strip()
        or "Asia/Saigon",
        "type": "deferred_intent",
    }
    secret = str(checkout.get("customer_session_client_secret") or "").strip()
    if secret:
        params["customer_session_client_secret"] = secret
    response = session.request(
        "GET",
        "https://api.stripe.com/v1/elements/sessions",
        params=params,
        timeout=30,
    )
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        status = int(response.status_code)
        code = _stripe_error_code(response)
        suffix = f", code={code}" if code else ""
        raise ProtocolError(status, f"Momo Stripe Elements init failed (HTTP {status}{suffix})")
    payload = json_payload(response, "Momo Stripe Elements init")
    checkout["elements_session"] = payload
    customer_id = _find_customer_id(payload.get("customer"))
    if customer_id:
        checkout["customer"] = customer_id
    checkout["stripe_js_id"] = str(
        checkout.get("stripe_js_id") or params["stripe_js_id"]
    )
    checkout["stripe_client_session_id"] = checkout["stripe_js_id"]
    return payload


def confirmation_token(session: Any, checkout: dict[str, Any], billing: dict[str, str], captcha: str = "") -> str:
    key = str(checkout.get("publishable_key") or DEFAULT_STRIPE_PK)
    nested_attribution = _attribution_fields(checkout, source="elements")
    body: dict[str, Any] = {
        "payment_method_data[type]": "momo",
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][phone]": "",
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][country]": "VN",
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{MOMO_STRIPE_RUNTIME_VERSION}; "
            f"stripe-js-v3/{MOMO_STRIPE_RUNTIME_VERSION}; "
            "payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        # Stripe.js reports elapsed page time.  Use the live flow clock and
        # keep a short synthetic floor for very fast test/CLI runs.
        "payment_method_data[time_on_page]": str(
            max(
                2000,
                min(
                    60000,
                    round(
                        (
                            time.perf_counter()
                            - float(
                                checkout.get("flow_started_at")
                                or time.perf_counter()
                            )
                        )
                        * 1000
                    ),
                ),
            )
        ),
        "payment_method_data[guid]": _stripe_fingerprint_id(),
        "payment_method_data[muid]": str(
            checkout.get("stripe_muid") or _stripe_fingerprint_id()
        ),
        "payment_method_data[sid]": str(checkout.get("stripe_sid") or "NA"),
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "client_context[currency]": "vnd",
        "client_context[mode]": "subscription",
        "client_context[payment_method_types][0]": "card",
        "client_context[payment_method_types][1]": "link",
        "client_context[payment_method_types][2]": "momo",
        "set_as_default_payment_method": "false",
        "key": key,
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    body["payment_method_data[billing_details][phone]"] = ""
    captcha_value = str(captcha or os.getenv("OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN", "")).strip()
    if captcha_value:
        body["payment_method_data[radar_options][hcaptcha_token]"] = captcha_value
    ctx = checkout.get("elements_session") if isinstance(checkout.get("elements_session"), dict) else {}
    customer = _find_customer_id(checkout.get("customer"))
    if not customer:
        customer = _find_customer_id(ctx.get("customer"))
    if customer:
        body["client_context[customer]"] = customer
    elements_session_id = str(
        ctx.get("session_id") or ctx.get("id") or ctx.get("elements_session_id") or ""
    )
    elements_config_id = str(
        ctx.get("config_id") or ctx.get("elements_session_config_id") or ""
    )
    if elements_session_id:
        body["payment_method_data[client_attribution_metadata][elements_session_id]"] = elements_session_id
    if elements_config_id:
        body["payment_method_data[client_attribution_metadata][elements_session_config_id]"] = elements_config_id
    for name, value in nested_attribution.items():
        body[f"payment_method_data[client_attribution_metadata][{name}]"] = value
    for index, value in enumerate(("expressCheckout", "payment", "address")):
        body[f"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][{index}]"] = value
    for name, value in nested_attribution.items():
        body[f"client_attribution_metadata[{name}]"] = value
    for index, value in enumerate(("expressCheckout", "payment", "address")):
        body[f"client_attribution_metadata[merchant_integration_additional_elements][{index}]"] = value
    if elements_session_id:
        body["client_attribution_metadata[elements_session_id]"] = elements_session_id
    if elements_config_id:
        body["client_attribution_metadata[elements_session_config_id]"] = elements_config_id
    payload = _post(session, "https://api.stripe.com/v1/confirmation_tokens", "Momo Stripe confirmation token", body)
    token = str(payload.get("id") or "")
    if not token.startswith("ctoken_"):
        keys = ",".join(sorted(str(key) for key in payload.keys()))
        raise ProtocolError(502, f"Momo Stripe confirmation token missing ctoken_ id (response_keys={keys})")
    return token


def checkout_confirm(session: Any, checkout: dict[str, Any], token: str) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/confirm"
    url = "https://chatgpt.com" + path
    processor = str(checkout.get("processor_entity") or "openai_llc")
    referer = f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}"
    response = session.request(
        "POST",
        url,
        json={
            "checkout_session_id": checkout["cs_id"],
            "confirm_token": token,
            "selected_payment_method_type": "momo",
        },
        timeout=30,
        headers=momo_request_headers(
            session,
            "POST",
            url,
            {
                "Referer": referer,
                "x-openai-target-path": path,
                "x-openai-target-route": path,
            },
            flow="checkout_session_approval",
            referer=referer,
        ),
    )
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        raise ProtocolError(int(response.status_code), "Momo checkout confirm failed")
    payload = json_payload(response, "Momo checkout confirm")
    client_secret = _find_client_secret(payload)
    if client_secret:
        payload.setdefault("client_secret", client_secret)
    if (str(payload.get("status") or "").lower() not in {"success", "open", "processing"}) or not client_secret:
        nested = payload.get("setup_intent") or payload.get("payment_intent") or {}
        nested_keys = ",".join(sorted(str(key) for key in nested.keys())) if isinstance(nested, dict) else ""
        top_keys = ",".join(sorted(str(key) for key in payload.keys()))
        raise ProtocolError(
            409,
            "Momo checkout confirm did not return a client secret "
            f"(status={payload.get('status') or '-'}, top_keys={top_keys}, nested_keys={nested_keys})",
        )
    return payload


def intent_confirm(session: Any, checkout: dict[str, Any], token: str, confirmed: dict[str, Any]) -> dict[str, Any]:
    secret = _find_client_secret(confirmed)
    intent_id = secret.split("_secret_", 1)[0]
    if not intent_id.startswith(("pi_", "seti_")):
        raise ProtocolError(502, "Momo Stripe client secret has unsupported intent type")
    endpoint = f"https://api.stripe.com/v1/{'payment_intents' if intent_id.startswith('pi_') else 'setup_intents'}/{intent_id}/confirm"
    return_url_value = str(confirmed.get("confirm_return_url") or "").strip()
    if not return_url_value:
        return_url_value = (
            "https://chatgpt.com/checkout/verify?stripe_session_id="
            + str(checkout["cs_id"])
            + "&processor_entity="
            + str(checkout.get("processor_entity") or "openai_llc")
            + "&plan_type=plus"
        )
    data: dict[str, Any] = {
        "return_url": return_url_value,
        "confirmation_token": token,
        "key": str(checkout.get("publishable_key") or DEFAULT_STRIPE_PK),
        "_stripe_version": STRIPE_VERSION_BASE,
        "client_secret": secret,
    }
    attribution = _attribution_fields(checkout, source="l1")
    # The SetupIntent confirm in the complete VN HAR carries only these two
    # attribution fields; the richer set belongs to confirmation_tokens.
    data["client_attribution_metadata[client_session_id]"] = attribution[
        "client_session_id"
    ]
    data["client_attribution_metadata[merchant_integration_source]"] = "l1"
    return _post(session, endpoint, "Momo Stripe intent confirm", data)


def redirect_url(payload: dict[str, Any]) -> str:
    for key in ("redirect_to_url", "url", "next_action", "payment_method_options"):
        value = payload.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, dict):
            found = redirect_url(value)
            if found:
                return found
    return ""


def resolve_momo_redirect(session: Any, value: str) -> str:
    """Follow Stripe's one-hop redirect to the final MoMo gateway URL."""
    candidate = str(value or "").strip()
    if validate_momo_url(candidate):
        return candidate
    parsed = urlparse(candidate)
    if parsed.scheme != "https" or parsed.hostname != "pm-redirects.stripe.com":
        return ""
    try:
        response = session.request(
            "GET",
            candidate,
            allow_redirects=True,
            timeout=30,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://checkout.stripe.com/",
            },
        )
    except Exception:
        return ""
    final_url = str(getattr(response, "url", "") or "").strip()
    if validate_momo_url(final_url):
        return final_url
    location = ""
    headers = getattr(response, "headers", {}) or {}
    if hasattr(headers, "items"):
        for key, header_value in headers.items():
            if str(key).lower() == "location":
                location = str(header_value or "").strip()
                break
    return location if validate_momo_url(location) else ""


def validate_momo_url(value: str) -> bool:
    parsed = urlparse(str(value or ""))
    query = parse_qs(parsed.query)
    return parsed.scheme == "https" and parsed.hostname == "payment.momo.vn" and parsed.path == "/v2/gateway/pay" and bool(query.get("t")) and bool(query.get("s"))
