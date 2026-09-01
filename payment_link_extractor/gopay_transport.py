from __future__ import annotations

import os
import base64
import hashlib
import json
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    import requests
except ImportError:  # pragma: no cover - installation issue handled at runtime
    requests = None  # type: ignore

from .auth import account_id
from .config import DEFAULT_TIMEOUT, DEFAULT_USER_AGENT, normalize_payment_method
from .errors import ConfigurationError, NetworkError, ProtocolError
from .logging_utils import compact_url, emit_log, safe_log_text
from .models import ExtractionConfig


EMPTY_PENDING_UPDATES = '{"v":3,"updates":[]}'
SENTINEL_SDK_VERSION = "20260810913b"


def browser_checkout_telemetry(action: str) -> str:
    """Return the eight-value browser telemetry shape seen in both GoPay HARs."""
    normalized = str(action or "checkout").strip().lower()
    duration = round(600 + secrets.randbelow(1601) / 10, 1)
    tail = round(duration + 3 + secrets.randbelow(21) / 10, 1)
    if normalized in {"approve", "confirm"}:
        values = [
            1,
            duration,
            5 + secrets.randbelow(116),
            24 + secrets.randbelow(13),
            24 + secrets.randbelow(41),
            2,
            0,
            tail,
        ]
    else:
        values = [
            1,
            duration,
            12 + secrets.randbelow(15),
            20 + secrets.randbelow(101),
            24 + secrets.randbelow(41),
            2,
            0,
            tail,
        ]
    return json.dumps(values, separators=(",", ":"))

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except ImportError:  # pragma: no cover
    CurlCffiSession = None  # type: ignore

try:
    from curl_cffi.requests import RequestException as CurlCffiRequestException  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from curl_cffi.requests.errors import RequestException as CurlCffiRequestException  # type: ignore
    except ImportError:  # pragma: no cover
        CurlCffiRequestException = None  # type: ignore

try:
    from curl_cffi.requests import HTTPError as CurlCffiHTTPError  # type: ignore
except ImportError:  # pragma: no cover
    try:
        from curl_cffi.requests.errors import HTTPError as CurlCffiHTTPError  # type: ignore
    except ImportError:  # pragma: no cover
        CurlCffiHTTPError = None  # type: ignore


class TransportFactory(Protocol):
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any: ...

    def stripe(self, config: ExtractionConfig) -> Any: ...


GOPAY_BROWSER_PROFILES: tuple[dict[str, Any], ...] = (
    {
        "name": "chrome150",
        "impersonate": "chrome150",
        "user_agent": DEFAULT_USER_AGENT.replace("Chrome/151.", "Chrome/150."),
        "sec_ch_ua": '"Not=A?Brand";v="99", "Google Chrome";v="150", "Chromium";v="150"',
        "weight": 55,
    },
    {
        "name": "chrome131",
        "impersonate": "chrome131",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
        "weight": 20,
    },
    {
        "name": "chrome136",
        "impersonate": "chrome136",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not:A-Brand";v="99"',
        "weight": 15,
    },
    {
        "name": "chrome124",
        "impersonate": "chrome124",
        "user_agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "sec_ch_ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "weight": 10,
    },
)


def select_gopay_browser_profile(
    *,
    device_id: str = "",
    transport_impersonate: str = "",
) -> dict[str, Any]:
    """Bind one coherent browser profile to an entire account context."""
    requested = os.getenv("OPLL_GOPAY_BROWSER_PROFILE", "").strip().lower()
    if requested not in {"", "auto", "chrome"}:
        for profile in GOPAY_BROWSER_PROFILES:
            if requested in {str(profile["name"]).lower(), str(profile["impersonate"]).lower()}:
                return dict(profile)
        raise ConfigurationError(f"unknown GoPay browser profile: {requested}")

    selected_transport = str(transport_impersonate or "").strip().lower()
    if selected_transport and selected_transport != "chrome":
        for profile in GOPAY_BROWSER_PROFILES:
            if selected_transport == str(profile["impersonate"]).lower():
                return dict(profile)
        raise ConfigurationError(
            f"GoPay TLS profile has no matching browser identity: {selected_transport}"
        )

    profiles = list(GOPAY_BROWSER_PROFILES)
    total_weight = sum(max(0, int(profile["weight"])) for profile in profiles)
    if total_weight <= 0:
        raise ConfigurationError("GoPay browser profiles have no positive weight")
    digest = hashlib.sha256(
        f"gopay-browser-profile:{str(device_id or '')}".encode("utf-8")
    ).digest()
    ticket = int.from_bytes(digest[:8], "big") % total_weight
    for profile in profiles:
        weight = max(0, int(profile["weight"]))
        if ticket < weight:
            return dict(profile)
        ticket -= weight
    return dict(profiles[-1])


def validate_tls_ua_consistency(impersonate: str, user_agent: str) -> bool:
    """Reject explicit Chrome TLS/UA version mismatches before a request."""
    tls_match = re.search(r"chrome(\d+)$", str(impersonate or "").lower())
    ua_match = re.search(r"(?:Chrome|Chromium)/(\d+)\.", str(user_agent or ""), re.I)
    if tls_match and ua_match and tls_match.group(1) != ua_match.group(1):
        raise ConfigurationError(
            f"TLS/UA version mismatch: impersonate={impersonate}, ua={ua_match.group(1)}"
        )
    return True


def gopay_browser_identity(config: ExtractionConfig) -> tuple[str, dict[str, Any]]:
    """Return the stable device and paired browser profile for one GoPay AT."""
    device_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"gopay-device:{config.access_token}")
    )
    profile = select_gopay_browser_profile(
        device_id=device_id,
        transport_impersonate=os.getenv("OPLL_HTTP_IMPERSONATE", "").strip(),
    )
    return device_id, profile


def new_session(impersonate: str | None = None) -> Any:
    if CurlCffiSession is not None:
        selected = str(impersonate or os.getenv("OPLL_HTTP_IMPERSONATE", "chrome")).strip() or "chrome"
        return CurlCffiSession(impersonate=selected)
    if requests is None:
        raise ConfigurationError("requests is required; install requirements.txt")
    return requests.Session()


def safe_close(session: Any) -> None:
    provider = getattr(session, "openai_sentinel_provider", None)
    close_provider = getattr(provider, "close", None)
    if callable(close_provider):
        try:
            close_provider()
        except Exception:
            pass
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def _is_1024proxy_host(host: str) -> bool:
    lowered = str(host or "").lower().rstrip(".")
    return lowered == "1024proxy.io" or lowered.endswith(".1024proxy.io")


def _direct_authenticated_proxy_url(
    scheme: str, host: str, port: int | str, username: str, password: str
) -> str:
    return (
        f"{scheme}://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{host}:{port}"
    )


def _is_iprocket_host(host: str) -> bool:
    lowered = str(host or "").lower().rstrip(".")
    return (
        lowered.endswith(".iprocket.io")
        or lowered.endswith(".iprocket.pro")
        or lowered == "proxy.iproyal.net"
        or lowered.endswith(".iproyal.net")
        or lowered == "proxy.iproyal.com"
        or lowered.endswith(".iproyal.com")
        or lowered == "1024proxy.io"
        or lowered.endswith(".1024proxy.io")
    )


def _iprocket_protocol(port: int, scheme: str = "") -> str:
    lowered = str(scheme or "").lower()
    if lowered.startswith("socks"):
        return "socks5"
    if lowered in {"http", "https"}:
        return "http"
    if port in {9595, 59999, 619999}:
        return "socks5"
    if port in {5959, 61999}:
        return "http"
    return "auto"


