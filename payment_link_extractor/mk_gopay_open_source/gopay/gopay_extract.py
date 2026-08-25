"""Extract an Indonesian GoPay redirect from a zero-amount Custom Checkout.

The protocol is intentionally narrow and follows the observed web flow:

    ID/IDR checkout with promo -> Stripe init -> Elements session ->
    ID taxes/snapshot -> inline GoPay confirm -> OpenAI approve -> Stripe poll

There is deliberately no ``/backend-api/payments/checkout/update`` call.  A
Checkout whose first Stripe init is not IDR, zero amount, and GoPay-capable is
rejected instead of being mutated into a different regional Checkout.

Sensitive values are never written to the diagnostic log or HTTP dumps.  In
particular, access/session tokens, Stripe keys/secrets, full Checkout IDs,
passive captcha data, redirect results, and full redirect URLs are redacted.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import random
import re
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, MutableMapping
from urllib.parse import quote, unquote, urljoin, urlsplit, urlunsplit

import requests

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from upi import stripe_token

try:
    from curl_cffi import CurlOpt
    from curl_cffi.requests import Session as CurlCffiSession
except ImportError:  # pragma: no cover - requests fallback is exercised instead
    CurlOpt = None
    CurlCffiSession = None


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("GOPAY_DATA_DIR", "").strip() or SCRIPT_DIR).expanduser()
LOG_DIR = DATA_DIR / "logs"
DUMP_DIR = DATA_DIR / "dumps"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DUMP_DIR.mkdir(parents=True, exist_ok=True)

TIMEOUT = 30
CHATGPT_TIMEOUT = 45
COUNTRY = "ID"
CURRENCY = "IDR"
BROWSER_LOCALE = "id-ID"
ELEMENTS_LOCALE = "id"
BROWSER_TIMEZONE = "Asia/Jakarta"
STRIPE_VERSION = (
    "2025-03-31.basil; checkout_server_update_beta=v1; "
    "checkout_manual_approval_preview=v1"
)
DEFAULT_STRIPE_RUNTIME_VERSION = "299e1ea907"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)
DEFAULT_SEC_CH_UA = '"Chromium";v="136", "Not.A/Brand";v="24", "Google Chrome";v="136"'
CHATGPT_CLIENT_VERSION = os.environ.get(
    "GOPAY_CHATGPT_CLIENT_VERSION", "prod-a625aa7240f682c53a83330ea4184be69e64e075"
).strip()
CHATGPT_CLIENT_BUILD_NUMBER = os.environ.get(
    "GOPAY_CHATGPT_CLIENT_BUILD_NUMBER", "9793347"
).strip()

_log_file = LOG_DIR / f"gopay_{time.strftime('%Y%m%d-%H%M%S')}.log"
_dump_counter = 0


class GoPayError(RuntimeError):
    """Base class for GoPay extraction failures."""


class GoPayUnavailableError(GoPayError):
    """The Checkout does not expose GoPay under the required ID/IDR terms."""


class GoPayApproveBlockedError(GoPayError):
    """OpenAI returned HTTP 200 but rejected the Checkout approval."""


class GoPayPollFailedError(GoPayError):
    """Stripe moved the submission into a terminal failed state."""


@dataclass(frozen=True)
class PollDecision:
    state: str
    redirect_url: str = ""
    detail: str = ""


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(
    name: str,
    default: int,
    minimum: int = 1,
    maximum: int = 3600,
) -> int:
    try:
        value = int(os.environ.get(name, "").strip() or default)
    except ValueError:
        value = default
    return min(maximum, max(minimum, value))


def _digest(value: str, length: int = 10) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="ignore")).hexdigest()[:length]


def safe_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "none"
    prefix_match = re.match(r"([A-Za-z]+(?:_[A-Za-z]+)?)_", text)
    prefix = prefix_match.group(1) if prefix_match else "id"
    return f"{prefix}...#{_digest(text)}"


def redact_url(value: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = urlsplit(text)
    except ValueError:
        return f"url...#{_digest(text)}"
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return f"url...#{_digest(text)}"
    path = re.sub(
        r"(?:cs_(?:live|test)_|oaics_)[A-Za-z0-9_-]+",
        lambda match: safe_identifier(match.group(0)),
        parsed.path,
    )
    # Host and route shape are useful; query, fragment, and signed path values are not.
    if len(path) > 120:
        path = path[:80] + f"...#{_digest(path)}"
    suffix = f"?redacted#{_digest(text)}" if parsed.query or parsed.fragment else ""
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", "")) + suffix


_SENSITIVE_KEYS = {
    "authorization",
    "cookie",
    "set-cookie",
    "access_token",
    "accesstoken",
    "session_token",
    "sessiontoken",
    "client_secret",
    "clientsecret",
    "stripe_pk",
    "publishable_key",
    "publishablekey",
    "key",
    "passive_captcha_token",
    "passive_captcha_ekey",
    "js_checksum",
    "rv_timestamp",
    "init_checksum",
    "rqdata",
    "hcaptcha_rqdata",
    "redirectresult",
    "redirect_result",
    "openai-sentinel-token",
    "openai-sentinel-so-token",
    "oai-web-deployment-attestation",
}

_SENSITIVE_KEY_MARKERS = (
    "passive_captcha",
    "hcaptcha",
    "rqdata",
    "client_secret",
    "publishable_key",
    "sentinel-token",
    "deployment-attestation",
    "js_checksum",
    "rv_timestamp",
    "init_checksum",
)

_IDENTIFIER_KEY_MARKERS = (
    "checkout_session_id",
    "checkoutsessionid",
    "elements_session_id",
    "elementssessionid",
    "client_session_id",
    "clientsessionid",
    "stripe_js_id",
    "stripejsid",
    "payment_page_id",
    "paymentpageid",
    "device_id",
    "deviceid",
    "oai-session-id",
)


def mask_email(value: Any) -> str:
    text = str(value or "").strip()
    if "@" not in text:
        return "***"
    local, domain = text.rsplit("@", 1)
    visible = local[:1] if local else ""
    return f"{visible}***@{domain}"


def _redact_string(value: str) -> str:
    text = str(value or "")
    if text.startswith(("http://", "https://")):
        return redact_url(text)
    text = re.sub(r"(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***", text)
    text = re.sub(
        r"(?i)(__Secure-next-auth\.session-token=)[^;\s]+", r"\1***", text
    )
    text = re.sub(r"pk_(?:live|test)_[A-Za-z0-9]+", "pk_***", text)
    text = re.sub(
        r"(?:cs_(?:live|test)_|oaics_)[A-Za-z0-9_-]+",
        lambda match: safe_identifier(match.group(0)),
        text,
    )
    text = re.sub(
        r"(?:cs_attempt_|elements_session_|ppage_)[A-Za-z0-9_-]+",
        lambda match: safe_identifier(match.group(0)),
        text,
    )
    text = re.sub(
        r"(?:pi|seti|src)_[A-Za-z0-9]+_secret_[A-Za-z0-9]+",
        "stripe_client_secret_***",
        text,
    )
    text = re.sub(
        r"(?i)(redirectResult|redirect_result)([=\"':% ]+)[A-Za-z0-9_!%+/.=-]+",
        r"\1\2***",
        text,
    )
    text = re.sub(
        r"(?i)(passive_captcha_(?:token|ekey))([=\"':% ]+)[A-Za-z0-9_!%+/.=-]+",
        r"\1\2***",
        text,
    )
    text = re.sub(
        r"(?i)(?:eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]{20,})?)",
        "jwt_***",
        text,
    )
    text = re.sub(
        r"https?://[^\s\"'<>]+",
        lambda match: redact_url(match.group(0)),
        text,
    )
    return text


def redact_sensitive(value: Any, *, key: str = "") -> Any:
    """Return a JSON-serializable, recursively redacted diagnostic value."""
    normalized_key = re.sub(r"[^a-z0-9_-]+", "", str(key or "").lower())
    if normalized_key == "redirect":
        text = str(value or "")
        if text.startswith(("http://", "https://")):
            return redact_url(text)
        return "***" if value not in (None, "") else ""
    if normalized_key in _SENSITIVE_KEYS or any(
        marker in normalized_key for marker in _SENSITIVE_KEY_MARKERS
    ):
        return "***" if value not in (None, "") else ""
    if any(marker in normalized_key for marker in _IDENTIFIER_KEY_MARKERS):
        return safe_identifier(value) if value not in (None, "") else ""
    if normalized_key == "email" or normalized_key.endswith("email"):
        return mask_email(value)
    if isinstance(value, Mapping):
        return {
            str(item_key): redact_sensitive(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set)):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, str):
        return _redact_string(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _redact_string(str(value))


def _safe_json(value: Any, limit: int = 100000) -> str:
    rendered = json.dumps(redact_sensitive(value), ensure_ascii=False, indent=2)
    if len(rendered) <= limit:
        return rendered
    return json.dumps(
        {
            "truncated": True,
            "sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            "preview": rendered[:limit],
        },
        ensure_ascii=False,
        indent=2,
    )


def log(message: str, prefix: str = "") -> None:
    safe_message = _redact_string(str(message or ""))
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {prefix}{safe_message}"
    print(line, flush=True)
    with open(_log_file, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def log_event(event: str, **fields: Any) -> None:
    safe_fields = redact_sensitive(fields)
    log(f"{event}: {json.dumps(safe_fields, ensure_ascii=False, separators=(',', ':'))}")


def _response_payload(response: Any) -> Any:
    if response is None:
        return None
    try:
        return response.json()
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return str(getattr(response, "text", "") or "")


def dump_http(
    response: Any,
    stage: str,
    request_body: Any = None,
    request_method: str = "",
    request_url: str = "",
    *,
    force: bool = False,
) -> Path | None:
    if not force and not env_bool("GOPAY_DUMP", False):
        return None
    global _dump_counter
    _dump_counter += 1
    safe_stage = re.sub(r"[^A-Za-z0-9_.-]+", "_", stage)
    path = DUMP_DIR / (
        f"{time.strftime('%Y%m%d-%H%M%S')}_{_dump_counter:04d}_{safe_stage}.json"
    )
    payload = {
        "stage": stage,
        "request": {
            "method": request_method,
            "url": redact_url(request_url) if request_url else "",
            "body": redact_sensitive(request_body),
        },
        "response": {
            "status": getattr(response, "status_code", None),
            "url": redact_url(str(getattr(response, "url", "") or ""))
            if getattr(response, "url", "")
            else "",
            "body": redact_sensitive(_response_payload(response)),
        }
        if response is not None
        else None,
    }
    path.write_text(_safe_json(payload), encoding="utf-8")
    return path


def first_value_by_key(payload: Any, key: str) -> Any:
    if isinstance(payload, Mapping):
        if key in payload:
            return payload[key]
        for value in payload.values():
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = first_value_by_key(value, key)
            if found not in (None, "", [], {}):
                return found
    return None


def find_submission_attempt(payload: Any) -> dict[str, Any]:
    value = first_value_by_key(payload, "submission_attempt")
    return dict(value) if isinstance(value, Mapping) else {}


def extract_redirect_url(payload: Any) -> str:
    """Extract a provider redirect only from action/redirect-shaped fields."""
    if isinstance(payload, Mapping):
        next_action = payload.get("next_action")
        if isinstance(next_action, Mapping):
            redirect = next_action.get("redirect_to_url")
            if isinstance(redirect, Mapping):
                url = str(redirect.get("url") or "").strip()
                if url.startswith(("http://", "https://")):
                    return url
            for key in ("url", "redirect_url", "hosted_instructions_url"):
                url = str(next_action.get(key) or "").strip()
                if url.startswith(("http://", "https://")):
                    return url
        for key in (
            "redirect_url",
            "redirect_to_url",
            "authorization_url",
            "authentication_url",
            "hosted_instructions_url",
        ):
            value = payload.get(key)
            if isinstance(value, Mapping):
                value = value.get("url")
            url = str(value or "").strip()
            if url.startswith(("http://", "https://")):
                return url
        for value in payload.values():
            url = extract_redirect_url(value)
            if url:
                return url
    elif isinstance(payload, list):
        for value in payload:
            url = extract_redirect_url(value)
            if url:
                return url
    return ""


def amount_from_payload(payload: Any) -> int | None:
    if isinstance(payload, Mapping):
        options = payload.get("elements_options")
        if isinstance(options, Mapping) and options.get("amount") is not None:
            return int(options["amount"])
        total = payload.get("total_summary")
        if isinstance(total, Mapping) and total.get("due") is not None:
            return int(total["due"])
        invoice = payload.get("invoice")
        if isinstance(invoice, Mapping):
            for key in ("amount_due", "total"):
                if invoice.get(key) is not None:
                    return int(invoice[key])
        for key in ("amount_total", "checkout_amount"):
            if payload.get(key) is not None:
                return int(payload[key])
    return None


def payment_method_types(payload: Any) -> list[str]:
    value = first_value_by_key(payload, "payment_method_types")
    if not isinstance(value, list):
        return []
    return [str(item).strip().lower() for item in value if str(item).strip()]


def payment_page_diagnostics(payload: Any) -> dict[str, Any]:
    """Build a compact status-only summary with no Stripe identifiers."""
    if not isinstance(payload, Mapping):
        return {"payload_type": type(payload).__name__}
    submission = find_submission_attempt(payload)
    next_action = first_value_by_key(payload, "next_action")
    next_action_type = ""
    if isinstance(next_action, Mapping):
        next_action_type = str(next_action.get("type") or "")
    error_summary: dict[str, str] = {}

    def merge_error(value: Any, *, depth: int = 0) -> None:
        if depth > 5 or len(error_summary) >= 12:
            return
        if isinstance(value, Mapping):
            for key in (
                "type",
                "code",
                "decline_code",
                "failure_code",
                "message",
                "failure_message",
                "reason",
                "payment_method_type",
            ):
                item = value.get(key)
                if item not in (None, "", [], {}):
                    error_summary.setdefault(
                        key, _redact_string(str(item))[:240]
                    )
            for key in (
                "error",
                "payment_error",
                "last_payment_error",
                "failure",
                "failure_reason",
                "manual_approval_updates",
            ):
                if key in value:
                    merge_error(value[key], depth=depth + 1)
        elif isinstance(value, list):
            for item in value[:8]:
                merge_error(item, depth=depth + 1)
        elif value not in (None, ""):
            error_summary.setdefault("message", _redact_string(str(value))[:240])

    for error_key in (
        "error",
        "payment_error",
        "last_payment_error",
        "failure_reason",
        "manual_approval_updates",
    ):
        if error_key in submission:
            merge_error(submission[error_key])
        merge_error(first_value_by_key(payload, error_key))
    return {
        "object": str(payload.get("object") or ""),
        "status": str(payload.get("status") or ""),
        "payment_status": str(payload.get("payment_status") or ""),
        "amount": amount_from_payload(payload),
        "currency": str(
            payload.get("currency")
            or (
                payload.get("elements_options", {}).get("currency")
                if isinstance(payload.get("elements_options"), Mapping)
                else ""
            )
            or ""
        ).lower(),
        "payment_method_types": payment_method_types(payload),
        "submission_state": str(submission.get("state") or ""),
        "submission_status": str(submission.get("status") or ""),
        "next_action_type": next_action_type,
        "has_redirect": bool(extract_redirect_url(payload)),
        "error": error_summary,
    }


def classify_approve_response(status_code: int, payload: Any) -> str:
    if status_code >= 400:
        raise GoPayError(f"GoPay approve HTTP {status_code}")
    result = ""
    if isinstance(payload, Mapping):
        result = str(payload.get("result") or "").strip().lower()
    if result == "blocked":
        raise GoPayApproveBlockedError(
            "GoPay approve blocked: HTTP 200 business rejection"
        )
    if result != "approved":
        raise GoPayError(f"GoPay approve unexpected result: {result or 'missing'}")
    return result


def classify_poll_payload(payload: Any) -> PollDecision:
    redirect_url = extract_redirect_url(payload)
    if redirect_url:
        return PollDecision("redirect", redirect_url=redirect_url)
    diagnostics = payment_page_diagnostics(payload)
    submission_state = str(diagnostics.get("submission_state") or "").lower()
    if submission_state == "requires_approval":
        return PollDecision("requires_approval", detail="submission requires approval")
    if submission_state in {"failed", "canceled", "cancelled"}:
        detail = json.dumps(diagnostics.get("error") or {}, ensure_ascii=False)
        return PollDecision("failed", detail=detail or submission_state)
    status = str(diagnostics.get("status") or "").lower()
    payment_status = str(diagnostics.get("payment_status") or "").lower()
    if status in {"complete", "completed"} or payment_status in {"paid", "no_payment_required"}:
        return PollDecision("completed", detail=f"status={status}, payment_status={payment_status}")
    return PollDecision("waiting", detail=f"status={status}, submission={submission_state}")


def stripe_browser_id() -> str:
    return f"{uuid.uuid4()}{uuid.uuid4().hex[:8]}"


def elements_session_params(ctx: Mapping[str, Any]) -> dict[str, str]:
    result = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": str(ctx.get("stripe_js_id") or ""),
        "elements_session_client[locale]": ELEMENTS_LOCALE,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
    }
    if ctx.get("elements_session_id"):
        result["elements_session_client[session_id]"] = str(ctx["elements_session_id"])
    return result


def stripe_return_url(checkout_id: str, processor_entity: str) -> str:
    verify_url = (
        "https://chatgpt.com/checkout/verify?"
        f"stripe_session_id={quote(checkout_id, safe='')}&"
        f"processor_entity={quote(processor_entity, safe='')}&plan_type=plus"
    )
    return (
        f"https://checkout.stripe.com/c/pay/{checkout_id}"
        f"?returned_from_redirect=true&ui_mode=custom&return_url={quote(verify_url, safe='')}"
    )


def build_gopay_confirm_body(
    *,
    checkout_id: str,
    stripe_pk: str,
    init_payload: Mapping[str, Any],
    ctx: Mapping[str, Any],
    billing: Mapping[str, str],
    processor_entity: str = "openai_llc",
    dynamic_token_fields: Mapping[str, str] | None = None,
    token_config: stripe_token.StripeTokenConfig | None = None,
    passive_captcha_token: str = "",
    passive_captcha_ekey: str = "",
) -> dict[str, str]:
    """Build the HAR-aligned inline GoPay confirm form without side effects."""
    amount = amount_from_payload(init_payload)
    if amount is None:
        amount = int(ctx.get("checkout_amount") or 0)
    if amount != 0:
        raise GoPayUnavailableError(
            f"GoPay checkout expected zero amount, got {amount}"
        )
    runtime = str(ctx.get("runtime_version") or DEFAULT_STRIPE_RUNTIME_VERSION)
    body: dict[str, str] = {
        "expected_amount": "0",
        "expected_payment_method_type": "gopay",
        "return_url": stripe_return_url(checkout_id, processor_entity),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION),
        "guid": str(ctx.get("guid") or stripe_browser_id()),
        "muid": str(ctx.get("muid") or stripe_browser_id()),
        "sid": str(ctx.get("sid") or stripe_browser_id()),
        "key": stripe_pk,
        "version": runtime,
        "init_checksum": str(init_payload.get("init_checksum") or ctx.get("init_checksum") or ""),
        "link_brand": "link",
        "passive_captcha_token": str(passive_captcha_token or ""),
        "passive_captcha_ekey": str(passive_captcha_ekey or ""),
        "client_attribution_metadata[client_session_id]": str(ctx.get("stripe_js_id") or ""),
        "client_attribution_metadata[checkout_session_id]": checkout_id,
        "client_attribution_metadata[checkout_config_id]": str(
            ctx.get("payment_element_config_id") or ""
        ),
        "client_attribution_metadata[elements_session_id]": str(ctx.get("elements_session_id") or ""),
        "client_attribution_metadata[elements_session_config_id]": str(ctx.get("elements_session_config_id") or ""),
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "merchant_specified",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "expressCheckout",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][2]": "address",
        "payment_method_data[type]": "gopay",
        "payment_method_data[billing_details][name]": str(billing.get("name") or ""),
        "payment_method_data[billing_details][email]": str(billing.get("email") or ""),
        "payment_method_data[billing_details][address][line1]": str(billing.get("line1") or ""),
        "payment_method_data[billing_details][address][city]": str(billing.get("city") or ""),
        "payment_method_data[billing_details][address][postal_code]": str(billing.get("postal_code") or ""),
        "payment_method_data[billing_details][address][state]": str(billing.get("state") or ""),
        "payment_method_data[billing_details][address][country]": COUNTRY,
        "payment_method_data[payment_user_agent]": (
            f"stripe.js/{runtime}; stripe-js-v3/{runtime}; payment-element; deferred-intent"
        ),
        "payment_method_data[referrer]": "https://chatgpt.com",
        "payment_method_data[time_on_page]": str(random.randint(18000, 125000)),
        "payment_method_data[client_attribution_metadata][client_session_id]": str(ctx.get("stripe_js_id") or ""),
        "payment_method_data[client_attribution_metadata][checkout_session_id]": checkout_id,
        "payment_method_data[client_attribution_metadata][merchant_integration_source]": "elements",
        "payment_method_data[client_attribution_metadata][merchant_integration_subtype]": "payment-element",
        "payment_method_data[client_attribution_metadata][merchant_integration_version]": "2021",
        "payment_method_data[client_attribution_metadata][payment_intent_creation_flow]": "deferred",
        "payment_method_data[client_attribution_metadata][payment_method_selection_flow]": "merchant_specified",
        "payment_method_data[client_attribution_metadata][elements_session_id]": str(ctx.get("elements_session_id") or ""),
        "payment_method_data[client_attribution_metadata][elements_session_config_id]": str(ctx.get("elements_session_config_id") or ""),
        "payment_method_data[client_attribution_metadata][checkout_config_id]": str(
            ctx.get("config_id") or ""
        ),
        "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][0]": "expressCheckout",
        "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][1]": "payment",
        "payment_method_data[client_attribution_metadata][merchant_integration_additional_elements][2]": "address",
    }
    body.update(elements_session_params(ctx))
    token_fields = dict(dynamic_token_fields or {})
    if not token_fields and token_config is not None:
        token_fields = stripe_token.build_token_fields(
            ppage_id=str(init_payload.get("id") or ""), config=token_config
        )
    body.update({str(key): str(value) for key, value in token_fields.items()})
    return body


_PROXY_SELECTOR_RE = re.compile(
    r"(?i)(?P<name>country|region|zone)(?P<separator>[-_=])(?P<value>[a-z]{2}(?:,[a-z]{2})*)(?![a-z])"
)
_PROXY_SCHEMES = ("socks5h", "socks5", "http", "https")


def proxy_scheme_mode() -> str:
    value = os.environ.get("GOPAY_PROXY_DEFAULT_SCHEME", "auto").strip().lower()
    value = value[:-3] if value.endswith("://") else value
    aliases = {"socket": "socks5", "socket5": "socks5", "socks": "socks5"}
    value = aliases.get(value, value)
    return value if value in {*_PROXY_SCHEMES, "auto"} else "auto"


def default_proxy_scheme() -> str:
    value = proxy_scheme_mode()
    return value if value != "auto" else "http"


def host_port_user_password_proxy_url(proxy: str, scheme: str = "") -> str:
    """Convert the project's common ``host:port:user:password`` seed form."""
    if "://" in proxy or "@" in proxy:
        return ""
    parts = proxy.split(":", 3)
    if len(parts) != 4:
        return ""
    host, port_text, username, password = (part.strip() for part in parts)
    if (
        not host
        or not username
        or not password
        or any(char in host for char in "/[]")
        or not port_text.isdigit()
    ):
        return ""
    port = int(port_text)
    if not 1 <= port <= 65535:
        return ""
    auth = f"{quote(username, safe='-._~')}:{quote(password, safe='-._~')}"
    return urlunsplit((scheme or default_proxy_scheme(), f"{auth}@{host}:{port}", "", "", ""))


