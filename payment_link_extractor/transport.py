from __future__ import annotations

import os
import base64
import json
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


def new_session() -> Any:
    if CurlCffiSession is not None:
        return CurlCffiSession(impersonate=os.getenv("OPLL_HTTP_IMPERSONATE", "chrome"))
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
) -> dict[str, str]:
    """Build fresh Sentinel/attestation headers for a browser checkout.

    A live browser provider is preferred for the two protected checkout
    operations.  The environment/session fallback remains available for
    deployments that deliberately inject their own short-lived values and for
    older non-GCash flows.
    """
    headers: dict[str, str] = {}
    provider = getattr(session, "openai_sentinel_provider", None)
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
    token = openai_sentinel_token(session)
    if token:
        headers["OpenAI-Sentinel-Token"] = token
    so_token = openai_sentinel_so_token(session)
    if so_token:
        headers["OpenAI-Sentinel-SO-Token"] = so_token
    return headers


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
        log: Any | None = None,
    ) -> None:
        self.access_token = str(access_token or "").strip()
        self.device_id = str(device_id or "").strip()
        self.session_id = str(session_id or "").strip()
        self.user_agent = str(user_agent or DEFAULT_USER_AGENT)
        self.proxy = str(proxy or "").strip()
        self.transport_session = transport_session
        self.log = log
        self.binary = _agent_browser_binary()
        self.namespace = "opll_sentinel_" + uuid.uuid4().hex[:12]
        self.session_name = "checkout_" + uuid.uuid4().hex[:12]
        self.temp_dir = Path(tempfile.mkdtemp(prefix="opll-sentinel-"))
        self.locale_script = self.temp_dir / "locale.js"
        self.locale_script.write_text(
            "Object.defineProperty(navigator, 'language', {get: () => 'en-PH'});"
            "Object.defineProperty(navigator, 'languages', {get: () => ['en-PH', 'en']});",
            encoding="utf-8",
        )
        self._lock = threading.RLock()
        self._started = False
        self._closed = False
        self._failed = False
        self._attestation = ""
        self._cookies = ""
        self._launch_args_used = False

    @property
    def enabled(self) -> bool:
        mode = os.getenv("OPLL_GCASH_SENTINEL_BROWSER", "auto").strip().lower()
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
        ]
        if self.proxy:
            command.extend(["--proxy", self.proxy])
        if not self._launch_args_used:
            command.extend(["--args", "--disable-blink-features=AutomationControlled"])
        return command

    def _run(self, args: list[str], timeout: float = 75.0) -> Any:
        if self._closed:
            raise RuntimeError("Sentinel browser provider is closed")
        output_path = self.temp_dir / ("command-" + uuid.uuid4().hex + ".out")
        env = dict(os.environ)
        # Chromium uses TZ when constructing the browser fingerprint.  Keep
        # the browser and the PH checkout locale coherent when supported.
        env.setdefault("TZ", "Asia/Manila")
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
            raise RuntimeError(f"agent-browser exited with status {status}")
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
        if pairs:
            self._cookies = "; ".join(pairs)
            headers = getattr(self.transport_session, "headers", None)
            if headers is not None:
                headers["Cookie"] = self._cookies

    def _start(self) -> None:
        if not self.enabled:
            raise RuntimeError("agent-browser is disabled or unavailable")
        try:
            self._run(["open", "about:blank", "--json"])
            self._started = True
            self._launch_args_used = True
            self._run(
                [
                    "cookies",
                    "set",
                    "oai-did",
                    self.device_id,
                    "--url",
                    "https://chatgpt.com",
                ]
            )
            self._run(["open", "https://chatgpt.com/backend-api/sentinel/frame.html"])
            # The bootstrap document carries the current signed deployment
            # attestation.  The initial navigation is authenticated when an
            # AT is available; an unauthenticated page simply leaves this
            # optional field empty while token generation still remains useful.
            auth_headers = {
                "Authorization": f"Bearer {self.access_token}",
                "oai-device-id": self.device_id,
                "oai-session-id": self.session_id,
                "oai-language": "en-US",
                # The current browser HAR (m.gcash.com111.har) uses the
                # deployed checkout build below.  Keep both values overridable
                # because the web client rotates them independently of the
                # payment flow.
                "oai-client-build-number": os.getenv("OPLL_OAI_CLIENT_BUILD_NUMBER", "9748354"),
                "oai-client-version": os.getenv(
                    "OPLL_OAI_CLIENT_VERSION",
                    "prod-1e268a33279bcedafc2fe5526bfe230880444b77",
                ),
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
            self._run(["open", "https://chatgpt.com/backend-api/sentinel/frame.html"])
            self._sync_cookies()
        except Exception:
            self._failed = True
            raise

    def _ping(self, referer: str) -> None:
        # The SDK itself uses a zero-length POST immediately before protected
        # checkout calls.  Running it in the same browser context keeps the
        # Cloudflare/Sentinel cookies on the matching proxy IP.
        expression = (
            "(async()=>{const r=await fetch('/backend-api/sentinel/ping',"
            "{method:'POST',credentials:'include',referrer:" + json.dumps(referer or "https://chatgpt.com/") +
            ",headers:{'Content-Type':'text/plain;charset=UTF-8'}});return r.status})()"
        )
        self._eval(expression, timeout=30)

    def headers(self, flow: str, *, referer: str = "") -> dict[str, str]:
        with self._lock:
            if self._failed:
                raise RuntimeError("Sentinel browser provider failed during startup")
            if not self._started:
                self._start()
            self._ping(referer)
            raw = self._eval(
                "(async()=>{const token=await SentinelSDK.token(" + json.dumps(flow) + ");return token})()",
                timeout=90,
            )
            if isinstance(raw, str):
                token = raw
            elif isinstance(raw, dict):
                token = json.dumps(raw, separators=(",", ":"))
            else:
                token = ""
            if not token:
                raise RuntimeError("SentinelSDK returned an empty token")
            result = {"OpenAI-Sentinel-Token": token}
            if self._attestation:
                result["oai-web-deployment-attestation"] = self._attestation
                session_headers = getattr(self.transport_session, "headers", None)
                if session_headers is not None:
                    session_headers["oai-web-deployment-attestation"] = self._attestation
            if self._cookies:
                result["Cookie"] = self._cookies
            return result

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


class DefaultTransportFactory:
    def chatgpt(self, config: ExtractionConfig, proxy: str) -> Any:
        device_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        session = new_session()
        user_agent = os.getenv("OPLL_USER_AGENT", DEFAULT_USER_AGENT).strip() or DEFAULT_USER_AGENT
        observation_override = os.getenv("OPLL_OAI_IS_CLIENT_OBSERVATION", "").strip()
        session.headers.update(
            {
                "User-Agent": user_agent,
                "Accept": "*/*",
                "Accept-Language": f"{country_locale(config)},en;q=0.9",
                "Authorization": f"Bearer {config.access_token}",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Content-Type": "application/json",
                "oai-device-id": device_id,
                "oai-session-id": session_id,
                # ChatGPT's API language is the browser UI locale; the
                # country-specific Accept-Language remains separate above.
                "oai-language": os.getenv("OPLL_OAI_LANGUAGE", "en-US"),
                # These values match the current browser checkout contract;
                # environment overrides keep the transport forward-compatible
                # when the web deployment rotates its build identifier.
                "oai-client-build-number": os.getenv("OPLL_OAI_CLIENT_BUILD_NUMBER", "9748354"),
                "oai-client-version": os.getenv(
                    "OPLL_OAI_CLIENT_VERSION",
                    "prod-1e268a33279bcedafc2fe5526bfe230880444b77",
                ),
                "x-oai-is-pending-updates": os.getenv(
                    "OPLL_X_OAI_IS_PENDING_UPDATES", '{"v":3,"updates":[]}'
                ),
                # The browser keeps one observation id for a request burst;
                # callers may pin a captured value for diagnostics, while the
                # default remains fresh for every transport session.
                "x-oai-is-client-observation": os.getenv(
                    "OPLL_OAI_IS_CLIENT_OBSERVATION",
                    f"v1.r.p.{secrets.token_urlsafe(12).rstrip('=')}",
                ),
                "sec-ch-ua": os.getenv(
                    "OPLL_SEC_CH_UA",
                    '"Not=A?Brand";v="99", "Google Chrome";v="151", "Chromium";v="151"',
                ),
                "sec-ch-ua-mobile": "?0",
                "sec-ch-ua-platform": os.getenv("OPLL_SEC_CH_UA_PLATFORM", '"Windows"'),
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
                "Cookie": f"oai-did={device_id}",
            }
        )
        account = account_id(config.access_token)
        if account:
            session.headers["chatgpt-account-id"] = account
        # Keep these values on the session for the GCash Sentinel adapter and
        # for diagnostics without putting identifiers into request URLs/logs.
        session.openai_device_id = device_id
        session.openai_did = device_id
        session.openai_proxy = proxy
        session.openai_client_observation = session.headers.get(
            "x-oai-is-client-observation", ""
        )
        session.openai_request_started = time.perf_counter()

        def refresh_openai_request_headers(method: str, url: str) -> dict[str, str]:
            pinned = observation_override or os.getenv("OPLL_OAI_IS_CLIENT_OBSERVATION", "").strip()
            observation = pinned or f"v1.r.p.{secrets.token_urlsafe(12).rstrip('=')}"
            session.openai_client_observation = observation
            session.headers["x-oai-is-client-observation"] = observation
            dynamic: dict[str, str] = {"x-oai-is-client-observation": observation}
            normalized_url = str(url or "").lower()
            if method.upper() == "POST" and (
                normalized_url.endswith("/backend-api/payments/checkout")
                or normalized_url.endswith("/backend-api/payments/checkout/confirm")
            ):
                if normalized_url.endswith("/confirm"):
                    elapsed = round((time.perf_counter() - session.openai_request_started) * 1000, 1)
                    values = [
                        1,
                        elapsed,
                        secrets.randbelow(200),
                        secrets.randbelow(200),
                        secrets.randbelow(120),
                        2,
                        0,
                        round(elapsed + secrets.randbelow(30), 1),
                    ]
                    dynamic["oai-telemetry"] = json.dumps(values, separators=(",", ":"))
                else:
                    dynamic["oai-telemetry"] = os.getenv("OPLL_OAI_CHECKOUT_TELEMETRY", "[1,null]")
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
        if normalize_payment_method(config.payment_method) == "gcash":
            mode = os.getenv("OPLL_GCASH_SENTINEL_BROWSER", "auto").strip().lower()
            if mode not in {"0", "false", "off", "disabled", "no"}:
                session.openai_sentinel_provider = BrowserSentinelProvider(
                    access_token=config.access_token,
                    device_id=device_id,
                    session_id=session_id,
                    user_agent=user_agent,
                    proxy=normalized_proxy,
                    transport_session=session,
                )
        session.proxies = (
            {"http": normalized_proxy, "https": normalized_proxy}
            if normalized_proxy
            else {}
        )
        return session

    def stripe(self, config: ExtractionConfig) -> Any:
        session = new_session()
        session.headers.update(
            {
                "User-Agent": os.getenv("OPLL_USER_AGENT", DEFAULT_USER_AGENT),
                "Accept-Language": f"{country_locale(config)},en;q=0.9",
            }
        )
        set_proxy_url(session, config.checkout_proxy)
        return session


def country_locale(config: ExtractionConfig) -> str:
    # Config is normalized before a transport is created. Keep this helper
    # dependency-free so fake factories can use the same interface.
    from .config import country_config

    return country_config(config.country)[2]