def _iprocket_bridge_proxy(
    host: str,
    port: int,
    username: str,
    password: str,
    scheme: str = "",
) -> str:
    bridge = os.getenv("IPROCKET_CHAIN_PROXY", "http://127.0.0.1:18796")
    protocol = (
        "socks5"
        if "1024proxy." in host.lower()
        else "http" if "iproyal." in host.lower() else _iprocket_protocol(port, scheme)
    )
    metadata = base64.urlsafe_b64encode(
        f"{protocol}|{host}|{port}|{username}".encode("utf-8")
    ).decode("ascii").rstrip("=")
    parsed_bridge = urlsplit(bridge)
    bridge_host = parsed_bridge.hostname or "127.0.0.1"
    bridge_port = parsed_bridge.port or 18796
    return (
        f"http://iprb_{metadata}:{quote(password, safe='')}"
        f"@{bridge_host}:{bridge_port}"
    )


def normalize_proxy_url(proxy: str) -> str:
    # The web UI accepts proxy pools (one entry per line).  A transport always
    # receives one proxy, so use the first non-empty entry as a safe fallback
    # for API clients that submit the pool without selecting an entry first.
    lines = [line.strip() for line in str(proxy or "").splitlines() if line.strip()]
    text = lines[0] if lines else ""
    if not text:
        return ""
    # IPRocket share/subscription URL: resolve it to the first exported entry.
    try:
        source = urlsplit(text)
        if (
            source.scheme == "https"
            and source.hostname == "app.iprocket.io"
            and source.path.endswith("/clienta/sysnation/getLink")
        ):
            request = Request(text, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(request, timeout=15) as response:
                exported = [
                    line.strip()
                    for line in response.read(1024 * 1024).decode("utf-8", errors="replace").splitlines()
                    if line.strip()
                ]
            return normalize_proxy_url(exported[0] if exported else "")
    except Exception as exc:
        raise ValueError("IPRocket proxy subscription could not be read") from exc
    # IPRocket QR exports use socks://BASE64 or http://BASE64 rather than a
    # conventional URL. Decode that representation before normalizing.
    if text.lower().startswith(("socks://", "http://")) and "@" not in text:
        encoded = text.split("://", 1)[1].strip()
        try:
            padded = encoded + "=" * ((4 - len(encoded) % 4) % 4)
            decoded = base64.b64decode(padded).decode("utf-8").strip()
            if "iprocket." in decoded.lower():
                return normalize_proxy_url(decoded)
        except Exception:
            pass
    had_explicit_scheme = "://" in text
    # IPRocket dashboard export formats 1/2/3. Password remains the fourth
    # field so punctuation inside it is preserved.
    if "://" not in text and "@" not in text:
        separator = next((item for item in (":", "|", ",", ";") if text.count(item) >= 3), ":")
        parts = text.split(separator, 3)
        parsed_vendor: tuple[str, str, str, str] | None = None
        if len(parts) == 4 and _is_iprocket_host(parts[0]) and parts[1].isdigit():  # host:port:user:pass
            parsed_vendor = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 4 and parts[0].isdigit() and _is_iprocket_host(parts[1]):  # port:host:user:pass
            parsed_vendor = parts[1], parts[0], parts[2], parts[3]
        elif len(parts) == 4 and parts[1].isdigit() and _is_iprocket_host(parts[2]):  # pass:port:host:user
            parsed_vendor = parts[2], parts[1], parts[3], parts[0]
        elif len(parts) == 4 and parts[3].isdigit() and _is_iprocket_host(parts[2]):  # user:pass:host:port
            parsed_vendor = parts[2], parts[3], parts[0], parts[1]
        if parsed_vendor is not None:
            host, port, username, password = parsed_vendor
            if _is_1024proxy_host(host) and int(port) == 3000:
                return _direct_authenticated_proxy_url(
                    "socks5h", host, port, username, password
                )
            if _is_iprocket_host(host):
                return _iprocket_bridge_proxy(host, int(port), username, password)
            # Vendor port conventions: IPRocket 9595 and Kookeey gateways are
            # SOCKS5; IPRocket 5959 is HTTP. Resolve DNS through SOCKS as well.
            scheme = (
                "socks5h"
                if port == "9595" or "kookeey" in host.lower()
                else "http"
            )
            text = (
                scheme
                + "://"
                + quote(username, safe="")
                + ":"
                + quote(password, safe="")
                + "@"
                + host
                + ":"
                + port
            )
        elif separator == ":" and len(parts) == 4 and parts[1].isdigit():
            host, port, username, password = parts
            scheme = "socks5h" if "kookeey" in host.lower() else "http"
            text = (
                scheme + "://" + quote(username, safe="") + ":"
                + quote(password, safe="") + "@" + host + ":" + port
            )
    if "://" not in text:
        text = "http://" + text
    try:
        parsed = urlsplit(text)
    except Exception:
        return text
    if not parsed.scheme or not parsed.netloc:
        return text
    host = parsed.hostname or ""
    if not host:
        return text
    if _is_iprocket_host(host) and parsed.username is not None:
        try:
            parsed_port = parsed.port or (9595 if parsed.scheme.lower().startswith("socks") else 5959)
        except ValueError as exc:
            raise ValueError("proxy contains an invalid port") from exc
        if _is_1024proxy_host(host) and parsed_port == 3000:
            return _direct_authenticated_proxy_url(
                "socks5h",
                host,
                parsed_port,
                unquote(parsed.username),
                unquote(parsed.password or ""),
            )
        return _iprocket_bridge_proxy(
            host,
            parsed_port,
            unquote(parsed.username),
            unquote(parsed.password or ""),
            parsed.scheme if had_explicit_scheme else "",
        )
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if parsed.username is not None:
        auth = quote(unquote(parsed.username), safe="%")
        if parsed.password is not None:
            auth += ":" + quote(unquote(parsed.password), safe="%")
        auth += "@"
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError as exc:
        raise ValueError("proxy contains an invalid port") from exc
    netloc = auth + host + port
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


def set_proxy_url(session: Any, proxy: str) -> None:
    normalized = normalize_proxy_url(proxy)
    session.proxies = {"http": normalized, "https": normalized} if normalized else {}


def stage_http_request(
    session: Any,
    stage: str,
    method: str,
    url: str,
    log: Any | None = None,
    **kwargs: Any,
) -> Any:
    started = time.perf_counter()
    emit_log(log, f"{stage}: {method.upper()} {compact_url(url)}")
    # The browser rotates this short-lived observation value on each logical
    # request.  Keep it out of call sites so route/tax/confirm requests all
    # receive the same transport-level treatment while test doubles remain
    # unchanged.
    refresh_headers = getattr(session, "refresh_openai_request_headers", None)
    if callable(refresh_headers):
        try:
            dynamic = refresh_headers(method.upper(), url)
        except Exception:
            dynamic = {}
        if dynamic:
            request_headers = dict(kwargs.get("headers") or {})
            for key, value in dynamic.items():
                request_headers[key] = value
            kwargs["headers"] = request_headers
    try:
        response = session.request(method.upper(), url, **kwargs)
    except Exception as exc:
        detail = safe_log_text(exc)
        emit_log(log, f"{stage}: request error={detail}")
        if is_network_exception(exc):
            raise NetworkError(stage, detail) from exc
        raise
    emit_log(
        log,
        f"{stage}: HTTP {response.status_code} elapsed={time.perf_counter() - started:.2f}s",
    )
    # ChatGPT's opaque pending item is `x-oai-is-receipt`. The much larger
    # `x-oai-is-update` field is a different response artifact and must never
    # be echoed as a receipt. An explicit ack clears the pending envelope.
    response_headers = getattr(response, "headers", None)
    pending_receipt = None
    pending_ack = None
    if response_headers is not None:
        try:
            pending_receipt = response_headers.get("x-oai-is-receipt")
            pending_ack = response_headers.get("x-oai-is-pending-updates-ack")
        except Exception:
            pending_receipt = None
            pending_ack = None
    session_headers = getattr(session, "headers", None)
    if session_headers is not None:
        try:
            if pending_ack:
                session_headers["x-oai-is-pending-updates"] = EMPTY_PENDING_UPDATES
            elif pending_receipt:
                session_headers["x-oai-is-pending-updates"] = json.dumps(
                    {"v": 3, "updates": [str(pending_receipt)]},
                    separators=(",", ":"),
                )
        except Exception:
            pass
    return response


def openai_sentinel_token(session: Any) -> str:
    """Return an optional Sentinel token supplied by the caller's session.

    The browser sends the short-lived value on checkout creation and custom
    checkout confirmation.  It must not be embedded in source; callers may
    inject a fresh value on a transport session (or through the environment
    for a one-off run).
    """
    value = getattr(session, "openai_sentinel_token", "")
    if not value:
        headers = getattr(session, "headers", {})
        value = headers.get("OpenAI-Sentinel-Token") or headers.get("openai-sentinel-token")
    if not value:
        value = os.getenv("OPLL_OPENAI_SENTINEL_TOKEN", "")
    return str(value or "").strip()


def openai_sentinel_so_token(session: Any) -> str:
    """Return the optional Sentinel SO token captured on some browser runs."""
    value = getattr(session, "openai_sentinel_so_token", "")
    if not value:
        headers = getattr(session, "headers", {})
        value = headers.get("OpenAI-Sentinel-SO-Token") or headers.get(
            "openai-sentinel-so-token"
        )
    if not value:
        value = os.getenv("OPLL_OPENAI_SENTINEL_SO_TOKEN", "")
    return str(value or "").strip()


def openai_sentinel_headers(
    session: Any,
    *,
    flow: str = "",
    referer: str = "",
    log: Any | None = None,
    required: bool = False,
) -> dict[str, str]:
    """Build fresh Sentinel/attestation headers for a browser checkout.

    A live browser provider is preferred for the two protected checkout
    operations.  The environment/session fallback remains available for
    deployments that deliberately inject their own short-lived values and for
    older non-GCash flows.
    """
    headers: dict[str, str] = {}
    provider = getattr(session, "openai_sentinel_provider", None)
    if required and not flow:
        flow = "chatgpt_checkout"
    if required and provider is None:
        raise RuntimeError("browser Sentinel provider is required for this Checkout")
    if flow and provider is not None:
        try:
            generated = provider.headers(flow, referer=referer)
            if generated:
                headers.update(generated)
                # Preserve an explicitly injected fresh attestation/SO token
                # when the browser page is unauthenticated and cannot expose
                # its bootstrap field yet.
                if "oai-web-deployment-attestation" not in headers:
                    fallback_attestation = os.getenv("OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION", "").strip()
                    if fallback_attestation:
                        headers["oai-web-deployment-attestation"] = fallback_attestation
                if "OpenAI-Sentinel-SO-Token" not in headers:
                    fallback_so = openai_sentinel_so_token(session)
                    if fallback_so:
                        headers["OpenAI-Sentinel-SO-Token"] = fallback_so
                return headers
        except Exception as exc:
            # Do not turn an optional browser helper into a transport outage;
            # the caller can still use explicitly supplied values.
            emit_log(log, f"Sentinel browser provider unavailable: {safe_log_text(exc, 180)}")
            if required:
                raise RuntimeError(
                    "browser Sentinel proof generation failed"
                ) from exc
    token = openai_sentinel_token(session)
    if token:
        headers["OpenAI-Sentinel-Token"] = token
    so_token = openai_sentinel_so_token(session)
    if so_token:
        headers["OpenAI-Sentinel-SO-Token"] = so_token
    if required and not headers.get("OpenAI-Sentinel-Token"):
        raise RuntimeError("browser Sentinel proof is missing")
    return headers


def prepare_openai_browser_session(session: Any) -> None:
    """Start the GoPay browser context before the promo probe when present."""
    provider = getattr(session, "openai_sentinel_provider", None)
    prepare = getattr(provider, "prepare", None)
    if callable(prepare):
        prepare()


def prepare_openai_browser_flow(
    session: Any,
    *,
    flow: str,
    referer: str,
    required: bool = False,
) -> bool:
    """Prefetch one Sentinel challenge without exporting a request token."""
    provider = getattr(session, "openai_sentinel_provider", None)
    prepare_flow = getattr(provider, "prepare_flow", None)
    if callable(prepare_flow):
        prepare_flow(flow=flow, referer=referer)
        return True
    if required:
        raise RuntimeError("browser Sentinel flow provider is required")
    return False


def _session_cookie_value(session: Any, name: str) -> str:
    target = str(name or "").strip()
    headers = getattr(session, "headers", None)
    cookie_header = str(headers.get("Cookie") or "") if headers is not None else ""
    for part in cookie_header.split(";"):
        key, separator, value = part.strip().partition("=")
        if separator and key == target:
            return value
    jar = getattr(session, "cookies", None)
    if jar is not None:
        try:
            value = jar.get(target)
            if value:
                return str(value)
        except Exception:
            pass
    return ""


def synchronize_stripe_browser_ids(session: Any, ctx: dict[str, Any]) -> None:
    """Bind Stripe confirm metrics to the ChatGPT browser cookie identity."""
    muid = _session_cookie_value(session, "__stripe_mid")
    if muid:
        ctx["muid"] = muid
    else:
        muid = str(ctx.get("muid") or "").strip()
        if muid:
            provider = getattr(session, "openai_sentinel_provider", None)
            set_cookie = getattr(provider, "set_cookie", None)
            if callable(set_cookie):
                set_cookie("__stripe_mid", muid, http_only=False)
            headers = getattr(session, "headers", None)
            if headers is not None and not _session_cookie_value(session, "__stripe_mid"):
                current = str(headers.get("Cookie") or "").strip()
                headers["Cookie"] = (current + "; " if current else "") + f"__stripe_mid={muid}"
            jar = getattr(session, "cookies", None)
            if jar is not None:
                try:
                    jar.set("__stripe_mid", muid, domain=".chatgpt.com", path="/")
                except Exception:
                    pass
    sid = _session_cookie_value(session, "__stripe_sid")
    if sid and sid.upper() != "NA":
        ctx["sid"] = sid
    else:
        # The successful no-cookie browser shape sends the literal NA.
        ctx["sid"] = "NA"


def is_network_exception(exc: BaseException) -> bool:
    """Return whether an exception indicates a transport failure.

    HTTP errors are deliberately excluded: an HTTP response means the transport
    completed, even when the provider returned a 4xx or 5xx status.
    """
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True

    if requests is not None:
        request_exceptions = requests.exceptions
        transport_exceptions = (
            request_exceptions.ConnectionError,
            request_exceptions.Timeout,
            request_exceptions.ChunkedEncodingError,
        )
        if isinstance(exc, transport_exceptions):
            return True

    if CurlCffiRequestException is not None:
        if isinstance(exc, CurlCffiRequestException):
            if CurlCffiHTTPError is not None and isinstance(exc, CurlCffiHTTPError):
                return False
            return type(exc).__name__ in {
                "ConnectionError",
                "ConnectTimeout",
                "ProxyError",
                "ReadTimeout",
                "SSLError",
                "Timeout",
            }

    return False


def response_json(response: Any, stage: str) -> dict[str, Any]:
    try:
        payload = response.json() or {}
    except Exception as exc:
        raise ProtocolError(502, f"{stage} invalid json: {safe_log_text(exc)}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(502, f"{stage} returned non-object json")
    return payload


def _agent_browser_binary() -> str:
    """Locate the optional native agent-browser executable."""
    configured = os.getenv("OPLL_AGENT_BROWSER_BIN", "").strip()
    if configured and Path(configured).exists():
        return configured
    if sys.platform == "win32":
        candidates = (
            Path.home()
            / "AppData"
            / "Roaming"
            / "npm"
            / "node_modules"
            / "agent-browser"
            / "bin"
            / "agent-browser-win32-x64.exe",
            Path(sys.prefix)
            / "node_modules"
            / "agent-browser"
            / "bin"
            / "agent-browser-win32-x64.exe",
        )
    else:
        candidates = ()
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return shutil.which("agent-browser") or shutil.which("agent-browser.cmd") or ""


def _decode_agent_browser_output(text: str) -> Any:
    """Decode agent-browser's JSON output without logging its contents."""
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        pass
    # Native builds can prepend a one-line status glyph when JSON mode is not
    # requested.  Decode the first complete JSON value in the remaining text.
    for start, char in enumerate(value):
        if char not in "[{\"":
            continue
        try:
            return json.JSONDecoder().raw_decode(value[start:])[0]
        except Exception:
            continue
    return value


class BrowserSentinelProvider:
    """Generate short-lived Sentinel headers in a real browser context.

    The checkout API binds the Sentinel proof to the browser's device cookie,
    user-agent and fingerprint.  Replaying a HAR token therefore cannot work.
    This adapter keeps the browser helper optional: when it is not installed or
    a deployment cannot be opened, callers retain the explicit environment
    fallback used by older deployments.
    """

    def __init__(
        self,
        *,
        access_token: str,
        device_id: str,
        session_id: str,
        user_agent: str,
        proxy: str,
        transport_session: Any,
        session_token: str = "",
        language: str = "en-US",
        timezone: str = "America/New_York",
        log: Any | None = None,
    ) -> None:
        self.access_token = str(access_token or "").strip()
        self.device_id = str(device_id or "").strip()
        self.session_id = str(session_id or "").strip()
        self.user_agent = str(user_agent or DEFAULT_USER_AGENT)
        self.proxy = str(proxy or "").strip()
        # Chromium/agent-browser accepts an unauthenticated HTTP proxy more
        # reliably than an authenticated ``socks5h://`` URL. Reuse the
        # loopback CONNECT bridge for 1024proxy-style authenticated SOCKS5
        # routes while keeping the direct normalized proxy for HTTP clients.
        try:
            from .web.socks5_bridge import http_proxy_for

            self.browser_proxy = http_proxy_for(self.proxy)
        except Exception:
            self.browser_proxy = self.proxy
        self.transport_session = transport_session
        self.session_token = str(session_token or "").strip()
        self.language = str(language or "en-US").strip() or "en-US"
        self.timezone = str(timezone or "America/New_York").strip() or "America/New_York"
        self.log = log
        self.binary = _agent_browser_binary()
        self.namespace = "opll_sentinel_" + uuid.uuid4().hex[:12]
        self.session_name = "checkout_" + uuid.uuid4().hex[:12]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="opll-sentinel-"))
        self.locale_script = self.temp_dir / "locale.js"
        self.sentinel_init_script = self.temp_dir / "sentinel-init.js"
        self.locale_script.write_text(
            "Object.defineProperty(navigator, 'language', {get: () => "
            + json.dumps(self.language)
            + "});Object.defineProperty(navigator, 'languages', {get: () => ["
            + json.dumps(self.language)
            + ", 'en']});",
            encoding="utf-8",
        )
        self.sentinel_init_script.write_text(
            self._build_sentinel_init_script(), encoding="utf-8"
        )
        self._lock = threading.RLock()
        self._started = False
        self._closed = False
        self._failed = False
        self._attestation = ""
        self._cookies = ""
        self._launch_args_used = False

    @staticmethod
    def _build_sentinel_init_script() -> str:
        """Inject the bundled SDK as a window property before page scripts run."""
        assets = Path(__file__).resolve().parent / "sentinel_assets"
        sdk = (assets / "sentinel_sdk.js").read_text(encoding="utf-8")
        return (
            "(() => {\n"
            "  const install = () => { try {\n"
            # The bootstrap shim is for the Node VM bridge and attempts to
            # replace browser read-only globals such as crypto/navigator.  A
            # real Chromium page already supplies those values, so inject
            # only the SDK itself and publish its var explicitly on window.
            f"{sdk}\n"
            "    window.SentinelSDK = SentinelSDK;\n"
            "    globalThis.SentinelSDK = SentinelSDK;\n"
            "    if (!window.__opllSentinelFetchWrapped) {\n"
            "      const originalFetch = window.fetch.bind(window);\n"
            "      window.fetch = async (...args) => {\n"
            "        const raw = args[0] && args[0].url ? args[0].url : String(args[0] || '');\n"
            "        const absolute = new URL(raw, location.origin).href;\n"
            "        const isPing = new URL(absolute).pathname === '/backend-api/sentinel/ping';\n"
            "        const started = performance.now();\n"
            "        if (isPing && window.__opllSentinelReferer) {\n"
            "          const init = Object.assign({}, args[1] || {}, {referrer: window.__opllSentinelReferer, referrerPolicy: 'strict-origin-when-cross-origin'});\n"
            "          args = [args[0], init];\n"
            "        }\n"
            "        const response = await originalFetch(...args);\n"
            "        if (isPing) {\n"
            "          const headersAt = performance.now();\n"
            "          try { await response.clone().arrayBuffer(); } catch (_) {}\n"
            "          await new Promise(resolve => setTimeout(resolve, 0));\n"
            "          const ended = performance.now();\n"
            "          const entries = performance.getEntriesByName(absolute);\n"
            "          const entry = entries[entries.length - 1];\n"
            "          const action = entry && entry.responseStart && entry.requestStart ? entry.responseStart - entry.requestStart : headersAt - started;\n"
            "          const total = Math.max(0, Math.round(ended - started));\n"
            "          const bodyRead = Math.max(0, Math.round(ended - headersAt));\n"
            "          const number = name => Number(response.headers.get(name) || 0) || 0;\n"
            "          const rtt = number('s-cf-tcp-rtt-msec') || number('s-cf-quic-rtt-msec');\n"
            "          const protocol = entry && String(entry.nextHopProtocol || '').toLowerCase();\n"
            "          const protocolCode = protocol.includes('h3') ? 3 : protocol.includes('h2') ? 2 : protocol.includes('http/1') ? 1 : 0;\n"
            "          window.__opllLastSentinelTelemetry = [1, action, number('s-cf-edge-msec'), number('s-cf-origin-ttfb-msec'), rtt, protocolCode, bodyRead, Math.max(total, Math.ceil(action))];\n"
            "        }\n"
            "        return response;\n"
            "      };\n"
            "      window.__opllSentinelFetchWrapped = true;\n"
            "    }\n"
            "    window.__opllSentinelInjected = true;\n"
            "  } catch (error) {\n"
            "    window.__opllSentinelInjectionError = String(error && error.message || error);\n"
            "  } };\n"
            "  if (document.body) install();\n"
            "  else document.addEventListener('DOMContentLoaded', install, {once:true});\n"
            "})();\n"
        )

    @property
    def enabled(self) -> bool:
        mode = os.getenv("OPLL_SENTINEL_BROWSER", "auto").strip().lower()
        return mode not in {"0", "false", "off", "disabled", "no"} and bool(self.binary)

    def _base_command(self) -> list[str]:
        if not self.binary:
            raise RuntimeError("agent-browser executable not found")
        command = [
            self.binary,
            "--namespace",
            self.namespace,
            "--session",
            self.session_name,
            "--user-agent",
            self.user_agent,
            "--init-script",
            str(self.locale_script),
            "--init-script",
            str(self.sentinel_init_script),
        ]
        if self.browser_proxy:
            command.extend(["--proxy", self.browser_proxy])
        return command

    def _run(self, args: list[str], timeout: float = 75.0) -> Any:
        if self._closed:
            raise RuntimeError("Sentinel browser provider is closed")
        output_path = self.temp_dir / ("command-" + uuid.uuid4().hex + ".out")
        env = dict(os.environ)
        # Chromium uses TZ when constructing the browser fingerprint.  Keep
        # Keep the browser and selected checkout locale coherent when supported.
        env.setdefault("TZ", self.timezone)
        env["AGENT_BROWSER_NAMESPACE"] = self.namespace
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        status = -1
        try:
            with output_path.open("w", encoding="utf-8") as output:
                process = subprocess.Popen(
                    self._base_command() + args,
                    stdin=subprocess.DEVNULL,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    env=env,
                    creationflags=creation_flags,
                )
                try:
                    status = process.wait(timeout=timeout)
                except subprocess.TimeoutExpired as exc:
                    process.kill()
                    process.wait(timeout=5)
                    raise RuntimeError("agent-browser command timed out") from exc
            text = output_path.read_text(encoding="utf-8", errors="replace")
        finally:
            # The daemon can briefly retain the redirected handle.  Cleanup is
            # best-effort and the temporary directory is removed on close.
            for _ in range(10):
                try:
                    output_path.unlink()
                    break
                except OSError:
                    time.sleep(0.05)
        if status != 0:
            detail = safe_log_text(text, 600)
            raise RuntimeError(
                f"agent-browser exited with status {status}"
                + (f": {detail}" if detail else "")
            )
        return _decode_agent_browser_output(text)

    def _eval(self, expression: str, timeout: float = 75.0) -> Any:
        return self._run(["eval", expression], timeout=timeout)

    def _capture_bootstrap(self) -> None:
        value = self._eval(
            "(() => {"
            "const node=document.getElementById('client-bootstrap');"
            "if (!node) return {};"
            "try { const data=JSON.parse(node.textContent||'{}');"
            "return {attestation:data.webDeploymentAttestation||'',locale:data.locale||'',sessionId:data.sessionId||''}; }"
            "catch (_) { return {}; }"
            "})()"
        )
        if isinstance(value, dict):
            attestation = str(value.get("attestation") or "").strip()
            if attestation:
                self._attestation = attestation
        if self._attestation:
            return
        # Current deployments may generate the signed value inside the web
        # client instead of exposing `client-bootstrap`. Recover the fresh
        # value from a same-context backend request without logging it.
        try:
            captured = self._run(
                ["network", "requests", "--json", "--filter", "chatgpt.com/backend-api"],
                timeout=20,
            )
        except Exception:
            return
        data = captured.get("data") if isinstance(captured, dict) else None
        requests = data.get("requests") if isinstance(data, dict) else None
        if not isinstance(requests, list):
            return
        for item in reversed(requests):
            if not isinstance(item, dict):
                continue
            headers = item.get("requestHeaders") or item.get("headers")
            if isinstance(headers, list):
                header_map = {
                    str(header.get("name") or "").lower(): str(header.get("value") or "")
                    for header in headers
                    if isinstance(header, dict)
                }
            elif isinstance(headers, dict):
                header_map = {str(key).lower(): str(val) for key, val in headers.items()}
            else:
                continue
            attestation = str(
                header_map.get("oai-web-deployment-attestation") or ""
            ).strip()
            if attestation:
                self._attestation = attestation
                return

    def _sync_pending_update_from_browser(self) -> None:
        """Mirror the browser's latest encrypted update receipt.

        ChatGPT returns `x-oai-is-receipt` on browser responses and the next
        request sends it as `x-oai-is-pending-updates`. The HTTP transport does
        not see those browser-only response headers, so read only the header
        metadata from agent-browser's request monitor and keep the opaque
        value in memory.
        """
        try:
            value = self._run(
                ["network", "requests", "--json", "--filter", "chatgpt.com/backend-api"],
                timeout=20,
            )
        except Exception:
            return
        data = value.get("data") if isinstance(value, dict) else None
        requests = data.get("requests") if isinstance(data, dict) else None
        if not isinstance(requests, list):
            return
        for item in reversed(requests):
            if not isinstance(item, dict):
                continue
            headers = item.get("responseHeaders")
            if not isinstance(headers, dict):
                continue
            receipt = headers.get("x-oai-is-receipt") or headers.get("X-OAI-IS-Receipt")
            if not receipt:
                continue
            transport_headers = getattr(self.transport_session, "headers", None)
            if transport_headers is None:
                return
            transport_headers["x-oai-is-pending-updates"] = json.dumps(
                {"v": 3, "updates": [str(receipt)]}, separators=(",", ":")
            )
            return

    def _set_cookie(self, name: str, value: str, *, http_only: bool = True) -> None:
        """Set a browser cookie, chunking large NextAuth values safely."""
        cookie_name = str(name or "").strip()
        cookie_value = str(value or "")
        if not cookie_name or not cookie_value:
            return
        chunk_size = 3800
        chunks = [
            cookie_value[index : index + chunk_size]
            for index in range(0, len(cookie_value), chunk_size)
        ]
        if len(chunks) == 1 and len(cookie_name) + len(chunks[0]) <= 4096:
            names = [(cookie_name, chunks[0])]
        else:
            names = [
                (f"{cookie_name}.{index}", chunk)
                for index, chunk in enumerate(chunks)
            ]
        for chunk_name, chunk in names:
            args = [
                "cookies", "set", chunk_name, chunk,
                "--domain", ".chatgpt.com", "--path", "/",
            ]
            if http_only:
                args.append("--httpOnly")
            args.append("--secure")
            self._run(args)

    def _sync_cookies(self) -> None:
        value = self._run(["cookies", "get", "--json"])
        cookies: list[dict[str, Any]] = []
        if isinstance(value, dict):
            data = value.get("data")
            if isinstance(data, dict) and isinstance(data.get("cookies"), list):
                cookies = [item for item in data["cookies"] if isinstance(item, dict)]
            elif isinstance(data, list):
                cookies = [item for item in data if isinstance(item, dict)]
        pairs = []
        for cookie in cookies:
            name = str(cookie.get("name") or "").strip()
            val = str(cookie.get("value") or "")
            if name:
                pairs.append(f"{name}={val}")
                if name.lower() == "oai-did" and val:
                    self.device_id = val
        if pairs:
            self._cookies = "; ".join(pairs)
            headers = getattr(self.transport_session, "headers", None)
            if headers is not None:
                headers["Cookie"] = self._cookies
                if self.device_id:
                    headers["oai-device-id"] = self.device_id

    def _start(self) -> None:
        if not self.enabled:
            raise RuntimeError("agent-browser is disabled or unavailable")
        try:
            # Init scripts are not executed by agent-browser on about:blank;
            # load the real ChatGPT origin so the SDK runs in a browser page
            # with the same origin, storage, and fingerprint as Checkout.
            self._run(["open", "https://chatgpt.com/", "--json"])
            self._started = True
            self._launch_args_used = True
            injected: Any = {}
            for _ in range(10):
                injected = self._eval(
                    "(() => ({injected:!!window.SentinelSDK,token:typeof window.SentinelSDK?.token,proto2:typeof window.SentinelSDK?.__proto2,error:window.__opllSentinelInjectionError||''}))()"
                )
                if isinstance(injected, dict) and (
                    injected.get("token") == "function"
                    or injected.get("proto2") == "function"
                ):
                    break
                self._run(["wait", "100"])
            if not isinstance(injected, dict) or not (
                injected.get("token") == "function"
                or injected.get("proto2") == "function"
            ):
                detail = str((injected or {}).get("error") or "SentinelSDK injection failed")
                raise RuntimeError(detail)
            # A NextAuth session may be represented by cookies left by a
            # previous browser context. Clear that context before installing
            # the current session so duplicate `.0/.1` chunks cannot be
            # concatenated into a stale token.
            if self.session_token:
                self._run(["cookies", "clear"])
            self._set_cookie("oai-did", self.device_id)
            if self.session_token:
                self._set_cookie("__Secure-next-auth.session-token", self.session_token)
            frame_url = (
                "https://chatgpt.com/backend-api/sentinel/frame.html?sv="
                + SENTINEL_SDK_VERSION
            )
            self._run(["open", frame_url])
            # The bootstrap document carries the current signed deployment
            # attestation.  The initial navigation is authenticated when an
            # AT is available; an unauthenticated page simply leaves this
            # optional field empty while token generation still remains useful.
            auth_headers = {
                "Authorization": f"Bearer {self.access_token}",
                "oai-device-id": self.device_id,
                "oai-session-id": self.session_id,
                "oai-language": self.language,
                # Keep deployed checkout build identifiers overridable because
                # the web client rotates them independently of payment flow.
                "oai-client-build-number": os.getenv(
                    "OPLL_OAI_CLIENT_BUILD_NUMBER", ""
                ).strip()
                or "10012890",
                "oai-client-version": os.getenv(
                    "OPLL_OAI_CLIENT_VERSION", ""
                ).strip()
                or "prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4",
            }
            account = account_id(self.access_token)
            if account:
                # The browser sends the selected ChatGPT account on the
                # authenticated bootstrap navigation.  Keeping it aligned
                # with the curl transport avoids loading a different account
                # shell when an AT belongs to a multi-account workspace.
                auth_headers["chatgpt-account-id"] = account
            self._run(
                [
                    "--headers",
                    json.dumps(auth_headers, separators=(",", ":")),
                    "open",
                    "https://chatgpt.com/?promo_campaign=plus-1-month-free",
                ]
            )
            self._capture_bootstrap()
            self._sync_pending_update_from_browser()
            self._run(["open", frame_url])
            self._sync_cookies()
        except Exception:
            self._failed = True
            raise

    def prepare(self) -> None:
        """Eagerly bootstrap the browser and mirror pending update state."""
        with self._lock:
            if self._failed:
                raise RuntimeError("Sentinel browser provider failed during startup")
            if not self._started:
                self._start()
            else:
                self._sync_pending_update_from_browser()

    def _store_ping_telemetry(self, name: str, telemetry: list[Any]) -> None:
        if len(telemetry) != 8:
            return
        setattr(
            self.transport_session,
            name,
            json.dumps(telemetry, separators=(",", ":")),
        )

    def _captured_ping_telemetry(self) -> list[Any]:
        value = self._eval(
            "(() => Array.isArray(window.__opllLastSentinelTelemetry) ? window.__opllLastSentinelTelemetry : [])()"
        )
        if not isinstance(value, list) or len(value) != 8:
            return []
        try:
            numbers = [float(item) for item in value]
        except (TypeError, ValueError):
            return []
        if numbers[1] <= 0 or numbers[7] < numbers[1]:
            return []
        return [
            1,
            numbers[1],
            int(numbers[2]),
            int(numbers[3]),
            int(numbers[4]),
            int(numbers[5]),
            int(numbers[6]),
            numbers[7],
        ]

    def prepare_flow(self, *, flow: str, referer: str) -> None:
        """Match SentinelSDK.init(flow): prefetch challenge, then browser ping."""
        with self._lock:
            if self._failed:
                raise RuntimeError("Sentinel browser provider failed during startup")
            if not self._started:
                self._start()
            selected_flow = self._normalize_flow(flow)
            self._eval(
                "(async()=>{window.__opllSentinelReferer="
                + json.dumps(str(referer or ""))
                + ";await window.SentinelSDK.init("
                + json.dumps(selected_flow)
                + ");return true})()",
                timeout=90,
            )
            try:
                self._sync_cookies()
            except Exception:
                pass

    @staticmethod
    def _normalize_flow(flow: str) -> str:
        """Never let the SDK's ineffective default flow reach Checkout."""
        selected = str(flow or "").strip()
        if selected.lower() in {"", "default", "__default__"}:
            return "chatgpt_checkout"
        return selected

    def headers(self, flow: str, *, referer: str = "") -> dict[str, str]:
        with self._lock:
            if self._failed:
                raise RuntimeError("Sentinel browser provider failed during startup")
            if not self._started:
                self._start()
            selected_flow = self._normalize_flow(flow)
            generated = self._eval(
                "(async()=>{window.__opllSentinelReferer="
                + json.dumps(str(referer or ""))
                + ";const token=await window.SentinelSDK.token("
                + json.dumps(selected_flow)
                + ");const timing=typeof window.SentinelSDK.timing==='function'"
                "?window.SentinelSDK.timing():null;return {token,timing}})()",
                timeout=90,
            )
            raw = generated.get("token") if isinstance(generated, dict) else generated
            timing_raw = generated.get("timing") if isinstance(generated, dict) else None
            if isinstance(raw, str):
                token = raw
            elif isinstance(raw, dict):
                token = json.dumps(raw, separators=(",", ":"))
            else:
                token = ""
            if not token:
                raise RuntimeError("SentinelSDK returned an empty token")
            result = {"OpenAI-Sentinel-Token": token}
            if bool(
                getattr(
                    self.transport_session,
                    "openai_sentinel_observer_enabled",
                    True,
                )
            ):
                observer = ""
                try:
                    observer_raw = self._eval(
                        "(async()=>{if(typeof window.SentinelSDK.sessionObserverToken!=='function')return '';"
                        "const token=await window.SentinelSDK.sessionObserverToken("
                        + json.dumps(selected_flow)
                        + ");return token||''})()",
                        timeout=45,
                    )
                    if isinstance(observer_raw, str):
                        observer = observer_raw.strip()
                    elif isinstance(observer_raw, dict):
                        observer = json.dumps(observer_raw, separators=(",", ":"))
                except Exception:
                    observer = ""
                if observer:
                    result["OpenAI-Sentinel-SO-Token"] = observer
            ping_telemetry: list[Any] = []
            if isinstance(timing_raw, str):
                try:
                    timing_value = json.loads(timing_raw)
                except (TypeError, ValueError):
                    timing_value = None
            else:
                timing_value = timing_raw
            if isinstance(timing_value, list) and len(timing_value) == 8:
                ping_telemetry = timing_value
            if not ping_telemetry:
                ping_telemetry = self._captured_ping_telemetry()
            if selected_flow == "chatgpt_checkout":
                self._store_ping_telemetry(
                    "openai_checkout_telemetry", ping_telemetry
                )
            else:
                self._store_ping_telemetry(
                    "openai_approve_telemetry", ping_telemetry
                )
            # token() and sessionObserverToken() may update HttpOnly browser
            # cookies. Read them through the browser/CDP command after proof
            # creation, then mirror oai-did into the HTTP transport.
            if hasattr(self, "_run"):
                try:
                    self._sync_cookies()
                except Exception:
                    pass
            if self._attestation:
                result["oai-web-deployment-attestation"] = self._attestation
                session_headers = getattr(self.transport_session, "headers", None)
                if session_headers is not None:
                    session_headers["oai-web-deployment-attestation"] = self._attestation
            if self._cookies:
                result["Cookie"] = self._cookies
            current_device_id = str(getattr(self, "device_id", "") or "").strip()
            if current_device_id:
                result["oai-device-id"] = current_device_id
            return result

    def set_cookie(self, name: str, value: str, *, http_only: bool = False) -> None:
        """Install a runtime cookie into the same browser proof context."""
        with self._lock:
            if self._failed:
                raise RuntimeError("Sentinel browser provider failed during startup")
            if not self._started:
                self._start()
            self._set_cookie(name, value, http_only=http_only)
            try:
                self._sync_cookies()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._started and self.binary:
                try:
                    self._run(["close", "--json"], timeout=30)
                except Exception:
                    pass
            self._closed = True
            try:
                for child in self.temp_dir.iterdir():
                    try:
                        child.unlink()
                    except OSError:
                        pass
                self.temp_dir.rmdir()
            except OSError:
                pass