def normalize_proxy_url(proxy: str, scheme: str = "") -> str:
    text = str(proxy or "").strip()
    if not text:
        return ""
    if "://" not in text:
        selected_scheme = scheme or default_proxy_scheme()
        text = host_port_user_password_proxy_url(text, selected_scheme) or f"{selected_scheme}://{text}"
    parsed = urlsplit(text)
    if parsed.username is None:
        return text
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host += f":{parsed.port}"
    username = quote(unquote(parsed.username or ""), safe="-._~")
    password = quote(unquote(parsed.password or ""), safe="-._~")
    auth = username + (f":{password}" if parsed.password is not None else "")
    return urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))


def proxy_url_candidates(proxy: str) -> list[str]:
    text = str(proxy or "").strip()
    if not text:
        return []
    if "://" in text:
        return [normalize_proxy_url(text)]
    mode = proxy_scheme_mode()
    schemes = _PROXY_SCHEMES if mode == "auto" else (mode,)
    return [normalize_proxy_url(text, scheme) for scheme in schemes]


def _probe_proxy_candidate(proxy: str) -> tuple[bool, int, str]:
    session = new_session(proxy)
    try:
        response = session.get(
            os.environ.get("GOPAY_PROXY_PROBE_URL", "https://chatgpt.com/").strip()
            or "https://chatgpt.com/",
            headers={"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,*/*"},
            timeout=env_int("GOPAY_PROXY_PROBE_TIMEOUT", 10, 2, 30),
            allow_redirects=False,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        return status > 0, status, ""
    except Exception as exc:
        return False, 0, type(exc).__name__
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            close()


def resolve_proxy_url(proxy: str) -> str:
    candidates = proxy_url_candidates(proxy)
    if not candidates:
        raise GoPayError("GoPay proxy seed is empty")
    if len(candidates) == 1:
        return candidates[0]

    with ThreadPoolExecutor(max_workers=len(candidates), thread_name_prefix="proxy-probe") as executor:
        outcomes = list(executor.map(_probe_proxy_candidate, candidates))

    for candidate, (available, status, _error) in zip(candidates, outcomes):
        if available:
            scheme = urlsplit(candidate).scheme
            log_event("proxy.scheme_selected", mode="auto", scheme=scheme, http_status=status)
            return candidate

    summary = ",".join(
        f"{urlsplit(candidate).scheme}={error or 'unavailable'}"
        for candidate, (_available, _status, error) in zip(candidates, outcomes)
    )
    log_event("proxy.scheme_failed", mode="auto", outcomes=summary)
    raise GoPayError(f"proxy protocol auto-detection failed: {summary}")


def proxy_label(proxy: str) -> str:
    value = normalize_proxy_url(proxy)
    return f"proxy#{_digest(value)}" if value else "direct"


def proxy_for_indonesia(proxy_seed: str) -> str:
    proxy = normalize_proxy_url(proxy_seed)
    if not proxy:
        if env_bool("GOPAY_ALLOW_DIRECT", False):
            return ""
        raise GoPayError("GoPay proxy seed is empty")
    parsed = urlsplit(proxy)
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    replacements = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal replacements
        replacements += 1
        current = match.group("value")
        target = "ID" if current.isupper() else "id"
        return f"{match.group('name')}{match.group('separator')}{target}"

    username = _PROXY_SELECTOR_RE.sub(replace, username)
    password = _PROXY_SELECTOR_RE.sub(replace, password)
    if replacements == 0:
        # A seed explicitly labelled ID is already suitable even if the
        # provider uses a nonstandard selector spelling.
        if re.search(r"(?i)(?:^|[-_=])id(?:[-_=@]|$)", username):
            return proxy
        raise GoPayError(
            f"proxy has no rewritable country/region/zone selector: {proxy_label(proxy)}"
        )
    hostname = parsed.hostname or ""
    host = f"[{hostname}]" if ":" in hostname and not hostname.startswith("[") else hostname
    if parsed.port:
        host += f":{parsed.port}"
    auth = quote(username, safe="-._~")
    if parsed.password is not None:
        auth += f":{quote(password, safe='-._~')}"
    return urlunsplit((parsed.scheme, f"{auth}@{host}", parsed.path, parsed.query, parsed.fragment))


def set_proxy(session: Any, proxy: str) -> None:
    normalized = normalize_proxy_url(proxy)
    if hasattr(session, "trust_env"):
        session.trust_env = False
    session.proxies = {"http": normalized, "https": normalized} if normalized else {}


def new_session(proxy: str = "") -> Any:
    pre_proxy = os.environ.get("GOPAY_PRE_PROXY", "").strip()
    if CurlCffiSession is not None:
        kwargs: dict[str, Any] = {
            "impersonate": os.environ.get("GOPAY_TLS_IMPERSONATE", "chrome136").strip()
            or "chrome136"
        }
        if pre_proxy:
            if CurlOpt is None:
                raise GoPayError("GOPAY_PRE_PROXY requires curl_cffi")
            kwargs["curl_options"] = {CurlOpt.PRE_PROXY: normalize_proxy_url(pre_proxy)}
        session = CurlCffiSession(**kwargs)
    else:
        if pre_proxy:
            raise GoPayError("GOPAY_PRE_PROXY requires curl_cffi")
        session = requests.Session()
    set_proxy(session, proxy)
    return session


def accept_language() -> str:
    return "id-ID,id;q=0.9,en-US;q=0.8,en;q=0.7"


def stripe_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Accept-Language": accept_language(),
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "User-Agent": DEFAULT_USER_AGENT,
    }


