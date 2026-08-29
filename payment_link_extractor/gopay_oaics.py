from __future__ import annotations

import json
import random
import re
import time
import uuid
from typing import Callable
from typing import Any

from .gopay_checkout import (
    chatgpt_success_return_url,
    first_value_by_key,
    merge_checkout_payload,
    openai_checkout_email,
)
from .config import (
    DEFAULT_TIMEOUT,
    OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION,
    STRIPE_VERSION_BASE,
    STRIPE_VERSION_FULL,
    normalize_payment_method,
    processor_entity_for_country,
)
from .errors import ProtocolError
from .logging_utils import emit_log
from .models import CheckoutData, ExtractionConfig, StripeContext
from .providers import provider_redirect_config
from .gopay_stripe_common import (
    cs_stripe_headers,
    ensure_payment_method_offered,
    extract_redirect_to_url,
    openai_stripe_headers,
    is_paypal_ba_approval_url,
    resolve_external_redirect,
    stripe_additional_elements_params,
    stripe_context,
    stripe_deferred_intent_params,
    stripe_key,
)
from .gopay_transport import openai_sentinel_headers, response_json, stage_http_request


CHECKOUT_DATA_ROUTE_QUERY = "_routes=routes%2Fcheckout.%24entity.%24checkoutId"


def openai_checkout_init_payload(checkout: CheckoutData) -> dict[str, Any]:
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    subtotal = total.get("subtotal") if isinstance(total.get("subtotal"), dict) else {}
    due = total.get("total") if isinstance(total.get("total"), dict) else {}
    return {
        "currency": str(state.get("currency") or checkout.get("currency") or "GBP").lower(),
        "payment_method_types": checkout.get("payment_method_types") or [],
        "custom_payment_methods": checkout.get("custom_payment_methods") or [],
        "total_summary": {
            "due": due.get("minorUnitsAmount"),
            "subtotal": subtotal.get("minorUnitsAmount"),
            "total": due.get("minorUnitsAmount"),
        },
    }


