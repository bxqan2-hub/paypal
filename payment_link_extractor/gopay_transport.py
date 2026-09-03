from __future__ import annotations

import os
import base64
import json
import re
import secrets
import threading
import time
import uuid
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit
from urllib.request import Request, urlopen

try:
    import requests
except ImportError:  # pragma: no cover - installation issue handled at runtime
    requests = None  # type: ignore

from .auth import account_id
from .config import DEFAULT_USER_AGENT, normalize_payment_method
from .errors import ConfigurationError, NetworkError, ProtocolError
from .logging_utils import compact_url, emit_log, safe_log_text
from .models import ExtractionConfig


EMPTY_PENDING_UPDATES = '{"v":3,"updates":[]}'
PENDING_RECEIPT_LIMIT = 2
GOPAY_OAI_CLIENT_BUILD_NUMBER = "10012890"
GOPAY_OAI_CLIENT_VERSION = "prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4"


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


def _pending_receipts_from_header(value: Any) -> list[str]:
    try:
        payload = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return []
    updates = payload.get("updates") if isinstance(payload, dict) else None
    if not isinstance(updates, list):
        return []
    values = [str(item).strip() for item in updates if str(item).strip()]
    return values[-PENDING_RECEIPT_LIMIT:]

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
        "name": "chrome151",
        "impersonate": "chrome151",
        "user_agent": DEFAULT_USER_AGENT,
        "sec_ch_ua": '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
    },
)


def select_gopay_browser_profile(
    *,
    device_id: str = "",
    transport_impersonate: str = "",
) -> dict[str, Any]:
    """Bind one coherent browser profile to an entire account context."""
    requested = os.getenv("OPLL_GOPAY_BROWSER_PROFILE", "").strip().lower()
    if requested not in {"", "auto", "chrome", "chrome151"}:
        for profile in GOPAY_BROWSER_PROFILES:
            if requested in {str(profile["name"]).lower(), str(profile["impersonate"]).lower()}:
                return dict(profile)
        raise ConfigurationError(f"unknown GoPay browser profile: {requested}")

    selected_transport = str(transport_impersonate or "").strip().lower()
    if selected_transport and selected_transport not in {"chrome", "chrome151"}:
        for profile in GOPAY_BROWSER_PROFILES:
            if selected_transport == str(profile["impersonate"]).lower():
                return dict(profile)
        raise ConfigurationError(
            f"GoPay TLS profile has no matching browser identity: {selected_transport}"
        )
    return dict(GOPAY_BROWSER_PROFILES[0])


def validate_gopay_client_hints(user_agent: str, sec_ch_ua: str) -> bool:
    ua_match = re.search(r"(?:Chrome|Chromium)/(\d+)\.", str(user_agent or ""), re.I)
    if not ua_match:
        raise ConfigurationError("GoPay User-Agent must identify Chrome or Chromium")
    expected = ua_match.group(1)
    hints = re.findall(
        r'"(Google Chrome|Chromium)"\s*;\s*v="(\d+)"',
        str(sec_ch_ua or ""),
        re.I,
    )
    brands = {brand.lower() for brand, _ in hints}
    if brands != {"google chrome", "chromium"} or any(
        value != expected for _, value in hints
    ):
        raise ConfigurationError(
            f"User-Agent/client-hints version mismatch: ua={expected}, hints={','.join(value for _, value in hints)}"
        )
    return True


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
    stable_account_id = account_id(config.access_token)
    device_seed = stable_account_id or config.access_token
    device_id = str(
        uuid.uuid5(uuid.NAMESPACE_URL, f"gopay-device:{device_seed}")
    )
    profile = select_gopay_browser_profile(
        device_id=device_id,
        transport_impersonate="",
    )
    return device_id, profile