def account_id_from_token(access_token: str) -> str:
    parts = str(access_token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        import base64

        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError):
        return ""
    auth = payload.get("https://api.openai.com/auth")
    if not isinstance(auth, Mapping):
        return ""
    return str(auth.get("chatgpt_account_id") or "")


def remember_response_context(session: Any, response: Any = None, document: str = "") -> None:
    headers = getattr(response, "headers", {}) or {}
    update_value = ""
    for name, value in headers.items():
        lowered = str(name).lower()
        if lowered == "x-oai-is-update":
            update_value = str(value or "")
        elif lowered == "oai-web-deployment-attestation" and value:
            setattr(session, "_gopay_attestation", str(value))
    update_match = re.fullmatch(
        r"ois1\.[A-Za-z0-9_-]+\.([A-Za-z0-9_-]{16})\.[A-Za-z0-9_-]+",
        update_value,
    )
    if update_match:
        setattr(session, "_gopay_observation_nonce", update_match.group(1))
    if document:
        match = re.search(
            r"[\"'](?:webDeploymentAttestation|web_deployment_attestation|"
            r"oai-web-deployment-attestation)[\"']\s*:\s*[\"']([^\"']+)[\"']",
            document,
        )
        if match:
            setattr(session, "_gopay_attestation", match.group(1))