def openai_elements_session(
    stripe: Any,
    config: ExtractionConfig,
    checkout: CheckoutData,
    init_payload: dict[str, Any],
    ctx: StripeContext,
    log: Any | None,
    *,
    reuse_session: bool = False,
) -> dict[str, Any]:
    customer_secret = str(checkout.get("customer_session_client_secret") or "").strip()
    if not customer_secret:
        raise ProtocolError(502, "oaics checkout missing customer_session_client_secret")
    methods = payment_method_types(init_payload)
    ctx["payment_method_types"] = methods
    params = {
        "customer_session_client_secret": customer_secret,
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_BASE,
        "elements_init_source": "stripe.elements",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": ctx["stripe_js_id"],
        "locale": config_locale(config),
        "type": "deferred_intent",
    }
    params.update(stripe_deferred_intent_params(expected_amount(init_payload), config_currency(config), methods))
    if reuse_session and ctx.get("elements_session_id"):
        params["session_id"] = str(ctx["elements_session_id"])
    custom = init_payload.get("custom_payment_methods")
    if isinstance(custom, list):
        for index, item in enumerate(custom):
            custom_id = item.get("id") if isinstance(item, dict) else item
            if custom_id:
                params[f"custom_payment_methods[{index}]"] = str(custom_id)
    response = stage_http_request(
        stripe,
        "Stripe Elements session",
        "GET",
        "https://api.stripe.com/v1/elements/sessions",
        log,
        params=params,
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe Elements session failed: {response.text[:500]}")
    payload = response_json(response, "Stripe Elements session")
    if payload.get("session_id"):
        ctx["elements_session_id"] = str(payload["session_id"])
    if payload.get("config_id"):
        ctx["elements_session_config_id"] = str(payload["config_id"])
        ctx["config_id"] = str(payload["config_id"])
    customer = payload.get("customer") if isinstance(payload.get("customer"), dict) else {}
    customer_session = customer.get("customer_session") if isinstance(customer.get("customer_session"), dict) else {}
    if customer_session.get("customer"):
        ctx["customer_id"] = str(customer_session["customer"])
    return payload


def openai_checkout_taxes(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/taxes"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    body = {
        "checkout_session_id": checkout["cs_id"],
        "checkout_email": openai_checkout_email(checkout) or billing["email"],
        "billing_country": config.country.upper(),
        "billing_name": billing["name"],
        "currency": config_currency(config).lower(),
        "processor_entity": processor,
        "tax_id": None,
        "billing_address": {
            "line1": billing["line1"],
            "line2": "",
            "city": billing["city"],
            "country": config.country.upper(),
            "postal_code": billing["postal_code"],
            "state": billing["state"],
        },
    }
    response = stage_http_request(
        chatgpt,
        "ChatGPT oaics checkout/taxes",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json=body,
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
            **openai_sentinel_headers(
                chatgpt,
                flow="chatgpt_checkout",
                referer=f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
                log=log,
                required=True,
            ),
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"oaics checkout/taxes failed: {response.text[:500]}")
    payload = response_json(response, "oaics checkout/taxes")
    merge_checkout_payload(checkout, payload)
    return payload


def add_attribution(body: dict[str, str], ctx: StripeContext, prefix: str) -> None:
    values = {
        "client_session_id": ctx.get("stripe_js_id", ""),
        "merchant_integration_source": "elements",
        "merchant_integration_subtype": "payment-element",
        "merchant_integration_version": "2021",
        "payment_intent_creation_flow": "deferred",
        "payment_method_selection_flow": "merchant_specified",
        "elements_session_id": ctx.get("elements_session_id", ""),
        "elements_session_config_id": ctx.get("elements_session_config_id", ""),
    }
    for key, value in values.items():
        body[f"{prefix}[{key}]"] = str(value)


def openai_confirmation_token(
    stripe: Any,
    config: ExtractionConfig,
    checkout: CheckoutData,
    billing: dict[str, str],
    ctx: StripeContext,
    payment_method: str,
    log: Any | None,
) -> str:
    runtime = OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION
    body = {
        "payment_method_data[type]": payment_method,
        "payment_method_data[billing_details][name]": billing["name"],
        "payment_method_data[billing_details][address][line1]": billing["line1"],
        "payment_method_data[billing_details][address][city]": billing["city"],
        "payment_method_data[billing_details][address][country]": config.country.upper(),
        "payment_method_data[billing_details][address][postal_code]": billing["postal_code"],
        "payment_method_data[billing_details][address][state]": billing["state"],
        "payment_method_data[billing_details][phone]": billing["phone"],
        "payment_method_data[payment_user_agent]": f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent",
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(45000, 85000)),
        "payment_method_data[guid]": uuid.uuid4().hex,
        "payment_method_data[muid]": uuid.uuid4().hex,
        "payment_method_data[sid]": uuid.uuid4().hex,
        "setup_future_usage": "off_session",
        "mandate_data[customer_acceptance][type]": "online",
        "mandate_data[customer_acceptance][online][infer_from_client]": "true",
        "client_context[currency]": config_currency(config).lower(),
        "client_context[mode]": "subscription",
        "set_as_default_payment_method": "false",
        "key": stripe_key(checkout),
        "_stripe_version": STRIPE_VERSION_BASE,
    }
    add_attribution(body, ctx, "payment_method_data[client_attribution_metadata]")
    add_attribution(body, ctx, "client_attribution_metadata")
    body.update(stripe_additional_elements_params("payment_method_data[client_attribution_metadata]"))
    body.update(stripe_additional_elements_params("client_attribution_metadata"))
    for index, method in enumerate(ctx.get("payment_method_types") or []):
        body[f"client_context[payment_method_types][{index}]"] = str(method)
    if ctx.get("customer_id"):
        body["client_context[customer]"] = str(ctx["customer_id"])
    if config.stripe_hcaptcha_token:
        body["payment_method_data[radar_options][hcaptcha_token]"] = config.stripe_hcaptcha_token
    response = stage_http_request(
        stripe,
        "Stripe confirmation token",
        "POST",
        "https://api.stripe.com/v1/confirmation_tokens",
        log,
        data=body,
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe confirmation token failed: {response.text[:500]}")
    token = str(response_json(response, "Stripe confirmation token").get("id") or "")
    if not token.startswith("ctoken_"):
        raise ProtocolError(502, "Stripe confirmation token response missing ctoken_ id")
    return token


def openai_checkout_confirm(
    chatgpt: Any,
    checkout: CheckoutData,
    confirmation_token: str,
    payment_method: str,
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/confirm"
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "GB"),
        str(checkout.get("processor_entity") or ""),
    )
    response = stage_http_request(
        chatgpt,
        "ChatGPT oaics checkout/confirm",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json={
            "checkout_session_id": checkout["cs_id"],
            "confirm_token": confirmation_token,
            "selected_payment_method_type": payment_method,
        },
        headers={
            "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
            "x-openai-target-path": path,
            "x-openai-target-route": path,
            **openai_sentinel_headers(
                chatgpt,
                flow="chatgpt_checkout",
                referer=f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
                log=log,
                required=True,
            ),
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"oaics checkout/confirm failed for {payment_method}: {response.text[:500]}")
    payload = response_json(response, "oaics checkout/confirm")
    if str(payload.get("status") or "").lower() != "success" or not payload.get("client_secret"):
        raise ProtocolError(409, f"oaics checkout/confirm rejected for {payment_method} or missing client_secret")
    return payload


def openai_intent_confirm(
    stripe: Any,
    checkout: CheckoutData,
    confirmation_token: str,
    confirm_payload: dict[str, Any],
    ctx: StripeContext,
    log: Any | None,
) -> dict[str, Any]:
    client_secret = str(confirm_payload.get("client_secret") or "").strip()
    if "_secret_" not in client_secret:
        raise ProtocolError(502, "oaics checkout/confirm returned invalid client_secret")
    intent_id = client_secret.split("_secret_", 1)[0]
    if intent_id.startswith("pi_"):
        label = "PaymentIntent"
        endpoint = f"https://api.stripe.com/v1/payment_intents/{intent_id}/confirm"
    elif intent_id.startswith("seti_"):
        label = "SetupIntent"
        endpoint = f"https://api.stripe.com/v1/setup_intents/{intent_id}/confirm"
    else:
        raise ProtocolError(502, "oaics client_secret has unsupported intent type")
    return_url = str(
        confirm_payload.get("confirm_return_url")
        or checkout.get("confirm_return_url")
        or chatgpt_success_return_url(checkout)
    )
    response = stage_http_request(
        stripe,
        f"Stripe {label} confirm",
        "POST",
        endpoint,
        log,
        data={
            "return_url": return_url,
            "confirmation_token": confirmation_token,
            "key": stripe_key(checkout),
            "_stripe_version": STRIPE_VERSION_FULL,
            "client_attribution_metadata[client_session_id]": ctx["stripe_js_id"],
            "client_attribution_metadata[merchant_integration_source]": "l1",
            "client_secret": client_secret,
        },
        headers=openai_stripe_headers(),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"Stripe {label} confirm failed: {response.text[:500]}")
    return response_json(response, f"Stripe {label} confirm")


def _custom_payment_methods(payload: Any) -> list[Any]:
    value = first_value_by_key(payload, "custom_payment_methods")
    return value if isinstance(value, list) else []


def _gcash_custom_payment_method_id(payload: Any) -> str:
    """Find the upstream OAICS custom-method id used by GCash."""
    methods = _custom_payment_methods(payload)
    candidates: list[str] = []
    for item in methods:
        if isinstance(item, dict):
            identifier = str(
                item.get("id")
                or item.get("type_id")
                or item.get("custom_payment_method_type_id")
                or item.get("typeId")
                or ""
            ).strip()
            label = " ".join(
                str(item.get(key) or "").strip().lower()
                for key in ("name", "display_name", "type", "payment_method_type")
            )
        else:
            identifier = str(item or "").strip()
            label = identifier.lower()
        if not identifier.startswith("cpmt_"):
            continue
        if "gcash" in label:
            return identifier
        candidates.append(identifier)
    # PH OAICS currently publishes only GCash as a cpmt_* method.  Retaining
    # the first id mirrors the upstream project while still preferring an
    # explicitly labelled GCash entry when Stripe adds metadata.
    return candidates[0] if candidates else ""


def _openai_checkout_headers(
    chatgpt: Any,
    path: str,
    checkout: CheckoutData,
    *,
    sentinel: bool = False,
) -> dict[str, str]:
    """Build the backend headers observed in the current browser HARs.

    Session-wide identity headers (device/session/client build) are supplied
    by ``DefaultTransportFactory``.  Sentinel is deliberately opt-in because
    the browser only attaches its short-lived token to ``checkout/confirm``.
    """
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "PH"),
        str(checkout.get("processor_entity") or ""),
    )
    headers = {
        "Accept": "*/*",
        "Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}",
        "x-openai-target-path": path,
        "x-openai-target-route": path,
    }
    if sentinel:
        headers.update(openai_sentinel_headers(chatgpt))
    return headers