class PlaywrightSentinelProvider:
    """Generate GoPay proofs in one persistent real Chromium context."""

    def __init__(
        self,
        *,
        access_token: str,
        device_id: str,
        session_id: str,
        user_agent: str,
        proxy: str,
        transport_session: Any,
        session_token: str = "",
        language: str = "id-ID",
        timezone: str = "Asia/Jakarta",
        log: Any | None = None,
    ) -> None:
        self.access_token = str(access_token or "").strip()
        self.device_id = str(device_id or "").strip()
        self.session_id = str(session_id or "").strip()
        self.user_agent = str(user_agent or DEFAULT_USER_AGENT)
        self.proxy = str(proxy or "").strip()
        try:
            from .web.socks5_bridge import http_proxy_for

            self.browser_proxy = http_proxy_for(self.proxy)
        except Exception:
            self.browser_proxy = self.proxy
        self.transport_session = transport_session
        self.session_token = str(session_token or "").strip()
        self.language = str(language or "id-ID").strip() or "id-ID"
        self.timezone = str(timezone or "Asia/Jakarta").strip() or "Asia/Jakarta"
        self.log = log
        self._lock = threading.RLock()
        self._runtime_id = ""
        self._attestation = ""
        self._cookies = ""
        self._profile_path = ""
        self._browser_channel = ""
        self._browser_version = ""
        self._challenge_shapes: list[dict[str, Any]] = []
        self._sdk_sha256 = ""
        self._started = False
        self._closed = False
        self._failed = False

    @property
    def enabled(self) -> bool:
        mode = os.getenv("OPLL_SENTINEL_BROWSER", "auto").strip().lower()
        if mode in {"0", "false", "off", "disabled", "no"}:
            return False
        try:
            from .gopay_sentinel_playwright import get_playwright_daemon

            return callable(get_playwright_daemon)
        except Exception:
            return False

    @staticmethod
    def _daemon() -> Any:
        from .gopay_sentinel_playwright import get_playwright_daemon

        return get_playwright_daemon()

    def _apply_runtime(self, result: dict[str, Any]) -> None:
        attestation = str(result.get("attestation") or "").strip()
        if attestation:
            self._attestation = attestation
        cookies = str(result.get("cookie_header") or "").strip()
        if cookies:
            self._cookies = cookies
        profile_path = str(result.get("profile_path") or "").strip()
        if profile_path:
            self._profile_path = profile_path
        browser_channel = str(result.get("browser_channel") or "").strip()
        if browser_channel:
            self._browser_channel = browser_channel
        browser_version = str(result.get("browser_version") or "").strip()
        if browser_version:
            self._browser_version = browser_version
        challenge_shapes = result.get("challenge_shapes")
        if isinstance(challenge_shapes, list):
            self._challenge_shapes = [
                dict(item) for item in challenge_shapes if isinstance(item, dict)
            ]
        sdk_sha256 = str(result.get("sdk_sha256") or "").strip()
        if sdk_sha256:
            self._sdk_sha256 = sdk_sha256
        headers = getattr(self.transport_session, "headers", None)
        if headers is not None:
            if self._cookies:
                headers["Cookie"] = self._cookies
            headers["oai-device-id"] = self.device_id
            if self._attestation:
                headers["oai-web-deployment-attestation"] = self._attestation
            receipt = str(result.get("latest_receipt") or "").strip()
            if receipt:
                headers["x-oai-is-pending-updates"] = json.dumps(
                    {"v": 3, "updates": [receipt]}, separators=(",", ":")
                )

    def _start(self) -> None:
        if not self.enabled:
            raise RuntimeError("Playwright Sentinel provider is unavailable")
        if self._closed:
            raise RuntimeError("Playwright Sentinel provider is closed")
        try:
            opened = self._daemon().open_session(
                access_token=self.access_token,
                account_id=account_id(self.access_token),
                device_id=self.device_id,
                session_id=self.session_id,
                user_agent=self.user_agent,
                browser_proxy=self.browser_proxy,
                session_token=self.session_token,
                language=self.language,
                timezone=self.timezone,
            )
            self._runtime_id = str(opened.get("runtime_id") or "")
            if not self._runtime_id:
                raise RuntimeError("Playwright Sentinel session did not start")
            self._apply_runtime(opened)
            self._started = True
        except Exception:
            self._failed = True
            raise

    def prepare(self) -> None:
        with self._lock:
            if self._failed:
                raise RuntimeError("Playwright Sentinel provider failed during startup")
            if not self._started:
                self._start()

    def prepare_flow(self, *, flow: str, referer: str) -> None:
        with self._lock:
            if self._failed:
                raise RuntimeError("Playwright Sentinel provider failed during startup")
            if not self._started:
                self._start()
            result = self._daemon().prepare_flow(
                self._runtime_id,
                BrowserSentinelProvider._normalize_flow(flow),
                str(referer or "https://chatgpt.com"),
            )
            self._apply_runtime(result)
            setattr(
                self.transport_session,
                "openai_sentinel_prepare_events",
                list(result.get("request_events") or []),
            )

    def headers(self, flow: str, *, referer: str = "") -> dict[str, str]:
        with self._lock:
            if self._failed:
                raise RuntimeError("Playwright Sentinel provider failed during startup")
            if not self._started:
                self._start()
            selected_flow = BrowserSentinelProvider._normalize_flow(flow)
            generated = self._daemon().token(
                self._runtime_id,
                selected_flow,
                str(referer or "https://chatgpt.com"),
            )
            self._apply_runtime(generated)
            raw_token = generated.get("token")
            if isinstance(raw_token, str):
                token = raw_token.strip()
            elif isinstance(raw_token, dict):
                token = json.dumps(raw_token, separators=(",", ":"))
            else:
                token = ""
            if not token:
                raise RuntimeError("Playwright Chromium returned an empty Sentinel token")
            timing = generated.get("timing")
            if isinstance(timing, str):
                try:
                    timing = json.loads(timing)
                except (TypeError, ValueError):
                    timing = None
            if not isinstance(timing, list) or len(timing) != 8:
                timing = json.loads(
                    browser_checkout_telemetry(
                        "checkout" if selected_flow == "chatgpt_checkout" else "approve"
                    )
                )
            setattr(
                self.transport_session,
                "openai_checkout_telemetry"
                if selected_flow == "chatgpt_checkout"
                else "openai_approve_telemetry",
                json.dumps(timing, separators=(",", ":")),
            )
            setattr(
                self.transport_session,
                "openai_sentinel_token_events",
                list(generated.get("request_events") or []),
            )
            result = {
                "OpenAI-Sentinel-Token": token,
                "oai-device-id": self.device_id,
            }
            if self._attestation:
                result["oai-web-deployment-attestation"] = self._attestation
            if self._cookies:
                result["Cookie"] = self._cookies
            return result

    def set_cookie(self, name: str, value: str, *, http_only: bool = False) -> None:
        with self._lock:
            if not self._started:
                self._start()
            result = self._daemon().set_cookie(
                self._runtime_id,
                str(name),
                str(value),
                bool(http_only),
            )
            self._apply_runtime(result)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            if self._runtime_id:
                try:
                    self._daemon().close_session(self._runtime_id)
                except Exception:
                    pass
            self._closed = True