def client_observation(session: Any) -> str:
    nonce = str(getattr(session, "_gopay_observation_nonce", "") or "")
    return f"v1.r.p.{nonce}" if re.fullmatch(r"[A-Za-z0-9_-]{16}", nonce) else "v1.r.m"


def deployment_attestation(session: Any) -> str:
    return str(
        getattr(session, "_gopay_attestation", "")
        or os.environ.get("GOPAY_WEB_DEPLOYMENT_ATTESTATION", "")
        or os.environ.get("OAI_WEB_DEPLOYMENT_ATTESTATION", "")
        or ""
    ).strip()


def build_chatgpt_session(
    access_token: str,
    session_token: str,
    proxy: str,
    device_id: str,
    oai_session_id: str,
) -> Any:
    session = new_session(proxy)
    cookie_jar = getattr(session, "cookies", None)
    cookie_setter = getattr(cookie_jar, "set", None)
    if not callable(cookie_setter):
        raise GoPayError("HTTP session does not expose a mutable Cookie Jar")
    cookie_setter("oai-did", device_id, domain=".chatgpt.com", path="/", secure=True)
    if session_token:
        cookie_setter(
            "__Secure-next-auth.session-token",
            session_token,
            domain=".chatgpt.com",
            path="/",
            secure=True,
        )
    session.headers.update(
        {
            "User-Agent": DEFAULT_USER_AGENT,
            "Accept": "*/*",
            "Accept-Language": accept_language(),
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Origin": "https://chatgpt.com",
            "Referer": "https://chatgpt.com/",
            "oai-device-id": device_id,
            "oai-language": BROWSER_LOCALE,
            "oai-session-id": oai_session_id,
            "oai-client-version": CHATGPT_CLIENT_VERSION,
            "oai-client-build-number": CHATGPT_CLIENT_BUILD_NUMBER,
            "sec-ch-ua": DEFAULT_SEC_CH_UA,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
    )
    account_id = account_id_from_token(access_token)
    if account_id:
        session.headers["chatgpt-account-id"] = account_id
    return session


def load_bootstrap_context(session: Any, checkout_url: str = "") -> None:
    candidates = [checkout_url, "https://chatgpt.com/"] if checkout_url else ["https://chatgpt.com/"]
    for url in candidates:
        saved: dict[str, Any] = {}
        for key in (
            "Authorization",
            "Content-Type",
            "Origin",
            "oai-device-id",
            "oai-language",
            "oai-session-id",
            "oai-client-version",
            "oai-client-build-number",
            "chatgpt-account-id",
            "x-openai-target-path",
            "x-openai-target-route",
            "x-oai-is-client-observation",
            "oai-web-deployment-attestation",
        ):
            if key in session.headers:
                saved[key] = session.headers.pop(key)
        try:
            response = session.get(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Referer": "https://chatgpt.com/",
                    "sec-fetch-dest": "document",
                    "sec-fetch-mode": "navigate",
                    "upgrade-insecure-requests": "1",
                },
                timeout=CHATGPT_TIMEOUT,
                allow_redirects=True,
            )
            remember_response_context(session, response, str(response.text or ""))
            log_event(
                "bootstrap.context",
                status=response.status_code,
                source="checkout" if url == checkout_url else "root",
                attestation=bool(deployment_attestation(session)),
                observation=client_observation(session),
            )
            if deployment_attestation(session):
                return
        except Exception as exc:
            log_event("bootstrap.error", error_type=type(exc).__name__, message=str(exc)[:180])
        finally:
            session.headers.update(saved)


def warmup_checkout_context(session: Any) -> None:
    """Warm CSRF/cookies before creating the checkout-bound Sentinel token."""
    try:
        response = session.get(
            "https://chatgpt.com/api/auth/csrf",
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://chatgpt.com/",
            },
            timeout=CHATGPT_TIMEOUT,
        )
        remember_response_context(session, response, str(response.text or ""))
        log_event(
            "checkout.warmup",
            http_status=response.status_code,
            observation=client_observation(session),
            attestation=bool(deployment_attestation(session)),
        )
    except Exception as exc:
        log_event(
            "checkout.warmup_error",
            error_type=type(exc).__name__,
            message=str(exc)[:180],
        )


def chatgpt_integrity_headers(session: Any) -> dict[str, str]:
    headers = {"x-oai-is-client-observation": client_observation(session)}
    attestation = deployment_attestation(session)
    if attestation:
        headers["oai-web-deployment-attestation"] = attestation
    return headers


def checkout_page_url(checkout: Mapping[str, Any]) -> str:
    return (
        f"https://chatgpt.com/checkout/{checkout.get('processor_entity') or 'openai_llc'}/"
        f"{checkout['checkout_id']}"
    )


def require_stripe_checkout_id(value: str) -> str:
    checkout_id = str(value or "").strip()
    if checkout_id.startswith("oaics_"):
        raise GoPayUnavailableError("GoPay rejects oaics_ checkout")
    if not checkout_id.startswith("cs_"):
        raise GoPayError("GoPay checkout response missing Stripe Custom Checkout ID")
    return checkout_id


