from __future__ import annotations

import base64
import json
import secrets
import re
import time
import uuid
from typing import Any
from urllib.parse import parse_qsl, quote, urljoin, urlsplit, urlencode, urlunsplit

from .gopay_checkout import chatgpt_success_return_url
from .config import (
    DEFAULT_STRIPE_PK,
    DEFAULT_TIMEOUT,
    STRIPE_VERSION_FULL,
)
from .errors import ProtocolError, ProviderRequiresApproval
from .logging_utils import compact_url, emit_log, safe_log_text
from .models import CheckoutData, StripeContext
from .gopay_transport import response_json, stage_http_request

STRIPE_CLIENT_BETAS = (
    "custom_checkout_server_updates_1",
    "custom_checkout_manual_approval_1",
)
STRIPE_ADDITIONAL_ELEMENTS = ("expressCheckout", "payment", "address")

# Stripe.js derives these two confirmation fields in the browser.  The
# derivation is intentionally reproduced here instead of copying values from
# a HAR: `js_checksum` binds the request to the Payment Page id, while
# `rv_timestamp` carries the current Stripe.js build constants.
STRIPE_RV_TIMESTAMP = "2024-01-01 00:00:00 -0000"
STRIPE_RV_BUILD = "b0f5e7abe5ab1a4b215a0dbc9e8f642173efc07e"
STRIPE_RV_SALT = "2ab88fa6a8e98b8aca4b257a12633402a6f9ca3d7f29ec369e620c310e8b2229"


def _protocol_failure(
    status_code,
    detail,
    *,
    retryable=True,
    failure_mode="provider_redirect_error",
):
    error = ProtocolError(status_code, detail)
    error.retryable = bool(retryable)
    error.failure_mode = str(failure_mode)
    return error


def _stripe_xor5(value: str) -> str:
    """Match Stripe.js module 3950's byte-wise XOR obfuscation."""
    return "".join(chr(ord(char) ^ 5) for char in value)


def _stripe_encoded(value: str) -> str:
    """Return Stripe.js's encodeURIComponent(base64(xor5(value + padding)))."""
    padding = 3 - (len(value) % 3)
    padded = value + (" " * padding)
    encoded = base64.b64encode(_stripe_xor5(padded).encode("latin-1")).decode("ascii")
    return quote(encoded, safe="-_.!~*'()")


def _stripe_printable_shift(value: str, amount: int = 11) -> str:
    return "".join(chr((ord(char) - 32 + amount) % 95 + 32) for char in value)


def stripe_js_checksum(payment_page_id: str) -> str:
    """Build the `js_checksum` required by Stripe payment-page confirm."""
    page_id = str(payment_page_id or "").strip()
    if not page_id:
        return ""
    payload = json.dumps({"id": page_id}, separators=(",", ":"), ensure_ascii=False)
    return _stripe_printable_shift(_stripe_encoded(payload))


def stripe_rv_timestamp() -> str:
    """Build the build-bound `rv_timestamp` emitted by Stripe.js."""
    payload = json.dumps(
        {"rvTs": STRIPE_RV_TIMESTAMP, "rv": STRIPE_RV_BUILD, "sv": STRIPE_RV_SALT},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return _stripe_printable_shift(_stripe_encoded(payload))


def stripe_key(checkout: CheckoutData) -> str:
    return str(checkout.get("publishable_key") or "").strip() or DEFAULT_STRIPE_PK


def extract_checkout_totals(payload: Any) -> dict[str, Any]:
    data = payload if isinstance(payload, dict) else {}
    summary = data.get("total_summary") if isinstance(data.get("total_summary"), dict) else {}
    invoice = data.get("invoice") if isinstance(data.get("invoice"), dict) else {}

    def number(value: Any) -> int | None:
        try:
            return int(value) if value not in (None, "") else None
        except Exception:
            return None

    return {
        "due": number(summary.get("due", invoice.get("amount_due"))),
        "subtotal": number(summary.get("subtotal", invoice.get("subtotal"))),
        "total": number(summary.get("total", invoice.get("total"))),
        "currency": str(data.get("currency") or invoice.get("currency") or "").lower(),
    }


def expected_amount(payload: Any) -> str:
    due = extract_checkout_totals(payload).get("due")
    return str(due) if due is not None else "0"




def payment_method_types(payload: Any) -> list[str]:
    methods: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str):
            item = value.strip().lower()
            if item and item not in methods:
                methods.append(item)
        elif isinstance(value, list):
            for item in value:
                add(item)
        elif isinstance(value, dict) and value.get("type"):
            add(value["type"])

    data = payload if isinstance(payload, dict) else {}
    sources = (data, data.get("elements_options") if isinstance(data.get("elements_options"), dict) else {})
    for source in sources:
        for key in (
            "payment_method_types",
            "ordered_payment_method_types",
            "ordered_payment_method_types_and_wallets",
            "payment_method_specs",
        ):
            add(source.get(key))
    return methods