def new_session(impersonate: str | None = None) -> Any:
    if CurlCffiSession is not None:
        selected = str(impersonate or os.getenv("OPLL_HTTP_IMPERSONATE", "chrome")).strip() or "chrome"
        return CurlCffiSession(impersonate=selected)
    if impersonate:
        raise ConfigurationError("curl_cffi is required for the GoPay Chrome 151 identity")
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
            pending_receipts = getattr(session, "openai_pending_receipts", None)
            if not isinstance(pending_receipts, list):
                pending_receipts = _pending_receipts_from_header(
                    session_headers.get("x-oai-is-pending-updates", "")
                )
            if pending_ack:
                pending_receipts = []
                session_headers["x-oai-is-pending-updates"] = EMPTY_PENDING_UPDATES
            elif pending_receipt:
                selected_receipt = str(pending_receipt).strip()
                if selected_receipt and selected_receipt not in pending_receipts:
                    pending_receipts.append(selected_receipt)
                pending_receipts = pending_receipts[-PENDING_RECEIPT_LIMIT:]
                session_headers["x-oai-is-pending-updates"] = json.dumps(
                    {"v": 3, "updates": pending_receipts},
                    separators=(",", ":"),
                )
            setattr(session, "openai_pending_receipts", pending_receipts)
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
        value = os.getenv(
            "OPLL_GOPAY_OPENAI_SENTINEL_TOKEN"
            if getattr(session, "gopay_browser_profile", "")
            else "OPLL_OPENAI_SENTINEL_TOKEN",
            "",
        )
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
        value = os.getenv(
            "OPLL_GOPAY_OPENAI_SENTINEL_SO_TOKEN"
            if getattr(session, "gopay_browser_profile", "")
            else "OPLL_OPENAI_SENTINEL_SO_TOKEN",
            "",
        )
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
    older deployments.
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
                    fallback_attestation = os.getenv(
                        "OPLL_GOPAY_OAI_WEB_DEPLOYMENT_ATTESTATION"
                        if getattr(session, "gopay_browser_profile", "")
                        else "OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION",
                        "",
                    ).strip()
                    if fallback_attestation:
                        headers["oai-web-deployment-attestation"] = fallback_attestation
                if "OpenAI-Sentinel-SO-Token" not in headers:
                    fallback_so = openai_sentinel_so_token(session)
                    if fallback_so:
                        headers["OpenAI-Sentinel-SO-Token"] = fallback_so
                if "OpenAI-Sentinel-Token" not in headers:
                    fallback_token = openai_sentinel_token(session)
                    if fallback_token:
                        headers["OpenAI-Sentinel-Token"] = fallback_token
                if required and not headers.get("OpenAI-Sentinel-Token"):
                    raise RuntimeError("browser Sentinel proof is missing")
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