class DefaultTransportFactory:
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any:
        payment_method = normalize_payment_method(config.payment_method)
        is_gopay = payment_method == "gopay"
        if is_gopay:
            device_id, profile = gopay_browser_identity(config)
        else:
            device_id, profile = str(uuid.uuid4()), None
        impersonate = (profile or {}).get("impersonate") or os.getenv(
            "OPLL_HTTP_IMPERSONATE", ""
        ).strip()
        try:
            session = new_session(impersonate if is_gopay else None)
        except TypeError:
            # Keep compatibility with lightweight test/fallback session factories.
            session = new_session()
        session_id = str(uuid.uuid4())
        user_agent = (
            os.getenv("OPLL_USER_AGENT", "").strip()
            or (profile or {}).get("user_agent", "")
            or DEFAULT_USER_AGENT
        )
        if is_gopay:
            validate_tls_ua_consistency(impersonate or "", user_agent)
            validate_tls_ua_consistency(str((profile or {}).get("name") or ""), user_agent)
        observation_override = os.getenv("OPLL_OAI_IS_CLIENT_OBSERVATION", "").strip()
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": (
                    country_accept_language(config)
                    if normalize_payment_method(config.payment_method) == "gopay"
                    else f"{country_locale(config)},en;q=0.9"
                ),
                "Authorization": f"Bearer {config.access_token}",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Content-Type": "application/json",
                "oai-device-id": device_id,
                "oai-session-id": session_id,
                # ChatGPT's API language is the browser UI locale; the
                # country-specific Accept-Language remains separate above.
                # Keep GoPay's Indonesian locale independent from the legacy
                # PayPal OPLL_OAI_LANGUAGE setting loaded by older .env files.
                "oai-language": os.getenv("OPLL_GOPAY_OAI_LANGUAGE", "").strip()
                or country_locale(config),
                # These values match the current browser checkout contract;
                # environment overrides keep the transport forward-compatible
                # when the web deployment rotates its build identifier.
                "oai-client-build-number": (
                    os.getenv("OPLL_OAI_CLIENT_BUILD_NUMBER", "").strip()
                    or ("10109010" if is_gopay else "9748354")
                ),
                "oai-client-version": (
                    os.getenv("OPLL_OAI_CLIENT_VERSION", "").strip()
                    or (
                        "prod-31e08510fe1189856ad77823ca134a25c60715b5"
                        if is_gopay
                        else "prod-1e268a33279bcedafc2fe5526bfe230880444b77"
                    )
                ),
                "x-oai-is-pending-updates": os.getenv(
                    "OPLL_X_OAI_IS_PENDING_UPDATES", ""
                ).strip()
                or '{"v":3,"updates":[]}',
                # The browser keeps one observation id for a request burst;
                # callers may pin a captured value for diagnostics, while the
                # default remains fresh for every transport session.
                "x-oai-is-client-observation": os.getenv(
                    "OPLL_OAI_IS_CLIENT_OBSERVATION",
                    f"v1.r.p.{secrets.token_urlsafe(12).rstrip('=')}",
                ),
                "sec-ch-ua": os.getenv("OPLL_SEC_CH_UA", "").strip()
                or (profile or {}).get(
                    "sec_ch_ua",
                    '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
                ),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": os.getenv(
                    "OPLL_SEC_CH_UA_PLATFORM", ""
                ).strip()
                or '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "Cookie": f"oai-did={device_id}",
            }
        )
        account = account_id(config.access_token)
        if account:
            session.headers["chatgpt-account-id"] = account
        # Keep these values on the session for the browser Sentinel adapter
        # and diagnostics without putting identifiers into request URLs/logs.
        session.openai_device_id = device_id
        session.gopay_browser_profile = (profile or {}).get("name", "")
        session.gopay_tls_impersonate = impersonate or ""
        session.openai_did = device_id
        session.openai_proxy = proxy
        session.openai_client_observation = session.headers.get(
            "x-oai-is-client-observation", ""
        )
        session.openai_request_started = time.perf_counter()
        session.openai_sentinel_observer_enabled = not is_gopay

        def refresh_openai_request_headers(method: str, url: str) -> dict[str, str]:
            pinned = observation_override or os.getenv("OPLL_OAI_IS_CLIENT_OBSERVATION", "").strip()
            observation = pinned or f"v1.r.p.{secrets.token_urlsafe(12).rstrip('=')}"
            session.openai_client_observation = observation
            session.headers["x-oai-is-client-observation"] = observation
            dynamic: dict[str, str] = {"x-oai-is-client-observation": observation}
            normalized_url = str(url or "").lower()
            is_checkout = normalized_url.endswith("/backend-api/payments/checkout")
            is_checkout_confirm = normalized_url.endswith(
                "/backend-api/payments/checkout/confirm"
            )
            is_checkout_approve = normalized_url.endswith(
                "/backend-api/payments/checkout/approve"
            )
            if method.upper() == "POST" and (
                is_checkout or is_checkout_confirm or is_checkout_approve
            ):
                if payment_method not in {"paypal", "gopay"}:
                    dynamic["oai-telemetry"] = os.getenv(
                        "OPLL_OAI_CHECKOUT_TELEMETRY", "[1,null]"
                    )
                elif is_checkout_confirm or is_checkout_approve:
                    override = os.getenv("OPLL_OAI_APPROVE_TELEMETRY", "").strip()
                    captured = str(
                        getattr(
                            session,
                            "openai_approve_telemetry"
                            if is_checkout_approve
                            else "openai_checkout_telemetry",
                            "",
                        )
                        or ""
                    ).strip()
                    dynamic["oai-telemetry"] = (
                        override
                        or captured
                        or browser_checkout_telemetry(
                            "approve" if is_checkout_approve else "confirm"
                        )
                    )
                    if is_checkout_approve:
                        # Both complete GoPay HARs acknowledge an empty update
                        # envelope on approve, regardless of earlier tax receipts.
                        dynamic["x-oai-is-pending-updates"] = EMPTY_PENDING_UPDATES
                else:
                    captured = str(
                        getattr(session, "openai_checkout_telemetry", "") or ""
                    ).strip()
                    dynamic["oai-telemetry"] = os.getenv(
                        "OPLL_OAI_CHECKOUT_TELEMETRY",
                        captured or browser_checkout_telemetry("checkout"),
                    )
            return dynamic

        session.refresh_openai_request_headers = refresh_openai_request_headers
        # Deployment attestation is browser-generated and optional.  Never
        # bake a captured value into the repository; operators can inject a
        # fresh value for environments that enforce it.
        attestation = os.getenv("OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION", "").strip()
        if attestation:
            session.headers["oai-web-deployment-attestation"] = attestation
        sentinel = os.getenv("OPLL_OPENAI_SENTINEL_TOKEN", "").strip()
        if sentinel:
            session.openai_sentinel_token = sentinel
        sentinel_so = os.getenv("OPLL_OPENAI_SENTINEL_SO_TOKEN", "").strip()
        if sentinel_so:
            session.openai_sentinel_so_token = sentinel_so
        normalized_proxy = normalize_proxy_url(proxy)
        if normalize_payment_method(config.payment_method) in {"paypal", "gopay"}:
            mode = os.getenv("OPLL_SENTINEL_BROWSER", "auto").strip().lower()
            if mode not in {"0", "false", "off", "disabled", "no"}:
                provider_type = (
                    PlaywrightSentinelProvider if is_gopay else BrowserSentinelProvider
                )
                session.openai_sentinel_provider = provider_type(
                    access_token=config.access_token,
                    device_id=device_id,
                    session_id=session_id,
                    user_agent=user_agent,
                    proxy=normalized_proxy,
                    transport_session=session,
                    session_token=str(getattr(config, "session_token", "") or ""),
                    language=country_locale(config),
                    timezone=country_timezone(config),
                )
        session.proxies = (
            {"http": normalized_proxy, "https": normalized_proxy}
            if normalized_proxy
            else {}
        )
        return session

    def stripe(self, config: ExtractionConfig) -> Any:
        is_gopay = normalize_payment_method(config.payment_method) == "gopay"
        profile: dict[str, Any] | None = None
        impersonate = ""
        if is_gopay:
            _, profile = gopay_browser_identity(config)
            impersonate = str(profile.get("impersonate") or "")
        try:
            session = new_session(impersonate if is_gopay else None)
        except TypeError:
            session = new_session()
        user_agent = (
            os.getenv("OPLL_USER_AGENT", "").strip()
            or str((profile or {}).get("user_agent") or "")
            or DEFAULT_USER_AGENT
        )
        if is_gopay:
            validate_tls_ua_consistency(impersonate, user_agent)
            validate_tls_ua_consistency(str((profile or {}).get("name") or ""), user_agent)
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept-Language": (
                    country_accept_language(config)
                    if is_gopay
                    else f"{country_locale(config)},en;q=0.9"
                ),
            }
        )
        if is_gopay:
            session.headers.update(
                {
                    "sec-ch-ua": os.getenv("OPLL_SEC_CH_UA", "").strip()
                    or str(profile.get("sec_ch_ua") or ""),
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": os.getenv(
                        "OPLL_SEC_CH_UA_PLATFORM", '"Windows"'
                    ),
                }
            )
            session.gopay_browser_profile = str(profile.get("name") or "")
            session.gopay_tls_impersonate = impersonate
        set_proxy_url(session, config.checkout_proxy)
        return session


# The copied factory is intentionally named at the GoPay boundary so the
# PayPal transport module remains untouched and cannot be selected by mistake.
GoPayTransportFactory = DefaultTransportFactory


def country_locale(config: ExtractionConfig) -> str:
    # Config is normalized before a transport is created. Keep this helper
    # dependency-free so fake factories can use the same interface.
    from .config import country_config

    return country_config(config.country)[2]


def country_accept_language(config: ExtractionConfig) -> str:
    """Match Chromium's locale-first Accept-Language ordering."""
    locale = country_locale(config)
    language = locale.split("-", 1)[0]
    return f"{locale},{language};q=0.9"


def country_timezone(config: ExtractionConfig) -> str:
    from .config import country_config

    return country_config(config.country)[3]
