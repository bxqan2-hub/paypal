from __future__ import annotations

import base64
import json
import re
from typing import Any


_TOKEN_KEYS = {"at", "token", "access_token", "accesstoken"}
_SESSION_TOKEN_KEYS = {
    "sessiontoken",
    "session_token",
    "nextauthsessiontoken",
    "next_auth_session_token",
    "__secure_next_auth_session_token",
    "__secure_next_auth.session_token",
    "securenextauthsessiontoken",
}
_TOKEN_CHARS_RE = re.compile(r"^[A-Za-z0-9._~+/=-]+$")


def _clean_token(value: Any) -> str:
    token = str(value or "").strip()
    if token.lower().startswith("bearer "):
        token = token[7:].strip()
    # Markdown and chat exports often escape URL-safe JWT/JWE characters.
    token = re.sub(r"\\([A-Za-z0-9._~+/=-])", r"\1", token)
    return re.sub(r"\s+", "", token)


def _token_shape(value: Any) -> bool:
    token = _clean_token(value)
    if not 1 <= len(token) <= 16384 or not _TOKEN_CHARS_RE.fullmatch(token):
        return False
    parts = token.split(".")
    # Signed JWT/JWS.
    if len(parts) == 3:
        return all(parts)
    # Compact JWE.  `alg=dir` legitimately has an empty encrypted-key part.
    if len(parts) == 5:
        return bool(parts[0] and parts[2] and parts[3] and parts[4])
    # Keep compatibility with opaque access-token formats.
    return "." not in token


def _find_token(value: Any) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key or "").replace("-", "_").lower()
            if normalized_key in _TOKEN_KEYS and isinstance(nested, str):
                candidate = _extract_from_text(nested)
                if candidate:
                    return candidate
        for nested in value.values():
            candidate = _find_token(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_token(nested)
            if candidate:
                return candidate
    return ""


def _extract_from_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # Stop at the next JSON property when a session export is pasted from the
    # token value onward, e.g. TOKEN","authProvider":"openai.
    marker = re.search(r"['\"]\s*,\s*['\"][A-Za-z][A-Za-z0-9_-]*['\"]\s*:", text)
    if marker:
        text = text[: marker.start()]
    candidate = _clean_token(text.strip(" \t\r\n'\"{}[]"))
    if _token_shape(candidate):
        return candidate
    # Extract a compact JWS/JWE embedded in otherwise malformed export text.
    for match in re.finditer(r"[A-Za-z0-9_\\-]+(?:\.[A-Za-z0-9_\\-]*){2,4}", text):
        candidate = _clean_token(match.group(0))
        if _token_shape(candidate):
            return candidate
    return ""


def extract_access_token(raw: Any) -> str:
    """Extract JWT, compact JWE, or opaque AT from common import envelopes."""
    if isinstance(raw, (dict, list)):
        return _find_token(raw)
    text = str(raw or "").strip()
    if text.startswith("{") or text.startswith("["):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        if value is not None:
            if isinstance(value, str):
                return _extract_from_text(value)
            return _find_token(value)
    return _extract_from_text(text)


def _find_session_token(value: Any) -> str:
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = str(key or "").replace("-", "_").lower()
            compact_key = normalized_key.replace("_", "")
            if (
                normalized_key in _SESSION_TOKEN_KEYS
                or compact_key in {key.replace("_", "") for key in _SESSION_TOKEN_KEYS}
            ) and isinstance(nested, str):
                candidate = str(nested).strip()
                if candidate:
                    return candidate
        for nested in value.values():
            candidate = _find_session_token(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_session_token(nested)
            if candidate:
                return candidate
    return ""


def extract_session_token(raw: Any) -> str:
    """Extract an optional NextAuth session token from an import envelope."""
    if isinstance(raw, (dict, list)):
        return _find_session_token(raw)
    text = str(raw or "").strip()
    if not text.startswith(("{", "[")):
        return ""
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        return ""
    return _find_session_token(value)


def normalize_access_token(raw: str) -> str:
    return extract_access_token(raw)


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
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