def _script_json_candidates(source: str) -> list[dict[str, Any]]:
    """Extract object payloads from the route-data script returned by ChatGPT.

    The browser HAR exports the ``.data`` response as ``text/x-script`` on
    some deployments, while other deployments return ordinary JSON.  Keeping
    this parser permissive lets the GCash flow consume both shapes without
    coupling it to a framework-specific loader wrapper.
    """
    text = str(source or "").strip()
    if not text:
        return []
    decoder = json.JSONDecoder()
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if isinstance(value, dict):
            marker = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
            if marker not in seen:
                seen.add(marker)
                candidates.append(value)
            for nested in value.values():
                add(nested)
        elif isinstance(value, list):
            for nested in value:
                add(nested)

    try:
        add(json.loads(text))
    except Exception:
        pass
    for index, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except (TypeError, ValueError):
            continue
        add(value)
    # Remix/React route loaders sometimes embed the JSON as an escaped string
    # inside a script push call (for example ``[1, "{\\"checkout_session\\"...")``).
    for match in re.finditer(r'"((?:\\.|[^"\\])*)"', text):
        try:
            decoded = json.loads('"' + match.group(1) + '"')
        except (TypeError, ValueError):
            continue
        if isinstance(decoded, str) and any(key in decoded for key in ("custom_payment_methods", "checkout_session", "checkout_state")):
            for nested in _script_json_candidates(decoded):
                add(nested)
    return candidates