def create_checkout(session: Any, device_id: str) -> dict[str, Any]:
    promo_id = os.environ.get("GOPAY_PROMO_ID", "plus-1-month-free").strip()
    body: dict[str, Any] = {
        "entry_point": os.environ.get("GOPAY_ENTRY_POINT", "all_plans_pricing_modal"),
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": COUNTRY, "currency": CURRENCY},
        "checkout_ui_mode": "custom",
    }
    if promo_id:
        body["promo_campaign"] = {
            "promo_campaign_id": promo_id,
            "is_coupon_from_query_param": False,
        }
    url = "https://chatgpt.com/backend-api/payments/checkout"
    warmup_checkout_context(session)
    load_bootstrap_context(session)
    sentinel_headers: dict[str, str] = {}
    if env_bool("GOPAY_CHECKOUT_SENTINEL", True):
        sentinel_headers = build_checkout_sentinel_headers(
            session, device_id, flow="chatgpt_checkout"
        )
    integrity_headers = chatgpt_integrity_headers(session)
    log_event(
        "checkout.integrity",
        sentinel=bool(sentinel_headers.get("OpenAI-Sentinel-Token")),
        sentinel_len=len(str(sentinel_headers.get("OpenAI-Sentinel-Token") or "")),
        telemetry=bool(sentinel_headers.get("OAI-Telemetry")),
        telemetry_len=len(str(sentinel_headers.get("OAI-Telemetry") or "")),
        session_observer=bool(sentinel_headers.get("OpenAI-Sentinel-So-Token")),
        session_observer_len=len(
            str(sentinel_headers.get("OpenAI-Sentinel-So-Token") or "")
        ),
        attestation=bool(integrity_headers.get("oai-web-deployment-attestation")),
        attestation_len=len(
            str(integrity_headers.get("oai-web-deployment-attestation") or "")
        ),
        observation=integrity_headers.get("x-oai-is-client-observation", ""),
    )
    response = session.post(
        url,
        json=body,
        headers={
            "Referer": "https://chatgpt.com/",
            "x-openai-target-path": "/backend-api/payments/checkout",
            "x-openai-target-route": "/backend-api/payments/checkout",
            **integrity_headers,
            **sentinel_headers,
        },
        timeout=CHATGPT_TIMEOUT,
    )
    remember_response_context(session, response)
    dump_path = dump_http(
        response, "checkout", body, "POST", url, force=response.status_code >= 400
    )
    if response.status_code >= 400:
        raise GoPayError(f"GoPay checkout HTTP {response.status_code}")
    payload = response.json() or {}
    checkout_id = require_stripe_checkout_id(
        str(
            payload.get("checkout_session_id")
            or payload.get("session_id")
            or payload.get("id")
            or ""
        )
    )
    raw_pk = str(
        payload.get("stripe_publishable_key")
        or payload.get("publishable_key")
        or payload.get("publishableKey")
        or payload.get("key")
        or os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
    )
    match = re.search(r"pk_live_[A-Za-z0-9]+", raw_pk)
    stripe_pk = match.group(0) if match else ""
    if not stripe_pk:
        raise GoPayError("GoPay checkout response missing Stripe publishable key")
    checkout = {
        "checkout_id": checkout_id,
        "stripe_pk": stripe_pk,
        "processor_entity": str(payload.get("processor_entity") or "openai_llc"),
        "currency": CURRENCY,
        "country": COUNTRY,
    }
    log_event(
        "checkout.created",
        checkout=safe_identifier(checkout_id),
        country=COUNTRY,
        currency=CURRENCY,
        promo=bool(promo_id),
        dump=dump_path.name if dump_path else "disabled",
    )
    return checkout


def stripe_init(session: Any, checkout: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    checkout_id = str(checkout["checkout_id"])
    stripe_pk = str(checkout["stripe_pk"])
    stripe_js_id = str(uuid.uuid4())
    body = {
        "browser_locale": BROWSER_LOCALE,
        "browser_timezone": BROWSER_TIMEZONE,
        "redirect_type": "url",
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": BROWSER_LOCALE,
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_options_client[saved_payment_method][enable_save]": "auto",
        "elements_options_client[saved_payment_method][enable_redisplay]": "auto",
        "key": stripe_pk,
        "_stripe_version": STRIPE_VERSION,
    }
    url = f"https://api.stripe.com/v1/payment_pages/{checkout_id}/init"
    response = session.post(url, data=body, headers=stripe_headers(), timeout=TIMEOUT)
    dump_path = dump_http(
        response, "stripe_init", body, "POST", url, force=response.status_code >= 400
    )
    if response.status_code >= 400:
        raise GoPayError(f"GoPay Stripe init HTTP {response.status_code}")
    payload = response.json() or {}
    amount = amount_from_payload(payload)
    currency = str(
        payload.get("currency")
        or (
            payload.get("elements_options", {}).get("currency")
            if isinstance(payload.get("elements_options"), Mapping)
            else ""
        )
        or ""
    ).lower()
    methods = payment_method_types(payload)
    log_event(
        "stripe.init",
        checkout=safe_identifier(checkout_id),
        amount=amount,
        currency=currency,
        methods=methods,
        dump=dump_path.name if dump_path else "disabled",
    )
    if amount != 0:
        raise GoPayUnavailableError(f"GoPay checkout expected zero amount, got {amount}")
    if currency != "idr":
        raise GoPayUnavailableError(f"GoPay checkout expected IDR, got {currency or 'missing'}")
    if "gopay" not in methods:
        raise GoPayUnavailableError(
            f"GoPay is not available; payment_method_types={methods}"
        )
    ctx = {
        "stripe_js_id": stripe_js_id,
        "guid": stripe_browser_id(),
        "muid": stripe_browser_id(),
        "sid": stripe_browser_id(),
        "elements_session_id": "",
        "elements_session_config_id": "",
        "payment_element_config_id": str(uuid.uuid4()),
        "config_id": str(payload.get("config_id") or ""),
        "init_checksum": str(payload.get("init_checksum") or ""),
        "checkout_amount": 0,
        "runtime_version": DEFAULT_STRIPE_RUNTIME_VERSION,
        "stripe_version": STRIPE_VERSION,
    }
    return payload, ctx


def stripe_elements_session(
    session: Any,
    checkout: Mapping[str, Any],
    ctx: MutableMapping[str, Any],
) -> dict[str, Any]:
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": "0",
        "deferred_intent[currency]": "idr",
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "deferred_intent[payment_method_types][1]": "link",
        "deferred_intent[payment_method_types][2]": "gopay",
        "currency": "idr",
        "key": str(checkout["stripe_pk"]),
        "_stripe_version": STRIPE_VERSION,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": str(ctx["stripe_js_id"]),
        "locale": ELEMENTS_LOCALE,
        "type": "deferred_intent",
        "checkout_session_id": str(checkout["checkout_id"]),
    }
    url = "https://api.stripe.com/v1/elements/sessions"
    response = session.get(url, params=params, headers=stripe_headers(), timeout=TIMEOUT)
    dump_path = dump_http(
        response,
        "stripe_elements_session",
        params,
        "GET",
        url,
        force=response.status_code >= 400,
    )
    if response.status_code >= 400:
        raise GoPayError(f"GoPay Elements session HTTP {response.status_code}")
    payload = response.json() or {}
    session_id = str(payload.get("session_id") or "")
    if not session_id:
        raise GoPayError("GoPay Elements response missing session_id")
    ctx["elements_session_id"] = session_id
    ctx["elements_session_config_id"] = str(payload.get("config_id") or "")
    log_event(
        "stripe.elements",
        session=safe_identifier(session_id),
        methods=payment_method_types(payload),
        dump=dump_path.name if dump_path else "disabled",
    )
    return payload


_BILLING_PROFILES = (
    ("Cahaya Dian Tanudjaja", "Jl Johar 10 C", "Surabaya", "60174", "Jawa Timur"),
    ("Putri Ayu Santoso", "Jl Merdeka 18", "Bandung", "40111", "Jawa Barat"),
    ("Rizky Aditya Pratama", "Jl Sudirman 25", "Jakarta", "10220", "DKI Jakarta"),
    ("Dewi Lestari Wijaya", "Jl Diponegoro 8", "Semarang", "50241", "Jawa Tengah"),
)


def account_email_from_token(access_token: str) -> str:
    parts = str(access_token or "").split(".")
    if len(parts) < 2:
        return ""
    try:
        import base64

        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    profile = payload.get("https://api.openai.com/profile")
    if not isinstance(profile, Mapping):
        profile = {}
    email = str(profile.get("email") or payload.get("email") or "").strip()
    return email if re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) else ""