def ensure_payment_method_offered(payload: dict[str, Any], payment_method: str, phase: str) -> None:
    methods = payment_method_types(payload)
    if payment_method not in methods:
        raise ProtocolError(
            409,
            f"{phase} does not offer {payment_method}; methods={','.join(methods) or '?'}",
        )


def stripe_context(
    init_payload: dict[str, Any],
    checkout: CheckoutData,
    stripe_js_id: str = "",
) -> StripeContext:
    payment_page_id = str(init_payload.get("id") or init_payload.get("payment_page_id") or "").strip()
    init_checkout_config_id = str(init_payload.get("config_id") or "")
    browser_locale = str(checkout.get("payment_locale") or "en-GB")
    context = {
        "stripe_js_id": str(stripe_js_id or uuid.uuid4()),
        "elements_session_id": f"elements_session_{uuid.uuid4().hex[:11]}",
        "elements_session_config_id": str(init_payload.get("config_id") or uuid.uuid4()),
        # Stripe keeps two checkout config identities in the final confirm:
        # nested payment-method attribution retains the /init config while
        # top-level attribution follows the latest server-update response.
        "config_id": init_checkout_config_id,
        "checkout_config_id": init_checkout_config_id,
        "init_checksum": str(init_payload.get("init_checksum") or ""),
        "checkout_amount": expected_amount(init_payload),
        "currency": str(init_payload.get("currency") or checkout.get("currency") or "GBP").lower(),
        # /init uses id-ID; Elements and payment-page calls use id.
        "locale": browser_locale.split("-", 1)[0].lower(),
        "browser_timezone": str(checkout.get("browser_timezone") or "Asia/Jakarta"),
        "payment_page_id": payment_page_id,
        "js_checksum": stripe_js_checksum(payment_page_id),
        "rv_timestamp": stripe_rv_timestamp(),
        # Stripe's metrics controller emits UUIDs with a six-character suffix
        # (42 characters total).  Keep them stable across payment-method and
        # confirm requests instead of regenerating 32-character hex strings.
        "guid": f"{uuid.uuid4()}" + secrets.token_hex(3),
        "muid": f"{uuid.uuid4()}" + secrets.token_hex(3),
        "sid": f"{uuid.uuid4()}" + secrets.token_hex(3),
    }
    validate_gopay_fingerprint_params(context)
    return context


def validate_gopay_fingerprint_params(ctx: StripeContext) -> bool:
    """Validate stable Stripe/OpenAI attribution identifiers used by GoPay."""
    for key in ("guid", "muid", "sid"):
        value = str(ctx.get(key) or "")
        if value and not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}[0-9a-f]{6}",
            value,
            re.I,
        ):
            raise ValueError(f"invalid GoPay fingerprint {key}")
    for key in ("oai-device-id", "oai-session-id"):
        value = str(ctx.get(key) or "")
        if value and not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            value,
            re.I,
        ):
            raise ValueError(f"invalid GoPay fingerprint {key}")
    return True


def cs_stripe_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def openai_stripe_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "Sec-Fetch-Site": "same-site",
        "Sec-Fetch-Mode": "cors",
    }


def stripe_elements_options_params() -> dict[str, str]:
    return {
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }


def stripe_additional_elements_params(prefix: str) -> dict[str, str]:
    return {
        f"{prefix}[merchant_integration_additional_elements][{index}]": value
        for index, value in enumerate(STRIPE_ADDITIONAL_ELEMENTS)
    }


def cs_elements_client_params(ctx: StripeContext, *, locale_fallback: str = "en") -> dict[str, str]:
    params = {
        "elements_session_client[client_betas][0]": STRIPE_CLIENT_BETAS[0],
        "elements_session_client[client_betas][1]": STRIPE_CLIENT_BETAS[1],
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": str(ctx.get("elements_session_id") or ""),
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or ""),
        "elements_session_client[locale]": str(ctx.get("locale") or locale_fallback),
        "elements_session_client[is_aggregation_expected]": "false",
    }
    params.update(stripe_elements_options_params())
    return params


def stripe_deferred_intent_params(amount: Any, currency: str, methods: list[str]) -> dict[str, str]:
    normalized_currency = str(currency or "GBP").lower()
    params = {
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": str(amount),
        "deferred_intent[currency]": normalized_currency,
        "deferred_intent[setup_future_usage]": "off_session",
        "currency": normalized_currency,
    }
    for index, method in enumerate(methods):
        params[f"deferred_intent[payment_method_types][{index}]"] = method
    return params


def cs_billing_address(billing: dict[str, str], *, country: str | None = None) -> dict[str, str]:
    address = {
        "line1": billing.get("line1", ""),
        "city": billing.get("city", ""),
        "country": country or billing.get("country", ""),
        "postal_code": billing.get("postal_code", ""),
    }
    if billing.get("state"):
        address["state"] = billing["state"]
    return address