def fetch_custom_checkout_data_state(
    chatgpt: Any,
    checkout: CheckoutData,
    log: Any | None,
) -> dict[str, Any]:
    """Hydrate OAICS state from the browser's current ``.data`` route.

    Full GCash HARs request this route immediately after checkout creation.
    An empty mapping signals that a deployment did not expose the route so the
    caller can use the legacy backend endpoint as a compatibility fallback.
    """
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "PH"),
        str(checkout.get("processor_entity") or ""),
    )
    checkout_id = str(checkout.get("cs_id") or "").strip()
    if not checkout_id:
        return {}
    path = f"/checkout/{processor}/{checkout_id}.data"
    response = stage_http_request(
        chatgpt,
        "ChatGPT GCash checkout route data",
        "GET",
        "https://chatgpt.com" + path + "?" + CHECKOUT_DATA_ROUTE_QUERY,
        log,
        headers={
            "Accept": "*/*",
            # The browser keeps the promo landing page as the referrer while
            # hydrating the route loader; checkout/taxes/confirm switch to the
            # session URL afterwards.
            "Referer": "https://chatgpt.com/?promo_campaign=plus-1-month-free",
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code in {404, 405, 410} or response.status_code >= 500:
        return {}
    if response.status_code >= 400:
        return {}
    payload: dict[str, Any] | None = None
    try:
        payload = response_json(response, "GCash checkout route data")
    except ProtocolError:
        for candidate in _script_json_candidates(getattr(response, "text", "")):
            if _custom_payment_methods(candidate) or first_value_by_key(candidate, "checkout_session") is not None:
                payload = candidate
                break
        if payload is None:
            candidates = _script_json_candidates(getattr(response, "text", ""))
            payload = candidates[0] if candidates else None
    if not isinstance(payload, dict) or not payload:
        return {}
    if not (
        _custom_payment_methods(payload)
        or first_value_by_key(payload, "checkout_session") is not None
        or first_value_by_key(payload, "checkout_state") is not None
        or first_value_by_key(payload, "payment_method_types") is not None
    ):
        return {}
    merge_checkout_payload(checkout, payload)
    return payload


def fetch_custom_checkout_state(
    chatgpt: Any,
    checkout: CheckoutData,
    log: Any | None,
) -> dict[str, Any]:
    route_payload = fetch_custom_checkout_data_state(chatgpt, checkout, log)
    if route_payload:
        return route_payload
    processor = processor_entity_for_country(
        str(checkout.get("billing_country") or "PH"),
        str(checkout.get("processor_entity") or ""),
    )
    path = f"/backend-api/payments/checkout/{processor}/{checkout['cs_id']}"
    response = stage_http_request(
        chatgpt,
        "ChatGPT GCash custom checkout",
        "GET",
        "https://chatgpt.com" + path,
        log,
        headers=_openai_checkout_headers(chatgpt, path, checkout),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"GCash custom checkout read failed: {response.text[:500]}")
    payload = response_json(response, "GCash custom checkout")
    merge_checkout_payload(checkout, payload)
    return payload


def confirm_custom_checkout_method(
    chatgpt: Any,
    checkout: CheckoutData,
    custom_payment_method_id: str,
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/confirm"
    response = stage_http_request(
        chatgpt,
        "ChatGPT GCash payment-method confirm",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json={
            "checkout_session_id": checkout["cs_id"],
            "selected_payment_method_type": custom_payment_method_id,
        },
        headers={
            **_openai_checkout_headers(chatgpt, path, checkout),
            **openai_sentinel_headers(
                chatgpt,
                flow="checkout_session_approval",
                referer=f"https://chatgpt.com/checkout/"
                f"{processor_entity_for_country(str(checkout.get('billing_country') or 'PH'), str(checkout.get('processor_entity') or ''))}/"
                f"{checkout['cs_id']}",
                log=log,
            ),
        },
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"GCash payment-method confirm failed: {response.text[:500]}")
    payload = response_json(response, "GCash payment-method confirm")
    status = str(payload.get("status") or "").lower()
    if status != "success":
        raise ProtocolError(409, f"GCash payment-method confirm rejected: status={status or '?'}")
    return payload


def start_custom_checkout_method(
    chatgpt: Any,
    checkout: CheckoutData,
    custom_payment_method_id: str,
    log: Any | None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/custom_payment_method/start"
    response = stage_http_request(
        chatgpt,
        "ChatGPT GCash payment start",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json={
            "checkout_session_id": checkout["cs_id"],
            "custom_payment_method_type_id": custom_payment_method_id,
        },
        headers=_openai_checkout_headers(chatgpt, path, checkout),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"GCash payment start failed: {response.text[:500]}")
    payload = response_json(response, "GCash payment start")
    redirect = _custom_action_url(payload)
    if str(payload.get("status") or "").lower() != "requires_action" or not redirect:
        raise ProtocolError(502, "GCash payment start did not return a redirect URL")
    return payload


def _custom_action_url(payload: Any) -> str:
    """Extract the redirect URL across OAICS response shape revisions."""
    if not isinstance(payload, dict):
        return ""
    action = payload.get("next_action")
    candidates: list[Any] = []
    if isinstance(action, dict):
        candidates.extend(
            action.get(key)
            for key in ("url", "redirect_url", "redirectUrl", "checkout_url")
        )
        redirect_to_url = action.get("redirect_to_url")
        if isinstance(redirect_to_url, dict):
            candidates.append(redirect_to_url.get("url"))
    candidates.extend(payload.get(key) for key in ("redirect_url", "redirectUrl", "url"))
    for value in candidates:
        if isinstance(value, str) and value.strip().startswith(("http://", "https://")):
            return value.strip()
    return ""


def continue_custom_checkout_method(
    chatgpt: Any,
    checkout: CheckoutData,
    callback_payload: dict[str, Any] | None,
    log: Any | None,
) -> dict[str, Any]:
    """Complete the browser's post-GCash callback handshake when available.

    The full HAR captures show this request only after GCash redirects back to
    ``/checkout/verify``.  Link extraction normally stops at the provider URL,
    but keeping the callback endpoint here makes the observed state machine
    explicit for callers that already possess the callback fields.
    """
    path = "/backend-api/payments/checkout/custom_payment_method/continue"
    body = dict(callback_payload or {})
    body.setdefault("checkout_session_id", checkout["cs_id"])
    response = stage_http_request(
        chatgpt,
        "ChatGPT GCash payment continue",
        "POST",
        "https://chatgpt.com" + path,
        log,
        json=body,
        headers=_openai_checkout_headers(chatgpt, path, checkout),
        timeout=DEFAULT_TIMEOUT,
    )
    if response.status_code >= 400:
        raise ProtocolError(response.status_code, f"GCash payment continue failed: {response.text[:500]}")
    payload = response_json(response, "GCash payment continue")
    status = str(payload.get("status") or "").lower()
    if status in {"failed", "error"} or payload.get("success") is False:
        raise ProtocolError(409, f"GCash payment continue rejected: status={status or '?'}")
    return payload


def extract_oaics_gcash_provider(
    config: ExtractionConfig,
    chatgpt: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
    *,
    stripe: Any | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, str]:
    """Run the OAICS custom-payment GCash path from the reference project."""
    state: dict[str, Any] = checkout
    custom_method_id = _gcash_custom_payment_method_id(state)
    for attempt in range(4):
        if custom_method_id:
            break
        if attempt:
            time.sleep(0.8 * attempt)
        state = fetch_custom_checkout_state(chatgpt, checkout, log)
        custom_method_id = _gcash_custom_payment_method_id(state) or _gcash_custom_payment_method_id(checkout)
    if not custom_method_id:
        raise ProtocolError(409, "GCash custom payment method is not available in the PH Checkout")

    # The browser initializes Stripe Elements even though GCash itself is a
    # custom payment method.  That initialization refreshes the OAICS session
    # state and is followed by more than one taxes calculation in every recent
    # successful capture.  Keep it opportunistic for older deployments and
    # lightweight test doubles that do not expose a Stripe transport.
    elements_ctx: StripeContext | None = None
    elements_enabled = bool(
        stripe is not None
        and callable(getattr(stripe, "request", None))
        and str(checkout.get("customer_session_client_secret") or "").strip()
    )
    if elements_enabled:
        init_payload = openai_checkout_init_payload(checkout)
        elements_ctx = stripe_context(init_payload, checkout)
        try:
            openai_elements_session(stripe, config, checkout, init_payload, elements_ctx, log)
        except Exception as exc:
            emit_log(log, f"GCash Elements bootstrap skipped: {type(exc).__name__}")
            elements_ctx = None

    if stage_callback:
        stage_callback("taxes")
    taxes_payload = openai_checkout_taxes(config, chatgpt, checkout, billing, log)
    # The current browser uses the taxes response as the refreshed checkout
    # state; it does not issue the legacy backend checkout GET.  Fall back to
    # that endpoint only when a deployment omits custom_payment_methods from
    # the taxes response and the original checkout state is also incomplete.
    state = taxes_payload
    custom_method_id = _gcash_custom_payment_method_id(state) or _gcash_custom_payment_method_id(checkout)
    if not custom_method_id:
        state = fetch_custom_checkout_state(chatgpt, checkout, log)
        custom_method_id = _gcash_custom_payment_method_id(state) or _gcash_custom_payment_method_id(checkout)
    if not custom_method_id:
        raise ProtocolError(409, "GCash custom payment method disappeared after tax refresh")

    # Reuse the Elements session before a second taxes refresh, matching the
    # route observed in m.gcash.com4.har and m.gcash.com5.har.  A deployment
    # that rejects the optional refresh still retains the first valid state.
    if elements_ctx is not None:
        try:
            refreshed_init = openai_checkout_init_payload(checkout)
            openai_elements_session(
                stripe,
                config,
                checkout,
                refreshed_init,
                elements_ctx,
                log,
                reuse_session=True,
            )
        except Exception as exc:
            emit_log(log, f"GCash Elements refresh skipped: {type(exc).__name__}")
    try:
        if stage_callback:
            stage_callback("taxes_refresh")
        second_taxes = openai_checkout_taxes(config, chatgpt, checkout, billing, log)
        state = second_taxes
        custom_method_id = _gcash_custom_payment_method_id(state) or _gcash_custom_payment_method_id(checkout)
    except ProtocolError as exc:
        # Older OAICS deployments accepted one tax request; retain that
        # compatibility while preferring the browser's two-refresh contract.
        emit_log(log, f"GCash second taxes refresh skipped: {exc.status_code}")
    if not custom_method_id:
        state = fetch_custom_checkout_state(chatgpt, checkout, log)
        custom_method_id = _gcash_custom_payment_method_id(state) or _gcash_custom_payment_method_id(checkout)
    if not custom_method_id:
        raise ProtocolError(409, "GCash custom payment method disappeared after second tax refresh")

    if stage_callback:
        stage_callback("payment_confirmation")
    confirmed = confirm_custom_checkout_method(chatgpt, checkout, custom_method_id, log)
    started = start_custom_checkout_method(chatgpt, checkout, custom_method_id, log)
    action = started.get("next_action") if isinstance(started.get("next_action"), dict) else {}
    raw_url = _custom_action_url(started)
    url = raw_url
    if stripe is not None and raw_url:
        provider_config = provider_redirect_config("gcash")
        url = resolve_external_redirect(
            stripe,
            raw_url,
            preferred_hosts=tuple(provider_config["preferred_hosts"]),
            log=log,
        ) or raw_url
    if stage_callback:
        stage_callback("redirect_resolution")
    return {
        "payment_method_id": custom_method_id,
        "stripe_redirect_url": raw_url,
        "provider_url": url,
        "gcash_url": url,
        "payment_method_type": str(
            action.get("paymentMethodType")
            or action.get("payment_method_type")
            or action.get("type")
            or "gcash"
        ),
        "confirm_return_url": str(confirmed.get("confirm_return_url") or ""),
    }


def extract_oaics_provider(
    config: ExtractionConfig,
    chatgpt: Any,
    stripe: Any,
    checkout: CheckoutData,
    billing: dict[str, str],
    log: Any | None,
    *,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, str]:
    payment_method = normalize_payment_method(config.payment_method)
    init_payload = openai_checkout_init_payload(checkout)
    if payment_method == "gcash":
        return extract_oaics_gcash_provider(
            config, chatgpt, checkout, billing, log, stripe=stripe, stage_callback=stage_callback
        )
    ensure_payment_method_offered(init_payload, payment_method, "oaics checkout")
    ctx = stripe_context(init_payload, checkout)
    ctx["runtime_version"] = OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION
    if stage_callback:
        stage_callback("elements_session")
    openai_elements_session(stripe, config, checkout, init_payload, ctx, log)
    if stage_callback:
        stage_callback("taxes")
    openai_checkout_taxes(config, chatgpt, checkout, billing, log)
    from .gopay_core import checkout_payable_amount_with_presence, validate_gopay_amount

    amount_due_minor, _ = checkout_payable_amount_with_presence(checkout)
    validate_gopay_amount(amount_due_minor, promotion_applied=True)
    refreshed = openai_checkout_init_payload(checkout)
    ensure_payment_method_offered(refreshed, payment_method, "oaics taxes refresh")
    ctx["checkout_amount"] = expected_amount(refreshed)
    ctx["currency"] = config_currency(config).lower()
    session = checkout.get("checkout_session")
    if isinstance(session, dict) and session.get("customer"):
        ctx["customer_id"] = str(session["customer"])
    refreshed_elements = openai_elements_session(
        stripe, config, checkout, refreshed, ctx, log, reuse_session=True
    )
    ensure_payment_method_offered(refreshed_elements, payment_method, "oaics refreshed Elements session")
    if stage_callback:
        stage_callback("payment_confirmation")
    confirmation_token = openai_confirmation_token(
        stripe, config, checkout, billing, ctx, payment_method, log
    )
    confirm_payload = openai_checkout_confirm(
        chatgpt, checkout, confirmation_token, payment_method, log
    )
    intent_payload = openai_intent_confirm(
        stripe, checkout, confirmation_token, confirm_payload, ctx, log
    )
    stripe_redirect = extract_redirect_to_url(intent_payload)
    if not stripe_redirect:
        raise ProtocolError(502, "oaics intent response missing redirect_to_url")
    provider_config = provider_redirect_config(payment_method)
    if stage_callback:
        stage_callback("redirect_resolution")
    provider_url = resolve_external_redirect(
        stripe,
        stripe_redirect,
        preferred_hosts=provider_config["preferred_hosts"],
        log=log,
    )
    if payment_method == "paypal" and not is_paypal_ba_approval_url(provider_url):
        raise ProtocolError(502, "PayPal BA 链解析失败：Stripe 中转地址未返回 agreements/approve?ba_token=BA- 链接")
    url = provider_url or stripe_redirect
    return {
        "payment_method_id": str(intent_payload.get("payment_method") or ""),
        "stripe_redirect_url": stripe_redirect,
        "provider_url": url,
        str(provider_config["result_field"]): url,
    }


def payment_method_types(payload: Any) -> list[str]:
    from .gopay_stripe_common import payment_method_types as _payment_method_types

    return _payment_method_types(payload)


def expected_amount(payload: Any) -> str:
    from .gopay_stripe_common import expected_amount as _expected_amount

    return _expected_amount(payload)


def config_currency(config: ExtractionConfig) -> str:
    from .config import country_config

    return country_config(config.country)[1]


def config_locale(config: ExtractionConfig) -> str:
    from .config import country_config

    return country_config(config.country)[2]
