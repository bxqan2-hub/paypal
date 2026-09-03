from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ..errors import ProtocolError
from ..logging_utils import emit_log
from ._transport import normalize_proxy_url


SDK_URL = "https://chatgpt.com/sentinel/20260810913b/sdk.js"
SDK_BUILD = "20260810913b"
# Resolved once so a rename of the runner fails at import/test time instead of
# silently turning every protected MoMo call into a 502 at run time.
RUNNER_SCRIPT = Path(__file__).with_name("_sentinel_runner.py")
CLIENT_BUILD_NUMBER = "9999461"
CLIENT_VERSION = "prod-d040bc6b02dd2a27b54e1d7c56d181a795593f41"


def _sentinel_attempts() -> int:
    try:
        return max(1, min(10, int(os.environ.get("OPLL_SENTINEL_MAX_ATTEMPTS", "2"))))
    except (TypeError, ValueError):
        return 2


def _sentinel_retry_delay() -> float:
    try:
        return max(0.0, min(10.0, float(os.environ.get("OPLL_SENTINEL_RETRY_DELAY", "0.5"))))
    except (TypeError, ValueError):
        return 0.5


def _header(session: Any, name: str, default: str = "") -> str:
    headers = getattr(session, "headers", {}) or {}
    try:
        value = headers.get(name, default)
    except Exception:
        value = default
    return str(value or default).strip()


def _current_proxy(session: Any) -> str:
    proxies = getattr(session, "proxies", {}) or {}
    return normalize_proxy_url(str(proxies.get("https") or proxies.get("http") or ""))


def _runner_python() -> str:
    configured = str(os.environ.get("OPLL_SENTINEL_PYTHON") or "").strip()
    if configured:
        return configured
    # The service venv intentionally does not carry Playwright.  The server's
    # system Python has the managed Playwright runtime installed.
    if Path("/usr/bin/python3").exists():
        return "/usr/bin/python3"
    return sys.executable


CURRENT_SENTINEL_PAYMENT_METHODS = frozenset({"momo"})


def uses_current_sentinel(payment_method: object) -> bool:
    return str(payment_method or "").strip().lower() in CURRENT_SENTINEL_PAYMENT_METHODS


def mint_sentinel_token(
    chatgpt: Any,
    flow: str,
    log: Any | None = None,
) -> str:
    """Mint a current browser SDK token for one checkout flow.

    A fresh token is minted after proxy switches so the SDK's observed exit
    and browser fingerprint stay paired with the API request using it.
    """
    flow = str(flow or "").strip()
    if flow not in {"chatgpt_checkout", "checkout_session_approval"}:
        raise ProtocolError(502, f"unsupported payment Sentinel flow: {flow or '?'}")
    payload = {
        "flow": flow,
        "proxy": _current_proxy(chatgpt),
        "device_id": _header(chatgpt, "oai-device-id"),
        "session_id": _header(chatgpt, "oai-session-id"),
        "user_agent": _header(chatgpt, "User-Agent"),
        "sec_ch_ua": _header(chatgpt, "sec-ch-ua"),
        "sec_ch_ua_platform": _header(chatgpt, "sec-ch-ua-platform"),
        "language": _header(chatgpt, "oai-language", "en-US"),
        "sdk_url": SDK_URL,
        "sdk_build": SDK_BUILD,
    }
    attempts = _sentinel_attempts()
    retry_delay = _sentinel_retry_delay()
    last_error = f"payment Sentinel SDK failed for {flow}"
    for attempt in range(1, attempts + 1):
        try:
            completed = subprocess.run(
                [_runner_python(), str(RUNNER_SCRIPT)],
                input=json.dumps(payload, ensure_ascii=False),
                text=True,
                capture_output=True,
                timeout=75,
                check=False,
            )
        except subprocess.TimeoutExpired:
            last_error = f"payment Sentinel SDK timed out for {flow}"
        except OSError as exc:
            last_error = f"payment Sentinel SDK runner unavailable: {type(exc).__name__}"
        else:
            if completed.returncode != 0:
                last_error = f"payment Sentinel SDK failed for {flow}"
            else:
                try:
                    result = json.loads(completed.stdout or "{}")
                except json.JSONDecodeError:
                    last_error = f"payment Sentinel SDK returned invalid output for {flow}"
                else:
                    token = str(result.get("token") or "").strip() if isinstance(result, dict) else ""
                    ok = bool(result.get("ok")) if isinstance(result, dict) else False
                    if ok and len(token) >= 100:
                        if attempt > 1:
                            emit_log(log, f"Current Sentinel SDK recovered attempt={attempt}/{attempts} flow={flow}")
                        emit_log(log, f"Current Sentinel SDK token ready flow={flow} build={SDK_BUILD}")
                        return token
                    last_error = f"payment Sentinel SDK returned no token for {flow}"
        if attempt < attempts:
            emit_log(log, f"Current Sentinel SDK retry attempt={attempt + 1}/{attempts} flow={flow}")
            if retry_delay:
                time.sleep(retry_delay * attempt)
    raise ProtocolError(502, f"{last_error} after {attempts} attempts")


def payment_sentinel_headers(chatgpt: Any, flow: str, log: Any | None = None) -> dict[str, str]:
    """Return headers minted by the current browser SDK for protected payment calls."""
    token = mint_sentinel_token(chatgpt, flow, log)
    return {
        "openai-sentinel-token": token,
        "oai-client-build-number": CLIENT_BUILD_NUMBER,
        "oai-client-version": CLIENT_VERSION,
    }

