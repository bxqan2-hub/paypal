from __future__ import annotations

"""Stripe MoMo confirmation chain; no PayPal or GoPay protocol imports."""

import json
import os
import secrets
import uuid
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from .config import DEFAULT_STRIPE_PK, STRIPE_VERSION_BASE
from .errors import ProtocolError
from .momo_checkout import json_payload
from .momo_transport import (
    current_momo_pending_updates_header,
    momo_request_headers,
    record_momo_pending_updates,
)


class MomoConfirmBlockedError(ProtocolError):
    """A server-side approval decision with an incomplete runtime context."""

    # A blocked approval is retried only as a fresh checkout attempt.  It is
    # not classified as a login/session-token failure.
    retryable = True
    failure_mode = "approval_context_rejected"


def _cookie_value(session: Any, name: str) -> str:
    """Read a cookie from a requests/curl cookie jar or Cookie header."""
    cookies = getattr(session, "cookies", None)
    if cookies is not None:
        try:
            value = cookies.get(name)
        except Exception:
            value = ""
        if value:
            return str(value).strip()
    headers = getattr(session, "headers", {}) or {}
    raw = ""
    try:
        raw = str(headers.get("Cookie") or headers.get("cookie") or "")
    except Exception:
        raw = ""
    for pair in raw.split(";"):
        key, separator, value = pair.strip().partition("=")
        if separator and key.strip() == name:
            return value.strip()
    return ""


def _set_cookie(session: Any, name: str, value: str, domain: str) -> None:
    cookies = getattr(session, "cookies", None)
    if cookies is not None:
        try:
            cookies.set(name, value, domain=domain, path="/")
            return
        except Exception:
            pass
    headers = getattr(session, "headers", None)
    if headers is not None:
        existing = str(headers.get("Cookie") or "")
        pairs = [p.strip() for p in existing.split(";") if p.strip()]
        pairs = [p for p in pairs if not p.split("=", 1)[0].strip() == name]
        pairs.append(f"{name}={value}")
        headers["Cookie"] = "; ".join(pairs)


def _remove_cookie(session: Any, name: str, domain: str = "") -> None:
    """Remove a browser metric from a session that must remain cookie-free."""
    cookies = getattr(session, "cookies", None)
    if cookies is not None:
        try:
            if domain:
                cookies.clear(domain=domain, path="/", name=name)
            else:
                cookies.clear(name=name)
        except Exception:
            try:
                for cookie in list(cookies):
                    if str(getattr(cookie, "name", "")) == name:
                        cookies.clear(
                            domain=getattr(cookie, "domain", None),
                            path=getattr(cookie, "path", "/") or "/",
                            name=name,
                        )
            except Exception:
                pass
    headers = getattr(session, "headers", None)
    if headers is not None:
        raw = str(headers.get("Cookie") or headers.get("cookie") or "")
        if raw:
            pairs = [
                pair.strip()
                for pair in raw.split(";")
                if pair.strip() and pair.split("=", 1)[0].strip() != name
            ]
            headers["Cookie"] = "; ".join(pairs)


def synchronize_momo_stripe_browser_ids(
    chatgpt: Any, stripe: Any, checkout: dict[str, Any]
) -> dict[str, str]:
    """Keep Stripe ``muid``/``sid`` identical to the ChatGPT browser cookies.

    Stripe Elements sets these two cookies on the platform origin.  The
    confirmation token then echoes the same values, while ``guid`` remains a
    separate per-token identifier.  Generate a fresh 42-character value only
    when a live cookie is not available.
    """
    result: dict[str, str] = {}
    for cookie_name, checkout_key in (
        ("__stripe_mid", "stripe_muid"),
        ("__stripe_sid", "stripe_sid"),
    ):
        value = (
            _cookie_value(chatgpt, cookie_name)
            or _cookie_value(stripe, cookie_name)
            or str(checkout.get(checkout_key) or "").strip()
        )
        if len(value) != 42:
            value = _stripe_fingerprint_id()
        # The canonical VN HAR keeps Stripe API requests cookie-free.  The
        # metric value is echoed only in the ConfirmationToken form while the
        # ChatGPT page owns the corresponding first-party cookie.
        _set_cookie(chatgpt, cookie_name, value, ".chatgpt.com")
        _remove_cookie(stripe, cookie_name, ".stripe.com")
        provider = getattr(chatgpt, "openai_sentinel_provider", None)
        setter = getattr(provider, "set_cookie", None)
        if callable(setter):
            try:
                setter(cookie_name, value, http_only=False)
            except Exception:
                pass
        checkout[checkout_key] = value
        result[cookie_name] = value
    return result


