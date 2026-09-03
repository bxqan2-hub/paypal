from __future__ import annotations

import time
import uuid
from typing import Any, Protocol
from urllib.parse import quote, unquote, urlsplit, urlunsplit

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore

from ._config import DEFAULT_TIMEOUT, DEFAULT_USER_AGENT
from ..errors import ConfigurationError, NetworkError, ProtocolError
from ..logging_utils import compact_url, emit_log, safe_log_text
from ..models import ExtractionConfig

try:
    from curl_cffi.requests import Session as CurlCffiSession  # type: ignore
except ImportError:  # pragma: no cover
    CurlCffiSession = None  # type: ignore
try:
    from curl_cffi.requests import RequestException as CurlCffiRequestException  # type: ignore
except ImportError:  # pragma: no cover
    CurlCffiRequestException = None  # type: ignore
try:
    from curl_cffi.requests import HTTPError as CurlCffiHTTPError  # type: ignore
except ImportError:  # pragma: no cover
    CurlCffiHTTPError = None  # type: ignore


PAYMENT_BROWSER_IMPERSONATE = "chrome146"
PAYMENT_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
PAYMENT_BROWSER_SEC_CH_UA = '"Chromium";v="146", "Not-A.Brand";v="24", "Google Chrome";v="146"'
PAYMENT_BROWSER_SEC_CH_UA_PLATFORM = '"macOS"'


class TransportFactory(Protocol):
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any: ...
    def stripe(self, config: ExtractionConfig) -> Any: ...


def new_session(impersonate: str = PAYMENT_BROWSER_IMPERSONATE) -> Any:
    if CurlCffiSession is not None:
        session = CurlCffiSession(impersonate=impersonate)
        try:
            session.trust_env = False
        except Exception:
            pass
        return session
    if requests is None:
        raise ConfigurationError("requests is required; install requirements.txt")
    return requests.Session()


def safe_close(session: Any) -> None:
    close = getattr(session, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


def normalize_proxy_url(proxy: str) -> str:
    text = str(proxy or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        return text
    host = parsed.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    auth = ""
    if parsed.username is not None:
        auth = quote(unquote(parsed.username), safe="%")
        if parsed.password is not None:
            auth += ":" + quote(unquote(parsed.password), safe="%")
        auth += "@"
    port = f":{parsed.port}" if parsed.port else ""
    return urlunsplit((parsed.scheme, auth + host + port, parsed.path, parsed.query, parsed.fragment))


def set_proxy_url(session: Any, proxy: str) -> None:
    normalized = normalize_proxy_url(proxy)
    session.proxies = {"http": normalized, "https": normalized} if normalized else {}


def stage_http_request(session: Any, stage: str, method: str, url: str, log: Any | None = None, **kwargs: Any) -> Any:
    started = time.perf_counter()
    emit_log(log, f"{stage}: {method.upper()} {compact_url(url)}")
    try:
        response = session.request(method.upper(), url, **kwargs)
    except Exception as exc:
        detail = safe_log_text(exc)
        emit_log(log, f"{stage}: request error={detail}")
        if is_network_exception(exc):
            raise NetworkError(stage, detail) from exc
        raise
    emit_log(log, f"{stage}: HTTP {response.status_code} elapsed={time.perf_counter() - started:.2f}s")
    return response


def is_network_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    if requests is not None and isinstance(exc, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError)):
        return True
    if CurlCffiRequestException is not None and isinstance(exc, CurlCffiRequestException):
        if CurlCffiHTTPError is not None and isinstance(exc, CurlCffiHTTPError):
            return False
        return type(exc).__name__ in {"ConnectionError", "ConnectTimeout", "ProxyError", "ReadTimeout", "SSLError", "Timeout"}
    return False


def response_json(response: Any, stage: str) -> dict[str, Any]:
    try:
        payload = response.json() or {}
    except Exception as exc:
        raise ProtocolError(502, f"{stage} invalid json: {safe_log_text(exc)}") from exc
    if not isinstance(payload, dict):
        raise ProtocolError(502, f"{stage} returned non-object json")
    return payload


class DefaultTransportFactory:
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any:
        device_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        session = new_session(PAYMENT_BROWSER_IMPERSONATE)
        session.headers.update(
            {
                "User-Agent": PAYMENT_BROWSER_USER_AGENT,
                "Accept": "*/*",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
                "Authorization": f"Bearer {config.access_token}",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Content-Type": "application/json",
                "oai-device-id": device_id,
                "oai-session-id": session_id,
                "oai-language": "vi-VN",
                "oai-client-build-number": "9999461",
                "oai-client-version": "prod-d040bc6b02dd2a27b54e1d7c56d181a795593f41",
                "sec-ch-ua": PAYMENT_BROWSER_SEC_CH_UA,
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": PAYMENT_BROWSER_SEC_CH_UA_PLATFORM,
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "Cookie": f"oai-did={device_id}",
            }
        )
        set_proxy_url(session, proxy)
        return session

    def stripe(self, config: ExtractionConfig) -> Any:
        session = new_session(PAYMENT_BROWSER_IMPERSONATE)
        session.headers.update({"User-Agent": PAYMENT_BROWSER_USER_AGENT, "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7"})
        set_proxy_url(session, config.checkout_proxy)
        return session


def country_locale(config: ExtractionConfig) -> str:
    return "vi-VN"