def indonesia_billing_profile(account_email: str = "") -> dict[str, str]:
    name, line1, city, postal_code, state = random.choice(_BILLING_PROFILES)
    local = re.sub(r"[^a-z]", "", name.lower())[:14] + str(random.randint(1000, 99999))
    profile = {
        "name": name,
        "email": str(account_email or "").strip() or f"{local}@gmail.com",
        "line1": line1,
        "city": city,
        "postal_code": postal_code,
        "state": state,
        "country": COUNTRY,
    }
    for key in tuple(profile):
        env_name = f"GOPAY_{key.upper()}"
        if os.environ.get(env_name, "").strip():
            profile[key] = os.environ[env_name].strip()
    profile["country"] = COUNTRY
    return profile


def sync_checkout_taxes(
    session: Any,
    checkout: Mapping[str, Any],
    billing: Mapping[str, str],
) -> None:
    if not env_bool("GOPAY_CHECKOUT_TAXES", True):
        log_event("checkout.taxes.skipped", configured=False)
        return
    body = {
        "checkout_session_id": str(checkout["checkout_id"]),
        "checkout_email": str(billing["email"]),
        "billing_country": COUNTRY,
        "billing_name": str(billing["name"]),
        "currency": "idr",
        "processor_entity": str(checkout.get("processor_entity") or "openai_llc"),
        "billing_address": {
            "line1": str(billing["line1"]),
            "city": str(billing["city"]),
            "country": COUNTRY,
            "postal_code": str(billing["postal_code"]),
            "state": str(billing["state"]),
        },
    }
    url = "https://chatgpt.com/backend-api/payments/checkout/taxes"
    response = session.post(
        url,
        json=body,
        headers={
            "Referer": checkout_page_url(checkout),
            "x-openai-target-path": "/backend-api/payments/checkout/taxes",
            "x-openai-target-route": "/backend-api/payments/checkout/taxes",
            **chatgpt_integrity_headers(session),
        },
        timeout=CHATGPT_TIMEOUT,
    )
    remember_response_context(session, response)
    dump_path = dump_http(
        response, "checkout_taxes", body, "POST", url, force=response.status_code >= 400
    )
    if response.status_code >= 400:
        raise GoPayError(f"GoPay checkout taxes HTTP {response.status_code}")
    payload = _response_payload(response)
    amount = amount_from_payload(payload)
    if amount not in (None, 0):
        raise GoPayUnavailableError(
            f"GoPay checkout taxes expected zero amount, got {amount}"
        )
    log_event(
        "checkout.taxes",
        status=response.status_code,
        amount=amount,
        attestation=bool(deployment_attestation(session)),
        observation=client_observation(session),
        dump=dump_path.name if dump_path else "disabled",
    )


def sync_checkout_snapshot(
    session: Any,
    checkout: Mapping[str, Any],
    billing: Mapping[str, str],
) -> None:
    if not env_bool("GOPAY_CHECKOUT_SNAPSHOT", True):
        log_event("checkout.snapshot.skipped", configured=False)
        return
    body = {
        "snapshot": {
            "billing_address": {
                "name": str(billing["name"]),
                "address": {
                    "line1": str(billing["line1"]),
                    "city": str(billing["city"]),
                    "country": COUNTRY,
                    "postal_code": str(billing["postal_code"]),
                    "state": str(billing["state"]),
                },
            }
        }
    }
    url = "https://chatgpt.com/backend-api/payments/checkout/snapshot"
    response = session.post(
        url,
        json=body,
        headers={
            "Referer": checkout_page_url(checkout),
            "x-openai-target-path": "/backend-api/payments/checkout/snapshot",
            "x-openai-target-route": "/backend-api/payments/checkout/snapshot",
            **chatgpt_integrity_headers(session),
        },
        timeout=CHATGPT_TIMEOUT,
    )
    remember_response_context(session, response)
    dump_path = dump_http(
        response,
        "checkout_snapshot",
        body,
        "POST",
        url,
        force=response.status_code >= 400,
    )
    if response.status_code >= 400:
        raise GoPayError(f"GoPay checkout snapshot HTTP {response.status_code}")
    log_event(
        "checkout.snapshot",
        status=response.status_code,
        attestation=bool(deployment_attestation(session)),
        observation=client_observation(session),
        dump=dump_path.name if dump_path else "disabled",
    )


def stripe_confirm_gopay(
    session: Any,
    checkout: Mapping[str, Any],
    init_payload: Mapping[str, Any],
    ctx: MutableMapping[str, Any],
    billing: Mapping[str, str],
) -> dict[str, Any]:
    token_config = stripe_token.extract_config_live(
        session,
        log=lambda message: log_event("stripe.dynamic_token", message=str(message)[:180]),
        user_agent=DEFAULT_USER_AGENT,
        accept_language=accept_language(),
    )
    ctx["runtime_version"] = token_config.runtime_version
    captcha_token = os.environ.get("GOPAY_PASSIVE_CAPTCHA_TOKEN", "").strip()
    captcha_ekey = os.environ.get("GOPAY_PASSIVE_CAPTCHA_EKEY", "").strip()
    body = build_gopay_confirm_body(
        checkout_id=str(checkout["checkout_id"]),
        stripe_pk=str(checkout["stripe_pk"]),
        init_payload=init_payload,
        ctx=ctx,
        billing=billing,
        processor_entity=str(checkout.get("processor_entity") or "openai_llc"),
        token_config=token_config,
        passive_captcha_token=captcha_token,
        passive_captcha_ekey=captcha_ekey,
    )
    dynamic_tokens = bool(body.get("js_checksum") and body.get("rv_timestamp"))
    log_event(
        "stripe.confirm.before",
        checkout=safe_identifier(checkout["checkout_id"]),
        expected_amount=body.get("expected_amount"),
        expected_method=body.get("expected_payment_method_type"),
        dynamic_tokens=dynamic_tokens,
        passive_captcha=bool(captcha_token),
        passive_captcha_ekey=bool(captcha_ekey),
        elements_session=safe_identifier(ctx.get("elements_session_id")),
    )
    url = f"https://api.stripe.com/v1/payment_pages/{checkout['checkout_id']}/confirm"
    started = time.monotonic()
    response = session.post(url, data=body, headers=stripe_headers(), timeout=TIMEOUT)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    dump_path = dump_http(response, "gopay_confirm", body, "POST", url, force=True)
    if response.status_code >= 400:
        log_event(
            "stripe.confirm.after",
            elapsed_ms=elapsed_ms,
            http_status=response.status_code,
            dynamic_tokens=dynamic_tokens,
            passive_captcha=bool(captcha_token),
            dump=dump_path.name if dump_path else "missing",
        )
        raise GoPayError(f"GoPay Stripe confirm HTTP {response.status_code}")
    payload = response.json() or {}
    diagnostics = payment_page_diagnostics(payload)
    log_event(
        "stripe.confirm.after",
        elapsed_ms=elapsed_ms,
        http_status=response.status_code,
        dynamic_tokens=dynamic_tokens,
        passive_captcha=bool(captcha_token),
        diagnostics=diagnostics,
        dump=dump_path.name if dump_path else "missing",
    )
    return payload


def _sentinel_profile() -> Any:
    return SimpleNamespace(
        user_agent=DEFAULT_USER_AGENT,
        sec_ch_ua=DEFAULT_SEC_CH_UA,
        sec_ch_ua_platform='"Windows"',
        resolution=os.environ.get("GOPAY_BROWSER_RESOLUTION", "1440x900"),
        language=BROWSER_LOCALE,
        locale=accept_language(),
        cores=env_int("GOPAY_BROWSER_CORES", 8, 1, 64),
        timezone_offset_min=420,
        timezone_label="WIB",
        timezone_id=BROWSER_TIMEZONE,
        platform_os="Windows",
        browser="chrome",
        chrome_major=136,
        chrome_full_version="136.0.0.0",
        device_memory=env_int("GOPAY_BROWSER_DEVICE_MEMORY", 8, 1, 64),
        max_touch_points=0,
    )


def build_checkout_sentinel_headers(
    session: Any,
    device_id: str,
    *,
    flow: str = "checkout_session_approval",
) -> dict[str, str]:
    from nicepay.nicepay_link_extractor.kakao_extract import checkout_sentinel_headers

    profile = _sentinel_profile()
    removable = (
        "Authorization",
        "Content-Type",
        "Origin",
        "Referer",
        "oai-device-id",
        "oai-language",
        "oai-session-id",
        "oai-client-version",
        "oai-client-build-number",
        "chatgpt-account-id",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
    )
    saved: dict[str, Any] = {}
    for name in removable:
        if name in session.headers:
            saved[name] = session.headers.pop(name)
    os.environ.setdefault("SENTINEL_TURNSTILE_WORKERS", "1")
    try:
        headers = checkout_sentinel_headers(
            session,
            device_id,
            profile,
            flow=flow,
        )
    finally:
        session.headers.update(saved)
    sentinel_token = str(headers.get("OpenAI-Sentinel-Token") or "")
    telemetry = str(headers.get("OAI-Telemetry") or "")
    if not sentinel_token or not telemetry:
        raise GoPayError("GoPay approve Sentinel headers are incomplete")
    try:
        sentinel_payload = json.loads(sentinel_token)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GoPayError("GoPay approve Sentinel token is invalid JSON") from exc
    if (
        sentinel_payload.get("id") != device_id
        or sentinel_payload.get("flow") != flow
        or not sentinel_payload.get("c")
    ):
        raise GoPayError("GoPay approve Sentinel token does not match device/flow")
    log_event(
        "sentinel.generated",
        flow=flow,
        sentinel=bool(sentinel_token),
        sentinel_len=len(sentinel_token),
        telemetry=bool(telemetry),
        telemetry_len=len(telemetry),
        pow=bool(sentinel_payload.get("p")),
        turnstile=bool(sentinel_payload.get("t")),
        session_observer=bool(headers.get("OpenAI-Sentinel-So-Token")),
        session_observer_len=len(str(headers.get("OpenAI-Sentinel-So-Token") or "")),
    )
    return {
        "OpenAI-Sentinel-Token": sentinel_token,
        "OAI-Telemetry": telemetry,
        **(
            {"OpenAI-Sentinel-So-Token": headers["OpenAI-Sentinel-So-Token"]}
            if headers.get("OpenAI-Sentinel-So-Token")
            else {}
        ),
    }


