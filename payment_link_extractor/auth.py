from __future__ import annotations

import base64
import json
import re
from typing import Any


_TOKEN_KEY_NAMES = {"accesstoken", "access_token", "token", "at"}
_SESSION_TOKEN_KEY_NAMES = {
    "sessiontoken",
    "session_token",
    "__secure_next_auth_session_token",
    "__secure_next_auth.session_token",
}
_KEYED_TOKEN_RE = re.compile(
    r"(?:^|[{,;\s])['\"]?(?:access[_-]?token|accesstoken|token|at)['\"]?\s*[:=]\s*['\"]?([A-Za-z0-9._~+/=\-]{8,})",
    re.IGNORECASE,
)
_JWT_RE = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
_SESSION_METADATA_RE = re.compile(
    r"['\"]\s*,\s*['\"][A-Za-z][A-Za-z0-9_-]*['\"]\s*:",
    re.IGNORECASE,
)


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
    jwt = _JWT_RE.search(_clean_token_text(text))
    if jwt:
        return jwt.group(0)
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


def _clean_session_token(value: Any) -> str:
    """Normalize a NextAuth session cookie without treating an AT as one."""
    text = str(value or "").strip()
    if text.lower().startswith("cookie:"):
        text = text.split(":", 1)[1].strip()
    # Accept a copied Cookie header or cookie jar line, but only select the
    # named session cookie.  Never derive this value from an access token.
    for part in re.split(r"[;\r\n]+", text):
        name, separator, cookie_value = part.strip().partition("=")
        if separator and _normalize_key(name) in _SESSION_TOKEN_KEY_NAMES:
            text = cookie_value.strip()
            break
    text = text.strip().strip('"\'')
    return re.sub(r"\s+", "", text)


def _find_session_token(value: Any) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _normalize_key(key) in _SESSION_TOKEN_KEY_NAMES:
                found = _clean_session_token(nested)
                if found:
                    return found
        if _normalize_key(value.get("name")) in _SESSION_TOKEN_KEY_NAMES:
            found = _clean_session_token(value.get("value"))
            if found:
                return found
        for nested in value.values():
            found = _find_session_token(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_session_token(nested)
            if found:
                return found
    elif isinstance(value, str):
        found = _clean_session_token(value)
        if found and "=" in value:
            return found if "." in found else ""
    return ""


def extract_session_token(raw: Any) -> str:
    """Extract the authenticated NextAuth JWE/session cookie from an export."""
    value: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                value = json.loads(text)
            except json.JSONDecodeError:
                value = raw
    return _find_session_token(value)


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