def extract_redirect_to_url(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    next_action = payload.get("next_action")
    if isinstance(next_action, dict):
        redirect = next_action.get("redirect_to_url")
        if isinstance(redirect, dict) and redirect.get("url"):
            return str(redirect["url"]).strip()
    for key in ("setup_intent", "payment_intent", "payment_method_object"):
        nested = payload.get(key)
        if isinstance(nested, dict):
            found = extract_redirect_to_url(nested)
            if found:
                return found
    return ""


def find_setup_intent(payload: Any) -> dict[str, Any] | None:
    if isinstance(payload, dict):
        setup_intent = payload.get("setup_intent")
        if isinstance(setup_intent, dict):
            return setup_intent
        for value in payload.values():
            found = find_setup_intent(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_setup_intent(value)
            if found:
                return found
    return None


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        if isinstance(payload.get("submission_attempt"), dict):
            return payload["submission_attempt"]
        for value in payload.values():
            found = find_submission_attempt(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = find_submission_attempt(value)
            if found:
                return found
    return {}


def stripe_provider_poll(
    stripe: Any,
    checkout: CheckoutData,
    payment_method: str,
    timeout_seconds: float,
    log: Any | None,
    ctx: StripeContext | None = None,
) -> str:
    deadline = time.time() + max(1.0, timeout_seconds)
    last_state = ""
    ctx = ctx or {}
    params = cs_elements_client_params(
        ctx,
        locale_fallback=str(checkout.get("payment_locale") or "en-GB"),
    )
    params.update({"key": stripe_key(checkout), "_stripe_version": STRIPE_VERSION_FULL})
    while time.time() < deadline:
        response = stage_http_request(
            stripe,
            f"Stripe {payment_method} redirect poll",
            "GET",
            f"https://api.stripe.com/v1/payment_pages/{checkout['cs_id']}",
            log,
            params=params,
            headers=cs_stripe_headers(),
            timeout=DEFAULT_TIMEOUT,
        )
        if response.status_code == 200:
            payload = response_json(response, f"Stripe {payment_method} poll")
            redirect = extract_redirect_to_url(payload)
            if redirect:
                return redirect
            setup_intent = find_setup_intent(payload)
            if setup_intent:
                redirect = extract_redirect_to_url(setup_intent)
                if redirect:
                    return redirect
            submission = find_submission_attempt(payload)
            last_state = str(submission.get("state") or "")
            if last_state == "requires_approval":
                raise ProviderRequiresApproval()
            if last_state == "failed":
                raise ProtocolError(502, f"Stripe {payment_method} submission failed: {safe_log_text(submission)}")
        else:
            last_state = f"http {response.status_code}: {safe_log_text(response.text, limit=1200)}"
            emit_log(log, f"Stripe {payment_method} redirect poll error {last_state}")
        time.sleep(1)
    raise ProtocolError(504, f"{payment_method} redirect poll timeout: state={last_state or '?'}")


def stripe_confirm_return_url(checkout: CheckoutData, hosted_url: str) -> str:
    url = str(hosted_url or "").strip()
    if not url:
        url = f"https://checkout.stripe.com/c/pay/{checkout['cs_id']}"
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    if "return_url" in query:
        return url
    query.setdefault("returned_from_redirect", "true")
    query.setdefault("ui_mode", "custom")
    query.setdefault("return_url", chatgpt_success_return_url(checkout))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def resolve_external_redirect(
    session: Any,
    redirect_url: str,
    preferred_hosts: tuple[str, ...] = ("paypal.com",),
    max_hops: int = 5,
    log: Any | None = None,
) -> str:
    current = str(redirect_url or "").strip()
    preferred = tuple(host.lower().lstrip(".") for host in preferred_hosts)
    if not current:
        return ""
    for _ in range(max(1, max_hops)):
        if not current:
            raise _protocol_failure(
                502,
                "provider redirect chain returned an empty URL",
                failure_mode="provider_redirect_empty",
            )
        try:
            parsed = urlsplit(current)
        except ValueError as exc:
            raise _protocol_failure(
                502,
                "provider redirect URL is invalid",
                failure_mode="provider_redirect_invalid",
            ) from exc
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise _protocol_failure(
                502,
                f"provider redirect URL is invalid: {compact_url(current)}",
                failure_mode="provider_redirect_invalid",
            )
        host = (parsed.hostname or "").lower()
        if any(host == item or host.endswith("." + item) for item in preferred):
            return current
        response = None
        # pm-redirects.stripe.com occasionally closes a proxied connection
        # before sending its 302.  Returning that intermediate URL made the
        # UI report a false success, so retry this cheap redirect hop locally.
        for attempt in range(3):
            try:
                response = stage_http_request(
                    session,
                    "Provider redirect hop",
                    "GET",
                    current,
                    log,
                    allow_redirects=False,
                    timeout=DEFAULT_TIMEOUT,
                    headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                        "Referer": "https://js.stripe.com/",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "cross-site",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                break
            except Exception as exc:
                if attempt >= 2:
                    raise _protocol_failure(
                        502,
                        f"provider redirect hop failed: {safe_log_text(exc)}",
                        failure_mode="provider_redirect_transport",
                    ) from exc
                time.sleep(0.35 * (attempt + 1))
        if response is None:
            raise _protocol_failure(
                502,
                "provider redirect hop returned no response",
                failure_mode="provider_redirect_empty_response",
            )
        status_code = int(getattr(response, "status_code", 0))
        if status_code not in (301, 302, 303, 307, 308):
            raise _protocol_failure(
                status_code if status_code >= 400 else 502,
                f"provider redirect stopped at intermediate URL: {compact_url(current)}",
                retryable=status_code != 401,
                failure_mode="provider_redirect_intermediate",
            )
        response_headers = getattr(response, "headers", {}) or {}
        location = str(
            response_headers.get("Location")
            or response_headers.get("location")
            or ""
        ).strip()
        if not location:
            raise _protocol_failure(
                502,
                f"provider redirect response missing Location: {compact_url(current)}",
                failure_mode="provider_redirect_missing_location",
            )
        current = urljoin(current, location)
    try:
        parsed = urlsplit(current)
    except ValueError as exc:
        raise _protocol_failure(
            502,
            "provider redirect chain returned an invalid URL",
            failure_mode="provider_redirect_invalid",
        ) from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme in {"http", "https"} and parsed.netloc and any(
        host == item or host.endswith("." + item) for item in preferred
    ):
        return current
    raise _protocol_failure(
        502,
        f"provider redirect chain exceeded {max(1, max_hops)} hops: {compact_url(current)}",
        failure_mode="provider_redirect_hops_exhausted",
    )