def build_approve_sentinel_headers(session: Any, device_id: str) -> dict[str, str]:
    return build_checkout_sentinel_headers(
        session, device_id, flow="checkout_session_approval"
    )


def chatgpt_approve(
    session: Any,
    checkout: Mapping[str, Any],
    device_id: str,
    *,
    proxy: str,
    confirm_payload: Any = None,
) -> str:
    load_bootstrap_context(session, checkout_page_url(checkout))
    sentinel_headers = build_approve_sentinel_headers(session, device_id)
    observation = client_observation(session)
    attestation = deployment_attestation(session)
    body = {
        "checkout_session_id": str(checkout["checkout_id"]),
        "processor_entity": str(checkout.get("processor_entity") or "openai_llc"),
    }
    headers = {
        "Referer": checkout_page_url(checkout),
        "x-openai-target-path": "/backend-api/payments/checkout/approve",
        "x-openai-target-route": "/backend-api/payments/checkout/approve",
        "x-oai-is-client-observation": observation,
        **sentinel_headers,
    }
    if attestation:
        headers["oai-web-deployment-attestation"] = attestation
    session_id = str(session.headers.get("oai-session-id") or "")
    log_event(
        "approve.before",
        checkout=safe_identifier(checkout["checkout_id"]),
        proxy=proxy_label(proxy),
        session=safe_identifier(session_id),
        sentinel=bool(sentinel_headers.get("OpenAI-Sentinel-Token")),
        sentinel_len=len(str(sentinel_headers.get("OpenAI-Sentinel-Token") or "")),
        telemetry_len=len(str(sentinel_headers.get("OAI-Telemetry") or "")),
        session_observer=bool(sentinel_headers.get("OpenAI-Sentinel-So-Token")),
        session_observer_len=len(
            str(sentinel_headers.get("OpenAI-Sentinel-So-Token") or "")
        ),
        attestation=bool(attestation),
        observation=observation,
        confirm=payment_page_diagnostics(confirm_payload),
    )
    url = "https://chatgpt.com/backend-api/payments/checkout/approve"
    started = time.monotonic()
    response = session.post(url, json=body, headers=headers, timeout=CHATGPT_TIMEOUT)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    remember_response_context(session, response)
    payload = _response_payload(response)
    dump_path = dump_http(response, "gopay_approve", body, "POST", url, force=True)
    result = str(payload.get("result") or "") if isinstance(payload, Mapping) else ""
    log_event(
        "approve.after",
        elapsed_ms=elapsed_ms,
        http_status=response.status_code,
        result=result,
        proxy=proxy_label(proxy),
        session=safe_identifier(session_id),
        attestation=bool(attestation),
        observation=observation,
        dump=dump_path.name if dump_path else "missing",
    )
    return classify_approve_response(response.status_code, payload)


def snapshot_payment_page_once(
    session: Any,
    checkout: Mapping[str, Any],
    ctx: Mapping[str, Any],
    *,
    stage: str,
) -> dict[str, Any]:
    """Capture one immediate Stripe state snapshot after an approval outcome."""
    url = f"https://api.stripe.com/v1/payment_pages/{checkout['checkout_id']}"
    params = {
        **elements_session_params(ctx),
        "key": str(checkout["stripe_pk"]),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION),
    }
    started = time.monotonic()
    response = session.get(url, params=params, headers=stripe_headers(), timeout=TIMEOUT)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        payload = response.json() or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {"raw_response": str(response.text or "")}
    dump_path = dump_http(
        response,
        f"gopay_{stage}_payment_page",
        params,
        "GET",
        url,
        force=True,
    )
    log_event(
        "approve.payment_page_snapshot",
        stage=stage,
        elapsed_ms=elapsed_ms,
        http_status=response.status_code,
        diagnostics=payment_page_diagnostics(payload),
        dump=dump_path.name if dump_path else "missing",
    )
    return payload


def poll_payment_page(
    session: Any,
    checkout: Mapping[str, Any],
    ctx: Mapping[str, Any],
) -> str:
    timeout = env_int("GOPAY_POLL_TIMEOUT", 45, 1, 600)
    interval_ms = env_int("GOPAY_POLL_INTERVAL_MS", 1000, 100, 10000)
    deadline = time.monotonic() + timeout
    url = f"https://api.stripe.com/v1/payment_pages/{checkout['checkout_id']}"
    params = {
        **elements_session_params(ctx),
        "key": str(checkout["stripe_pk"]),
        "_stripe_version": str(ctx.get("stripe_version") or STRIPE_VERSION),
    }
    last_decision = PollDecision("waiting")
    last_diagnostics: dict[str, Any] = {}
    last_summary = ""
    poll_no = 0
    while time.monotonic() < deadline:
        poll_no += 1
        started = time.monotonic()
        response = session.get(url, params=params, headers=stripe_headers(), timeout=TIMEOUT)
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            dump_path = dump_http(
                response, "gopay_poll_http_error", params, "GET", url, force=True
            )
            log_event(
                "poll.http_error",
                poll=poll_no,
                elapsed_ms=elapsed_ms,
                http_status=response.status_code,
                dump=dump_path.name if dump_path else "missing",
            )
            time.sleep(interval_ms / 1000)
            continue
        payload = response.json() or {}
        diagnostics = payment_page_diagnostics(payload)
        last_diagnostics = diagnostics
        summary = json.dumps(diagnostics, ensure_ascii=False, sort_keys=True)
        decision = classify_poll_payload(payload)
        last_decision = decision
        if summary != last_summary or decision.state not in {"waiting", "requires_approval"}:
            last_summary = summary
            log_event(
                "poll.state",
                poll=poll_no,
                elapsed_ms=elapsed_ms,
                http_status=response.status_code,
                decision=decision.state,
                diagnostics=diagnostics,
            )
        if decision.state == "redirect":
            dump_path = dump_http(
                response, "gopay_poll_redirect", params, "GET", url, force=True
            )
            log_event(
                "poll.terminal",
                decision="redirect",
                redirect=redact_url(decision.redirect_url),
                dump=dump_path.name if dump_path else "missing",
            )
            return decision.redirect_url
        if decision.state == "failed":
            dump_path = dump_http(
                response, "gopay_poll_failed", params, "GET", url, force=True
            )
            log_event(
                "poll.terminal",
                decision="failed",
                detail=decision.detail,
                dump=dump_path.name if dump_path else "missing",
            )
            raise GoPayPollFailedError(
                f"GoPay Stripe submission failed: {decision.detail or 'unknown'}"
            )
        if decision.state == "completed":
            dump_path = dump_http(
                response, "gopay_poll_completed_no_redirect", params, "GET", url, force=True
            )
            raise GoPayError(
                "GoPay payment page completed without provider redirect; "
                f"diagnostic dump={dump_path.name if dump_path else 'missing'}"
            )
        time.sleep(interval_ms / 1000)
    synthetic = type("DumpResponse", (), {})()
    synthetic.status_code = 200
    synthetic.url = url
    synthetic.text = json.dumps(last_diagnostics, ensure_ascii=False)
    dump_path = dump_http(
        synthetic, "gopay_poll_timeout", params, "GET", url, force=True
    )
    log_event(
        "poll.timeout",
        timeout=timeout,
        decision=last_decision.state,
        diagnostics=last_diagnostics,
        dump=dump_path.name if dump_path else "missing",
    )
    raise GoPayError(
        f"GoPay redirect poll timed out: state={last_decision.state}; "
        f"dump={dump_path.name if dump_path else 'missing'}"
    )


