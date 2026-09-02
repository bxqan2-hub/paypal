from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Any, Iterable

from ..logging_utils import safe_log_text


EVENT_HISTORY_SIZE = 500
_SECRET_KEYS = {
    "access_token", "accesstoken", "authorization", "cookie", "password",
    "security_code", "securitycode", "cvv", "pin", "otp", "api_key", "apikey",
    "client_secret", "clientsecret",
}
_TOKEN_KEYS = {
    "token", "ba_token", "batoken", "ec_token", "ectoken",
    "billing_agreement_id", "billingagreementid", "billingagreementtoken",
}
_FUNCTIONAL_URL_KEYS = {"provider_url", "paypal_url"}


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact_text(value: Any, secrets: Iterable[str] = ()) -> str:
    text = safe_log_text(value, limit=1200)
    for secret in secrets:
        secret = str(secret or "")
        if secret:
            text = text.replace(secret, "***")
    text = re.sub(r"(https?://[^\s:/]+:)[^@\s]+@", r"\1***@", text, flags=re.I)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*", "Bearer <redacted>", text)
    text = re.sub(r"\b(?:BA|EC)-[A-Za-z0-9]{8,80}\b", _mask_token_match, text)
    text = re.sub(
        r"(?i)([?&](?:access_token|client_secret|ba_token|ec_token|token|otp|pin)=)[^&\s\"']+",
        r"\1<redacted>",
        text,
    )
    return text


def _mask_token(value: str) -> str:
    return value if len(value) <= 10 else f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"


def _mask_token_match(match: re.Match[str]) -> str:
    return _mask_token(match.group(0))


def sanitize_event_data(value: Any, key: str = "") -> Any:
    """Recursively sanitize event payloads before history/WebSocket storage."""
    compact = str(key or "").lower().replace("-", "_")
    collapsed = compact.replace("_", "")
    if isinstance(value, dict):
        return {str(name): sanitize_event_data(item, str(name)) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_event_data(item, key) for item in value]
    if compact in _SECRET_KEYS or collapsed in _SECRET_KEYS:
        return "<redacted>"
    if compact in _TOKEN_KEYS or collapsed in _TOKEN_KEYS:
        text = str(value or "")
        return _mask_token(text) if len(text) > 10 else "<redacted>"
    if not isinstance(value, str):
        return value
    # A successful PayPal extraction is delivered to the browser through the
    # task event stream.  Keep the strict BA approval URL intact in these two
    # result fields so copy/push actions receive the same value as /api/tasks.
    # Other URLs and all credential-bearing fields continue through redaction.
    if compact in _FUNCTIONAL_URL_KEYS:
        try:
            from ..stripe_common import is_paypal_ba_approval_url

            if is_paypal_ba_approval_url(value):
                return value
        except Exception:
            pass
    return redact_text(value)


def make_event(task_id: str, event_type: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": event_type,
        "task_id": task_id,
        "timestamp": utc_timestamp(),
        "data": sanitize_event_data(data or {}),
    }