def _payable_amount_minor(checkout: dict[str, Any]) -> int:
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    due = total.get("total") if isinstance(total.get("total"), dict) else {}
    raw = due.get("minorUnitsAmount")
    if raw in (None, ""):
        raw = checkout.get("payable_amount_minor")
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _momo_payment_method_types(checkout: dict[str, Any]) -> list[str]:
    """Use the live Checkout order, with the canonical VN fallback."""
    raw = checkout.get("payment_method_types")
    if isinstance(raw, (list, tuple)):
        values = [
            str(item or "").strip().lower()
            for item in raw
            if str(item or "").strip()
        ]
        values = list(dict.fromkeys(values))
        if values and "momo" in values:
            return values
    return ["card", "link", "momo"]


def _find_runtime_captcha(value: Any) -> str:
    """Find a live captcha token returned by a Stripe Elements context."""
    if isinstance(value, dict):
        for key in ("token", "captcha_token", "hcaptcha_token", "passive_captcha_token"):
            candidate = str(value.get(key) or "").strip()
            if candidate and len(candidate) >= 64:
                return candidate
        for child in value.values():
            found = _find_runtime_captcha(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_runtime_captcha(child)
            if found:
                return found
    return ""


def _find_captcha_field(value: Any, field_names: tuple[str, ...]) -> str:
    if isinstance(value, dict):
        for key in field_names:
            candidate = str(value.get(key) or "").strip()
            if candidate:
                return candidate
        for child in value.values():
            found = _find_captcha_field(child, field_names)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_captcha_field(child, field_names)
            if found:
                return found
    return ""


def _stripe_fingerprint_id() -> str:
    """Match Stripe.js's UUID-plus-hex fingerprint shape observed in HAR."""
    return f"{uuid.uuid4()}{secrets.token_hex(3)}"


def _attribution_fields(checkout: dict[str, Any], *, source: str) -> dict[str, str]:
    # Stripe's Elements controller, Link consumer lookups and both attribution
    # layers share one browser-generated 36-character session id.  Older code
    # created a second UUID at ConfirmationToken time, which broke that
    # cross-request binding even though the field names were present.
    session_id = str(
        checkout.get("stripe_client_session_id")
        or checkout.get("stripe_js_id")
        or uuid.uuid4()
    )
    checkout["stripe_client_session_id"] = session_id
    checkout["stripe_js_id"] = session_id
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
    stripe_session_id = str(
        checkout.get("stripe_js_id")
        or checkout.get("stripe_client_session_id")
        or uuid.uuid4()
    )
    checkout["stripe_js_id"] = stripe_session_id
    checkout["stripe_client_session_id"] = stripe_session_id
    params: dict[str, Any] = {}
    secret = str(checkout.get("customer_session_client_secret") or "").strip()
    if secret:
        # Stripe.js serializes this first in the current VN HAR.
        params["customer_session_client_secret"] = secret
    payment_method_types = _momo_payment_method_types(checkout)
    params.update({
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": "vnd",
        "deferred_intent[setup_future_usage]": "off_session",
    })
    for index, value in enumerate(payment_method_types):
        params[f"deferred_intent[payment_method_types][{index}]"] = value
    params.update({
        "currency": "vnd",
        "key": key,
        "_stripe_version": STRIPE_VERSION_BASE,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_session_id,
        "locale": "vi-VN",
        "browser_timezone": os.getenv("OPLL_MOMO_BROWSER_TIMEZONE", "").strip()
        or "Asia/Saigon",
        "type": "deferred_intent",
    })
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
    customer = payload.get("customer") if isinstance(payload, dict) else None
    if isinstance(customer, dict):
        customer_session = customer.get("customer_session")
        if isinstance(customer_session, dict):
            customer = customer_session.get("customer") or customer.get("id")
        else:
            customer = customer.get("id")
    if customer and str(customer).strip():
        checkout["customer"] = str(customer).strip()
    link_settings = payload.get("link_settings") if isinstance(payload, dict) else None
    if isinstance(link_settings, dict):
        for source, target in (
            ("link_hcaptcha_site_key", "momo_hcaptcha_link_site_key"),
            ("link_hcaptcha_rqdata", "momo_hcaptcha_link_rqdata"),
        ):
            value = str(link_settings.get(source) or "").strip()
            if value:
                checkout[target] = value
    checkout["momo_hcaptcha_site_key"] = _find_captcha_field(
        payload, ("site_key", "sitekey", "hcaptcha_site_key")
    )
    checkout["momo_hcaptcha_rqdata"] = _find_captcha_field(
        payload, ("rqdata", "hcaptcha_rqdata")
    )
    passive = payload.get("passive_captcha") if isinstance(payload, dict) else None
    if isinstance(passive, dict):
        checkout["momo_hcaptcha_provider"] = str(
            passive.get("captcha_provider") or "hcaptcha"
        ).strip()
        checkout["momo_hcaptcha_token_timeout_seconds"] = passive.get(
            "token_timeout_seconds"
        )
    checkout["momo_hcaptcha_required"] = bool(
        checkout.get("momo_hcaptcha_site_key")
        or checkout.get("momo_hcaptcha_link_site_key")
    )
    checkout["stripe_js_id"] = str(checkout.get("stripe_js_id") or params["stripe_js_id"])
    return payload


def prepare_momo_link_context(
    session: Any, checkout: dict[str, Any], email: str = ""
) -> dict[str, Any]:
    """Perform the live Stripe Link bootstrap shape with current values.

    These calls are browser background initializers observed in all complete
    VN MoMo captures.  They are best-effort: a null Link consumer response is
    valid and must not replace the authoritative Elements/Checkout state.
    """
    session_id = str(
        checkout.get("stripe_client_session_id")
        or checkout.get("stripe_js_id")
        or uuid.uuid4()
    )
    checkout["stripe_client_session_id"] = session_id
    checkout["stripe_js_id"] = session_id
    key = str(checkout.get("publishable_key") or DEFAULT_STRIPE_PK)
    address_email = str(email or checkout.get("account_email") or "").strip()
    result: dict[str, Any] = {
        "link_cookie_status": 0,
        "lookup_statuses": [],
        "lookup_successes": 0,
    }
    link_cookie_url = "https://merchant-ui-api.stripe.com/link/get-cookie"
    try:
        cookie_response = session.request(
            "GET",
            link_cookie_url,
            params={"referrer_host": "chatgpt.com"},
            timeout=30,
            headers={
                "Accept": "application/json",
                "Accept-Language": "vi-VN,vi;q=0.9",
                "Sec-Fetch-Storage-Access": "active",
            },
        )
        result["link_cookie_status"] = int(
            getattr(cookie_response, "status_code", 0) or 0
        )
        if result["link_cookie_status"] < 400:
            try:
                cookie_payload = json_payload(cookie_response, "Momo Stripe Link cookie")
            except ProtocolError:
                cookie_payload = {}
            secret = str(cookie_payload.get("auth_session_client_secret") or "").strip()
            if secret:
                checkout["stripe_link_auth_session_client_secret"] = secret
    except Exception:
        pass

    common = {
        "email_address": address_email,
        "email_source": "default_value",
        "session_id": session_id,
        "key": key,
        "link_global_holdback_data[assignment]": "control",
        "link_global_holdback_data[arb_id]": "",
    }
    forms: list[dict[str, Any]] = []
    forms.append(
        {
            "request_surface": "web_payment_element",
            **common,
        }
    )
    second: dict[str, Any] = {
        "request_surface": "web_link_authentication_in_payment_element",
        "transaction_context[link_supported_payment_methods][0]": "CARD",
        "transaction_context[is_recurring]": "true",
        "transaction_context[link_mode]": "LINK_CARD_BRAND",
    }
    for index, value in enumerate(
        (
            "CARD",
            "BANK_ACCOUNT",
            "KLARNA",
            "BALANCE",
            "PIX",
            "CRYPTO",
            "SEPA_BANK_ACCOUNT",
            "UPI",
        )
    ):
        second[f"supported_payment_details_types[{index}]"] = value
    customer = str(checkout.get("customer") or "").strip()
    if customer:
        second["customer_id"] = customer
    second.update(common)
    forms.append(second)
    forms.append(
        {
            "request_surface": "web_elements_controller",
            "email_address": address_email,
            "email_source": "default_value",
            "session_id": session_id,
            "key": key,
            "do_not_log_consumer_funnel_event": "true",
        }
    )
    for form in forms:
        try:
            response = session.request(
                "POST",
                "https://api.stripe.com/v1/consumers/sessions/lookup",
                data=form,
                timeout=30,
                headers={
                    "Accept": "application/json",
                    # Stripe.js's consumer lookup uses the compact `en`
                    # locale in the canonical VN browser capture (the
                    # hosted checkout itself remains vi-VN).
                    "Accept-Language": "en",
                },
            )
            status = int(getattr(response, "status_code", 0) or 0)
            result["lookup_statuses"].append(status)
            if status < 400:
                result["lookup_successes"] += 1
                try:
                    payload = json_payload(response, "Momo Stripe consumer lookup")
                except ProtocolError:
                    payload = {}
                if isinstance(payload, dict):
                    consumer = payload.get("consumer_session")
                    if consumer not in (None, "", {}, []):
                        checkout["stripe_consumer_session"] = consumer
        except Exception:
            result["lookup_statuses"].append(0)
    checkout["momo_link_context"] = result
    return result


def confirmation_token(session: Any, checkout: dict[str, Any], billing: dict[str, str], captcha: str = "") -> str:
    key = str(checkout.get("publishable_key") or DEFAULT_STRIPE_PK)
    nested_attribution = _attribution_fields(checkout, source="elements")
    stripe_js_version = (
        os.getenv("OPLL_MOMO_STRIPE_JS_VERSION", "").strip() or "939d686cd5"
    )
    try:
        time_on_page = max(
            1,
            min(
                120000,
                int(os.getenv("OPLL_MOMO_TIME_ON_PAGE_MS", "") or "20000"),
            ),
        )
    except ValueError:
        time_on_page = 20000
    guid = _stripe_fingerprint_id()
    muid = str(checkout.get("stripe_muid") or _stripe_fingerprint_id())
    sid = str(checkout.get("stripe_sid") or _stripe_fingerprint_id())
    payment_method_types = _momo_payment_method_types(checkout)
    body: dict[str, Any] = {
        "payment_method_data[type]": "momo",
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][country]": "VN",
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        # The captured MoMo Elements form submits an empty phone field.  Keep
        # it empty by default; an explicitly injected runtime value remains
        # available without changing the stable HAR shape.
        "payment_method_data[billing_details][phone]": os.getenv(
            "OPLL_MOMO_STRIPE_BILLING_PHONE", ""
        ).strip(),
        "payment_method_data[payment_user_agent]": (
            "stripe.js/"
            + stripe_js_version
            + "; stripe-js-v3/"
            + stripe_js_version
            + "; payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(time_on_page),
    }
    supplied_captcha = str(captcha or "").strip()
    env_captcha = os.getenv("OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN", "").strip()
    runtime_captcha = _find_runtime_captcha(checkout.get("elements_session"))
    captcha_value = supplied_captcha or env_captcha or runtime_captcha
    checkout["momo_hcaptcha_source"] = (
        "argument" if supplied_captcha else
        "environment" if env_captcha else
        "elements_session" if runtime_captcha else
        "absent"
    )
    checkout["momo_hcaptcha_supplied"] = bool(captcha_value)
    ctx = checkout.get("elements_session") if isinstance(checkout.get("elements_session"), dict) else {}
    elements_session_id = str(
        ctx.get("session_id") or ctx.get("id") or ctx.get("elements_session_id") or ""
    )
    elements_config_id = str(
        ctx.get("config_id") or ctx.get("elements_session_config_id") or ""
    )
    # The browser places the nested Elements attribution before the metrics
    # identifiers and the optional radar token.
    for name, value in nested_attribution.items():
        if value:
            body[f"payment_method_data[client_attribution_metadata][{name}]"] = value
    if elements_session_id:
        body[
            "payment_method_data[client_attribution_metadata][elements_session_id]"
        ] = elements_session_id
    if elements_config_id:
        body[
            "payment_method_data[client_attribution_metadata][elements_session_config_id]"
        ] = elements_config_id
    for index, value in enumerate(("expressCheckout", "payment", "address")):
        body[
            f"payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][{index}]"
        ] = value
    body["payment_method_data[guid]"] = guid
    body["payment_method_data[muid]"] = muid
    body["payment_method_data[sid]"] = sid
    if captcha_value:
        body["payment_method_data[radar_options][hcaptcha_token]"] = captcha_value
    body["setup_future_usage"] = "off_session"
    body["mandate_data[customer_acceptance][type]"] = "online"
    body["mandate_data[customer_acceptance][online][infer_from_client]"] = "true"
    body["client_context[currency]"] = "vnd"
    body["client_context[mode]"] = "subscription"
    for index, value in enumerate(payment_method_types):
        body[f"client_context[payment_method_types][{index}]"] = value
    customer = str(checkout.get("customer") or "").strip()
    if customer:
        body["client_context[customer]"] = customer
    for name, value in nested_attribution.items():
        body[f"client_attribution_metadata[{name}]"] = value
    for index, value in enumerate(("expressCheckout", "payment", "address")):
        body[f"client_attribution_metadata[merchant_integration_additional_elements][{index}]"] = value
    if elements_session_id:
        body["client_attribution_metadata[elements_session_id]"] = elements_session_id
    if elements_config_id:
        body["client_attribution_metadata[elements_session_config_id]"] = elements_config_id
    body["set_as_default_payment_method"] = "false"
    body["key"] = key
    body["_stripe_version"] = STRIPE_VERSION_BASE
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
    record_momo_pending_updates(session, response)
    status_code = int(getattr(response, "status_code", 0) or 0)
    try:
        payload = json_payload(response, "Momo checkout confirm")
    except ProtocolError:
        if status_code >= 400:
            raise ProtocolError(status_code, "Momo checkout confirm failed")
        raise
    client_secret = _find_client_secret(payload)
    if client_secret:
        payload.setdefault("client_secret", client_secret)
    status_value = str(payload.get("status") or "").lower()
    if status_code >= 400 or status_value not in {"success", "open", "processing"} or not client_secret:
        response_keys = ",".join(sorted(str(key) for key in payload.keys()))
        response_type = str(payload.get("type") or "")
        has_attestation = bool(
            str(
                getattr(session, "headers", {}).get(
                    "oai-web-deployment-attestation", ""
                )
                if getattr(session, "headers", None) is not None
                else ""
            ).strip()
        )
        error_type = (
            MomoConfirmBlockedError
            if status_value == "blocked"
            else ProtocolError
        )
        last_headers = getattr(session, "momo_last_request_headers", {}) or {}
        if not isinstance(last_headers, dict):
            last_headers = {}
        pending_header = current_momo_pending_updates_header(session)
        try:
            pending_count = int(
                getattr(session, "momo_last_request_pending_updates", -1)
            )
        except (TypeError, ValueError):
            pending_count = -1
        if pending_count < 0:
            try:
                pending_count = len(json.loads(pending_header).get("updates", []))
            except Exception:
                pending_count = 0
        account_cookie = bool(_cookie_value(session, "_account"))
        stripe_ids_consistent = str(
            checkout.get("stripe_js_id") or ""
        ) == str(checkout.get("stripe_client_session_id") or "")
        hydration = checkout.get("momo_checkout_hydration")
        hydration_status = (
            hydration.get("status", 0)
            if isinstance(hydration, dict)
            else 0
        )
        link_context = checkout.get("momo_link_context")
        link_successes = (
            link_context.get("lookup_successes", 0)
            if isinstance(link_context, dict)
            else 0
        )
        raise error_type(
            status_code if status_code >= 400 else 409,
            "Momo checkout confirm did not return a client secret "
            f"(status={status_value or '?'}, type={response_type or '?'}, "
            f"response_keys={response_keys or '?'}, "
            f"hcaptcha={checkout.get('momo_hcaptcha_source', 'absent')}, "
            f"hcaptcha_site_key={'present' if checkout.get('momo_hcaptcha_site_key') else 'absent'}, "
            f"pending_updates={pending_count}, "
            f"account_cookie={'present' if account_cookie else 'absent'}, "
            f"hydration_status={hydration_status}, "
            f"link_lookups={link_successes}, "
            f"backend_at_bridge={'present' if getattr(session, 'momo_backend_auth_bridge_enabled', False) else 'absent'}, "
            f"browser_receipts={'enabled' if getattr(session, 'momo_browser_receipts_enabled', False) else 'gated'}, "
            f"timezone={'applied' if getattr(session, 'momo_timezone_applied', False) else 'default'}, "
            f"stripe_session_id={'consistent' if stripe_ids_consistent else 'mismatch'}, "
            f"sentinel={'present' if any(str(k).lower() == 'openai-sentinel-token' and str(v).strip() for k, v in last_headers.items()) else 'absent'}, "
            f"oai_telemetry={'present' if any(str(k).lower() == 'oai-telemetry' and str(v).strip() for k, v in last_headers.items()) else 'absent'}, "
            f"attestation={'present' if has_attestation else 'absent'})",
        )
    return payload


def intent_confirm(session: Any, checkout: dict[str, Any], token: str, confirmed: dict[str, Any]) -> dict[str, Any]:
    secret = _find_client_secret(confirmed)
    intent_id = secret.split("_secret_", 1)[0]
    if not intent_id.startswith(("pi_", "seti_")):
        raise ProtocolError(502, "Momo Stripe client secret has unsupported intent type")
    endpoint = f"https://api.stripe.com/v1/{'payment_intents' if intent_id.startswith('pi_') else 'setup_intents'}/{intent_id}/confirm"
    data: dict[str, Any] = {
        "return_url": str(
            confirmed.get("confirm_return_url")
            or f"https://chatgpt.com/checkout/verify?stripe_session_id={checkout['cs_id']}"
        ),
        "confirmation_token": token,
        "key": str(checkout.get("publishable_key") or DEFAULT_STRIPE_PK),
        # This is the OAICS/Custom branch.  The long beta suffix belongs to
        # hosted cs_live_ Payment Pages; the VN MoMo HAR uses the base version.
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    attribution = _attribution_fields(checkout, source="l1")
    # OAICS/Custom intent confirmation carries only the two top-level
    # attribution fields observed in the VN HAR.  The additional Elements
    # metadata belongs to confirmation_tokens, not this endpoint.
    for name in ("client_session_id", "merchant_integration_source"):
        value = attribution[name]
        data[f"client_attribution_metadata[{name}]"] = value
    data["client_secret"] = secret
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
            # Read the authorize Location without fetching the MoMo page in
            # the Stripe session.  The dedicated MoMo session performs the
            # single gateway GET later, matching the browser HAR and keeping
            # Cookie/CSRF state on the correct host.
            allow_redirects=False,
            timeout=30,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": "https://chatgpt.com/",
                "Content-Type": None,
                "Origin": None,
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "cross-site",
                "Upgrade-Insecure-Requests": "1",
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