def _normalize_sentinel_flow(flow: str) -> str:
    selected = str(flow or "").strip()
    if selected.lower() in {"", "default", "__default__"}:
        return "chatgpt_checkout"
    return selected


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
        promo_campaign: bool = True,
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
        self.promo_campaign = bool(promo_campaign)
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
        device_id = str(result.get("device_id") or "").strip()
        if device_id:
            self.device_id = device_id
            setattr(self.transport_session, "openai_device_id", device_id)
            setattr(self.transport_session, "openai_did", device_id)
        session_id = str(result.get("session_id") or "").strip()
        try:
            session_id = str(uuid.UUID(session_id)) if session_id else ""
        except (TypeError, ValueError):
            session_id = ""
        if session_id:
            self.session_id = session_id
            setattr(self.transport_session, "openai_session_id", session_id)
        current_session_id = str(getattr(self, "session_id", "") or "").strip()
        try:
            current_session_id = str(uuid.UUID(current_session_id)) if current_session_id else ""
        except (TypeError, ValueError):
            current_session_id = ""
        if current_session_id:
            self.session_id = current_session_id
            setattr(self.transport_session, "openai_session_id", current_session_id)
        attestation = str(result.get("attestation") or "").strip()
        if "attestation" in result:
            self._attestation = attestation
        cookies = str(result.get("cookie_header") or "").strip()
        if "cookie_header" in result:
            self._cookies = cookies or (
                f"oai-did={self.device_id}" if self.device_id else ""
            )
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
            if "cookie_header" in result:
                if self._cookies:
                    headers["Cookie"] = self._cookies
                else:
                    headers.pop("Cookie", None)
            headers["oai-device-id"] = self.device_id
            if current_session_id:
                headers["oai-session-id"] = current_session_id
            if "attestation" in result:
                if self._attestation:
                    headers["oai-web-deployment-attestation"] = self._attestation
                else:
                    headers.pop("oai-web-deployment-attestation", None)
            elif self._attestation:
                headers["oai-web-deployment-attestation"] = self._attestation
            receipt = str(result.get("latest_receipt") or "").strip()
            if receipt:
                pending_receipts = getattr(self.transport_session, "openai_pending_receipts", None)
                if not isinstance(pending_receipts, list):
                    pending_receipts = _pending_receipts_from_header(
                        headers.get("x-oai-is-pending-updates", "")
                    )
                if receipt not in pending_receipts:
                    pending_receipts.append(receipt)
                pending_receipts = pending_receipts[-PENDING_RECEIPT_LIMIT:]
                setattr(
                    self.transport_session,
                    "openai_pending_receipts",
                    pending_receipts,
                )
                headers["x-oai-is-pending-updates"] = json.dumps(
                    {"v": 3, "updates": pending_receipts}, separators=(",", ":")
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
                promo_campaign=self.promo_campaign,
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
                _normalize_sentinel_flow(flow),
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
            selected_flow = _normalize_sentinel_flow(flow)
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
            if is_gopay:
                raise
            session = new_session()
        session_id = str(uuid.uuid4())
        language = (
            (
                os.getenv("OPLL_GOPAY_OAI_LANGUAGE", "").strip()
                or country_locale(config)
            )
            if is_gopay
            else os.getenv("OPLL_GOPAY_OAI_LANGUAGE", "").strip()
            or country_locale(config)
        )
        user_agent = (
            str((profile or {}).get("user_agent") or "").strip()
            if is_gopay
            else os.getenv("OPLL_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
        ) or DEFAULT_USER_AGENT
        if is_gopay:
            validate_tls_ua_consistency(impersonate or "", user_agent)
            validate_tls_ua_consistency(str((profile or {}).get("name") or ""), user_agent)
        observation_override = os.getenv(
            "OPLL_GOPAY_OAI_IS_CLIENT_OBSERVATION"
            if is_gopay
            else "OPLL_OAI_IS_CLIENT_OBSERVATION",
            "",
        ).strip()
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
                "oai-language": language,
                # These values match the current browser checkout contract;
                # environment overrides keep the transport forward-compatible
                # when the web deployment rotates its build identifier.
                "oai-client-build-number": (
                    os.getenv(
                        "OPLL_GOPAY_OAI_CLIENT_BUILD_NUMBER"
                        if is_gopay
                        else "OPLL_OAI_CLIENT_BUILD_NUMBER",
                        "",
                    ).strip()
                    or (GOPAY_OAI_CLIENT_BUILD_NUMBER if is_gopay else "9748354")
                ),
                "oai-client-version": (
                    os.getenv(
                        "OPLL_GOPAY_OAI_CLIENT_VERSION"
                        if is_gopay
                        else "OPLL_OAI_CLIENT_VERSION",
                        "",
                    ).strip()
                    or (
                        GOPAY_OAI_CLIENT_VERSION
                        if is_gopay
                        else "prod-1e268a33279bcedafc2fe5526bfe230880444b77"
                    )
                ),
                "x-oai-is-pending-updates": (
                    EMPTY_PENDING_UPDATES
                    if is_gopay
                    else os.getenv("OPLL_X_OAI_IS_PENDING_UPDATES", "").strip()
                    or EMPTY_PENDING_UPDATES
                ),
                # The browser keeps one observation id for a request burst;
                # callers may pin a captured value for diagnostics, while the
                # default remains fresh for every transport session.
                "x-oai-is-client-observation": observation_override
                or f"v1.r.p.{secrets.token_urlsafe(12).rstrip('=')}",
                "sec-ch-ua": (
                    str((profile or {}).get("sec_ch_ua") or "").strip()
                    if is_gopay
                    else os.getenv("OPLL_SEC_CH_UA", "").strip()
                    or '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"'
                ),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": (
                    '"Windows"'
                    if is_gopay
                    else os.getenv("OPLL_SEC_CH_UA_PLATFORM", "").strip()
                    or '"Windows"'
                ),
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "Cookie": f"oai-did={device_id}",
            }
        )
        if is_gopay:
            validate_gopay_client_hints(
                user_agent,
                str(session.headers.get("sec-ch-ua") or ""),
            )
        account = account_id(config.access_token)
        if account and not is_gopay:
            session.headers["chatgpt-account-id"] = account
        # Keep these values on the session for the browser Sentinel adapter
        # and diagnostics without putting identifiers into request URLs/logs.
        session.openai_device_id = device_id
        session.gopay_browser_profile = (profile or {}).get("name", "")
        session.gopay_tls_impersonate = impersonate or ""
        session.openai_did = device_id
        session.openai_session_id = session_id
        session.openai_proxy = proxy
        session.openai_client_observation = session.headers.get(
            "x-oai-is-client-observation", ""
        )
        session.openai_pending_receipts = _pending_receipts_from_header(
            session.headers.get("x-oai-is-pending-updates", "")
        )
        session.openai_request_started = time.perf_counter()
        session.openai_sentinel_observer_enabled = not is_gopay

        def refresh_openai_request_headers(method: str, url: str) -> dict[str, str]:
            pinned = observation_override or os.getenv(
                "OPLL_GOPAY_OAI_IS_CLIENT_OBSERVATION"
                if is_gopay
                else "OPLL_OAI_IS_CLIENT_OBSERVATION",
                "",
            ).strip()
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
                        "OPLL_GOPAY_OAI_CHECKOUT_TELEMETRY"
                        if is_gopay
                        else "OPLL_OAI_CHECKOUT_TELEMETRY",
                        "[1,null]",
                    )
                elif is_checkout_confirm or is_checkout_approve:
                    override = os.getenv(
                        "OPLL_GOPAY_OAI_APPROVE_TELEMETRY"
                        if is_gopay
                        else "OPLL_OAI_APPROVE_TELEMETRY",
                        "",
                    ).strip()
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
                        session.openai_pending_receipts = []
                        session.headers["x-oai-is-pending-updates"] = EMPTY_PENDING_UPDATES
                        dynamic["x-oai-is-pending-updates"] = EMPTY_PENDING_UPDATES
                else:
                    captured = str(
                        getattr(session, "openai_checkout_telemetry", "") or ""
                    ).strip()
                    dynamic["oai-telemetry"] = (
                        os.getenv(
                            "OPLL_GOPAY_OAI_CHECKOUT_TELEMETRY"
                            if is_gopay
                            else "OPLL_OAI_CHECKOUT_TELEMETRY",
                            "",
                        ).strip()
                        or captured
                        or browser_checkout_telemetry("checkout")
                    )
            return dynamic

        session.refresh_openai_request_headers = refresh_openai_request_headers
        # Deployment attestation is browser-generated and optional.  Never
        # bake a captured value into the repository; operators can inject a
        # fresh value for environments that enforce it.
        attestation = os.getenv(
            "OPLL_GOPAY_OAI_WEB_DEPLOYMENT_ATTESTATION"
            if is_gopay
            else "OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION",
            "",
        ).strip()
        if attestation:
            session.headers["oai-web-deployment-attestation"] = attestation
        sentinel = os.getenv(
            "OPLL_GOPAY_OPENAI_SENTINEL_TOKEN"
            if is_gopay
            else "OPLL_OPENAI_SENTINEL_TOKEN",
            "",
        ).strip()
        if sentinel:
            session.openai_sentinel_token = sentinel
        sentinel_so = os.getenv(
            "OPLL_GOPAY_OPENAI_SENTINEL_SO_TOKEN"
            if is_gopay
            else "OPLL_OPENAI_SENTINEL_SO_TOKEN",
            "",
        ).strip()
        if sentinel_so:
            session.openai_sentinel_so_token = sentinel_so
        normalized_proxy = normalize_proxy_url(proxy)
        if is_gopay:
            mode = os.getenv("OPLL_SENTINEL_BROWSER", "auto").strip().lower()
            if mode not in {"0", "false", "off", "disabled", "no"}:
                session.openai_sentinel_provider = PlaywrightSentinelProvider(
                    access_token=config.access_token,
                    device_id=device_id,
                    session_id=session_id,
                    user_agent=user_agent,
                    proxy=normalized_proxy,
                    transport_session=session,
                    session_token=str(getattr(config, "session_token", "") or ""),
                    language=language,
                    timezone=country_timezone(config),
                    promo_campaign=bool(config.gopay_zero_trial_validation),
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
            if is_gopay:
                raise
            session = new_session()
        user_agent = (
            str((profile or {}).get("user_agent") or "").strip()
            if is_gopay
            else os.getenv("OPLL_USER_AGENT", "").strip() or DEFAULT_USER_AGENT
        ) or DEFAULT_USER_AGENT
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
                    "sec-ch-ua": str(profile.get("sec_ch_ua") or "").strip(),
                    "sec-ch-ua-mobile": "?0",
                    "sec-ch-ua-platform": '"Windows"',
                }
            )
            validate_gopay_client_hints(
                user_agent,
                str(session.headers.get("sec-ch-ua") or ""),
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