def run_gopay_flow(
    access_token: str,
    session_token: str,
    proxy_seed: str,
    billing: Mapping[str, str] | None = None,
) -> str:
    proxy = proxy_for_indonesia(proxy_seed)
    device_id = str(uuid.uuid4())
    oai_session_id = str(uuid.uuid4())
    log_event(
        "flow.start",
        country=COUNTRY,
        currency=CURRENCY,
        locale=BROWSER_LOCALE,
        timezone=BROWSER_TIMEZONE,
        proxy=proxy_label(proxy),
        device=safe_identifier(device_id),
        session=safe_identifier(oai_session_id),
        checkout_update=False,
        require_zero=True,
    )
    chatgpt = build_chatgpt_session(
        access_token, session_token, proxy, device_id, oai_session_id
    )
    checkout = create_checkout(chatgpt, device_id)
    load_bootstrap_context(chatgpt, checkout_page_url(checkout))
    stripe = new_session(proxy)
    stripe.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept-Language": accept_language()})
    init_payload, ctx = stripe_init(stripe, checkout)
    stripe_elements_session(stripe, checkout, ctx)
    billing_profile = dict(
        billing or indonesia_billing_profile(account_email_from_token(access_token))
    )
    sync_checkout_taxes(chatgpt, checkout, billing_profile)
    sync_checkout_snapshot(chatgpt, checkout, billing_profile)
    confirm_payload = stripe_confirm_gopay(
        stripe, checkout, init_payload, ctx, billing_profile
    )
    direct_redirect = extract_redirect_url(confirm_payload)
    if direct_redirect:
        log_event("flow.redirect_from_confirm", redirect=redact_url(direct_redirect))
        return direct_redirect
    submission = find_submission_attempt(confirm_payload)
    if str(submission.get("state") or "").lower() != "requires_approval":
        decision = classify_poll_payload(confirm_payload)
        if decision.state == "failed":
            raise GoPayPollFailedError(
                f"GoPay confirm submission failed: {decision.detail or 'unknown'}"
            )
        log_event(
            "flow.confirm_without_approval",
            decision=decision.state,
            diagnostics=payment_page_diagnostics(confirm_payload),
        )
    else:
        try:
            chatgpt_approve(
                chatgpt,
                checkout,
                device_id,
                proxy=proxy,
                confirm_payload=confirm_payload,
            )
        except GoPayApproveBlockedError:
            try:
                snapshot_payment_page_once(
                    stripe,
                    checkout,
                    ctx,
                    stage="approve_blocked",
                )
            except Exception as snapshot_error:
                log_event(
                    "approve.payment_page_snapshot_error",
                    stage="approve_blocked",
                    error_type=type(snapshot_error).__name__,
                    message=str(snapshot_error)[:240],
                )
            raise
    return poll_payment_page(stripe, checkout, ctx)


def _read_text(path: Path) -> str:
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16", "utf-8", "ascii"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeError:
            continue
    return raw.decode("utf-8", errors="ignore").strip()


def _find_named_token(payload: Any, aliases: tuple[str, ...]) -> str:
    alias_names = {name.lower() for name in aliases}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if str(key).lower() in alias_names and isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            found = _find_named_token(value, aliases)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_named_token(value, aliases)
            if found:
                return found
    return ""


def _find_session_cookie(payload: Any) -> str:
    if isinstance(payload, str):
        match = re.search(
            r"(?:^|[;\s])__Secure-next-auth\.session-token=([^;\s]+)",
            payload,
        )
        return unquote(match.group(1)) if match else ""
    if isinstance(payload, dict):
        values = payload.values()
    elif isinstance(payload, list):
        values = payload
    else:
        return ""
    for value in values:
        found = _find_session_cookie(value)
        if found:
            return found
    return ""


def normalize_token(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text or text[0] not in "[{":
        return text, ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return text, ""
    access_token = _find_named_token(
        payload,
        (
            "accessToken",
            "access_token",
            "token",
            "bearerToken",
            "bearer_token",
            "jwt",
        ),
    )
    session_token = _find_named_token(
        payload,
        (
            "sessionToken",
            "session_token",
            "nextAuthSessionToken",
            "next_auth_session_token",
            "__Secure-next-auth.session-token",
            "secureNextAuthSessionToken",
        ),
    ) or _find_session_cookie(payload)
    return access_token, session_token


def load_token() -> tuple[str, str]:
    raw = ""
    source = ""
    for env_name in ("GOPAY_TOKEN", "PP_TOKEN"):
        value = os.environ.get(env_name, "").strip()
        if value:
            raw = value
            source = env_name
            break
    if not raw:
        token_path = DATA_DIR / "token.txt"
        if token_path.is_file():
            raw = _read_text(token_path)
            source = token_path.name
    if not raw:
        raise GoPayError("GoPay access token is missing")
    access_token, embedded_session = normalize_token(raw)
    session_token = (
        os.environ.get("GOPAY_SESSION_TOKEN", "").strip()
        or os.environ.get("PP_SESSION_TOKEN", "").strip()
        or embedded_session
    )
    log_event(
        "token.loaded",
        source=source,
        access_token=bool(access_token),
        session_token=bool(session_token),
    )
    return access_token, session_token


def proxy_seed_file() -> Path:
    configured = (
        os.environ.get("GOPAY_PROXY_SEED_FILE", "").strip()
        or os.environ.get("PP_PROXY_SEED_FILE", "").strip()
    )
    if configured:
        return Path(configured).expanduser()
    local = DATA_DIR / "proxy_seeds.txt"
    if local.is_file():
        return local
    return PROJECT_DIR / "stripe" / "proxy_seeds.txt"


def load_proxy_seeds() -> list[str]:
    path = proxy_seed_file()
    if not path.is_file():
        if env_bool("GOPAY_ALLOW_DIRECT", False):
            return [""]
        raise GoPayError(f"GoPay proxy seed file not found: {path}")
    seeds = [line.strip() for line in _read_text(path).splitlines() if line.strip()]
    seeds = list(dict.fromkeys(seed for seed in seeds if seed))
    random.shuffle(seeds)
    if not seeds:
        raise GoPayError("GoPay proxy seed file is empty")
    log_event("proxy.loaded", count=len(seeds), source=path.name)
    return seeds


def main() -> int:
    try:
        access_token, session_token = load_token()
        if not access_token:
            raise GoPayError("GoPay access token is empty")
        proxy_seeds = load_proxy_seeds()
        max_retry = env_int("GOPAY_MAX_RETRY", 5, 1, 100)
        checkout_retry = env_int("GOPAY_CHECKOUT_RETRY_MAX", max_retry, 1, 100)
        poll_timeout = env_int("GOPAY_POLL_TIMEOUT", 45, 1, 600)
        poll_interval_ms = env_int("GOPAY_POLL_INTERVAL_MS", 1000, 100, 10000)
        attempts = min(len(proxy_seeds), max(max_retry, checkout_retry))
        account_email = account_email_from_token(access_token)
        billing = indonesia_billing_profile(account_email)
        last_error = ""
        blocked = 0
        blocked_limit = env_int("GOPAY_MAX_APPROVE_BLOCKED", max_retry, 1, 100)
        log_event(
            "flow.config",
            max_retry=max_retry,
            checkout_retry=checkout_retry,
            provider_retry_config=env_int("GOPAY_PROVIDER_RETRY_MAX", 1, 1, 100),
            provider_retry_effective=1,
            workers=1,
            proxy_candidates=attempts,
            poll_timeout=poll_timeout,
            poll_interval_ms=poll_interval_ms,
            checkout_update=False,
            approve_retry=False,
            billing_identity_sticky=True,
            billing_email_source=(
                "configured"
                if os.environ.get("GOPAY_EMAIL", "").strip()
                else "account_token"
                if account_email
                else "generated_fallback"
            ),
        )
        for index, proxy_seed in enumerate(proxy_seeds[:attempts], start=1):
            log_event(
                "flow.attempt",
                attempt=index,
                total=attempts,
                proxy=proxy_label(proxy_seed),
            )
            try:
                selected_proxy = resolve_proxy_url(proxy_seed)
                redirect_url = run_gopay_flow(
                    access_token,
                    session_token,
                    selected_proxy,
                    billing,
                )
                if redirect_url:
                    print("\n===== 结果 =====")
                    print(f"GoPay 最终支付 URL:\n{redirect_url}")
                    return 0
                last_error = "GoPay flow returned no redirect"
            except GoPayApproveBlockedError as exc:
                blocked += 1
                last_error = str(exc)
                log_event(
                    "flow.failed",
                    attempt=index,
                    error_type=type(exc).__name__,
                    category="approval_business_rejection",
                    message=str(exc),
                    blocked=blocked,
                    blocked_limit=blocked_limit,
                )
                if blocked >= blocked_limit:
                    break
            except GoPayPollFailedError as exc:
                last_error = str(exc)
                log_event(
                    "flow.failed",
                    attempt=index,
                    error_type=type(exc).__name__,
                    category="provider_payment_failure",
                    message=str(exc)[:300],
                )
            except Exception as exc:
                last_error = str(exc)
                log_event(
                    "flow.failed",
                    attempt=index,
                    error_type=type(exc).__name__,
                    category="protocol_or_transport",
                    message=str(exc)[:300],
                )
        log_event("flow.exhausted", attempts=attempts, last_error=last_error)
        return 1
    except Exception as exc:
        log_event(
            "flow.fatal",
            error_type=type(exc).__name__,
            message=str(exc)[:300],
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
