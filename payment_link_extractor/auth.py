from __future__ import annotations

import base64
import json
import re
from typing import Any


_TOKEN_KEY_NAMES = {"accesstoken", "access_token", "token", "at"}
_KEYED_TOKEN_RE = re.compile(
    r"(?:^|[{,;\s])['\"]?(?:access[_-]?token|accesstoken|token|at)['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)
_SESSION_METADATA_RE = re.compile(r"['\"]\s*,\s*['\"]rumViewTags['\"]\s*:", re.IGNORECASE)


def _normalize_key(key: Any) -> str:
    return str(key or "").replace("-", "_").lower()


def _clean_token_text(value: Any) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    # Markdown/JSON viewers may escape URL-safe characters when copying.
    token = re.sub(r"\\([A-Za-z0-9._~+/-])", r"\1", token)
    return re.sub(r"\s+", "", token)


def _embedded_access_token(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    keyed = _KEYED_TOKEN_RE.search(text)
    if keyed:
        return _clean_token_text(keyed.group(1))
    marker = _SESSION_METADATA_RE.search(text)
    if marker and marker.start() > 0:
        candidate = text[: marker.start()].strip(" \t\r\n'\"{}")
        return _clean_token_text(candidate)
    return ""


def _find_token(value: Any) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _normalize_key(key) in _TOKEN_KEY_NAMES:
                found = (
                    (_embedded_access_token(nested) or _clean_token_text(nested))
                    if isinstance(nested, str)
                    else _find_token(nested)
                )
                if found:
                    return found
        for nested in value.values():
            found = _find_token(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_token(nested)
            if found:
                return found
    return ""


def extract_access_token(raw: Any) -> str:
    """Extract an access token from a token, JSON envelope, or session export."""
    if isinstance(raw, (dict, list)):
        return _find_token(raw)
    text = str(raw or "").strip()
    if text.startswith("{") or text.startswith("["):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            found = _find_token(parsed)
            if found:
                return found
            if isinstance(parsed, str):
                return _clean_token_text(parsed)
    return _embedded_access_token(text) or _clean_token_text(text)


def normalize_access_token(raw: str) -> str:
    return extract_access_token(raw)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = normalize_access_token(token).split(".")
    if len(parts) < 2:
        return {}
    try:
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def account_email(access_token: str) -> str:
    payload = decode_jwt_payload(access_token)
    profile = payload.get("https://api.openai.com/profile")
    if isinstance(profile, dict) and "@" in str(profile.get("email") or ""):
        return str(profile["email"]).strip()
    for key in ("email", "preferred_username", "upn"):
        value = str(payload.get(key) or "").strip()
        if "@" in value:
            return value
    return ""


def account_id(access_token: str) -> str:
    """Extract the ChatGPT account UUID used by the browser API headers."""
    payload = decode_jwt_payload(access_token)
    auth = payload.get("https://api.openai.com/auth")
    if isinstance(auth, dict):
        for key in ("chatgpt_account_id", "account_id", "id"):
            value = str(auth.get(key) or "").strip()
            if value:
                return value
    for key in ("chatgpt_account_id", "account_id"):
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""
