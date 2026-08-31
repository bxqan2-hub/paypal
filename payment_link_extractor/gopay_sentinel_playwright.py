from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine
from urllib.parse import unquote, urlsplit

try:
    from playwright.async_api import BrowserContext, Page, async_playwright
except ImportError:  # pragma: no cover - dependency is validated at runtime
    BrowserContext = Any  # type: ignore[misc,assignment]
    Page = Any  # type: ignore[misc,assignment]
    async_playwright = None  # type: ignore[assignment]


SENTINEL_SDK_VERSION = "20260810913b"
CHATGPT_ORIGIN = "https://chatgpt.com"


def _enabled(value: str, *, default: bool) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "off", "disabled", "no"}


def _profile_root() -> Path:
    configured = os.getenv("OPLL_GOPAY_SENTINEL_PROFILE_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[1] / "data" / "gopay-sentinel-profiles").resolve()


def _profile_key(device_id: str) -> str:
    try:
        return str(uuid.UUID(str(device_id)))
    except (TypeError, ValueError):
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"gopay-profile:{device_id}"))


def _proxy_options(proxy_url: str) -> dict[str, str] | None:
    text = str(proxy_url or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.hostname:
        raise ValueError("browser proxy URL is invalid")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    result = {"server": f"{parsed.scheme}://{host}{port}"}
    if parsed.username is not None:
        result["username"] = unquote(parsed.username)
    if parsed.password is not None:
        result["password"] = unquote(parsed.password)
    return result


def _cookie_header(cookies: list[dict[str, Any]]) -> str:
    return "; ".join(
        f"{str(item.get('name') or '')}={str(item.get('value') or '')}"
        for item in cookies
        if str(item.get("name") or "").strip()
    )


@dataclass
class _BrowserSession:
    context: BrowserContext
    page: Page
    profile_path: Path
    device_id: str
    attestation: str = ""
    request_events: list[dict[str, Any]] = field(default_factory=list)
    latest_receipt: str = ""
    challenge_shapes: list[dict[str, Any]] = field(default_factory=list)
    sdk_sha256: str = ""
    bootstrap_headers: dict[str, str] = field(default_factory=dict)
    active_page_url: str = ""


class PersistentPlaywrightDaemon:
    """One daemon thread owns Playwright, its event loop and live browsers."""

    def __init__(self) -> None:
        if async_playwright is None:
            raise RuntimeError("playwright is not installed")
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._playwright: Any = None
        self._sessions: dict[str, _BrowserSession] = {}
        self._thread = threading.Thread(
            target=self._thread_main,
            name="gopay-sentinel-playwright",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("Playwright daemon did not start")
        if self._startup_error is not None:
            raise RuntimeError("Playwright daemon startup failed") from self._startup_error

    def _thread_main(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._playwright = self._loop.run_until_complete(async_playwright().start())
        except BaseException as exc:  # pragma: no cover - platform startup
            self._startup_error = exc
            self._ready.set()
            return
        self._ready.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._shutdown_async())
            self._loop.close()

    def _call(self, coro: Coroutine[Any, Any, Any], timeout: float) -> Any:
        if self._startup_error is not None or not self._thread.is_alive():
            raise RuntimeError("Playwright daemon is unavailable")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    async def _capture_request(self, session_id: str, request: Any) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            parsed = urlsplit(request.url)
            headers = await request.all_headers()
        except Exception:
            return
        attestation = str(headers.get("oai-web-deployment-attestation") or "").strip()
        if attestation:
            session.attestation = attestation
        if parsed.path in {
            "/backend-api/sentinel/req",
            "/backend-api/sentinel/ping",
        }:
            session.request_events.append(
                {
                    "path": parsed.path,
                    "method": request.method,
                    "body_length": len(request.post_data or ""),
                    "referer_kind": (
                        "checkout"
                        if "/checkout/" in str(headers.get("referer") or "")
                        else "frame"
                        if "/sentinel/frame.html" in str(headers.get("referer") or "")
                        else "other"
                    ),
                }
            )

    async def _capture_response(self, session_id: str, response: Any) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            response_url = response.url
            headers = await response.all_headers()
        except Exception:
            return
        path = urlsplit(response_url).path
        receipt = str(headers.get("x-oai-is-receipt") or "").strip()
        if receipt:
            session.latest_receipt = receipt
        if path == "/backend-api/sentinel/req":
            try:
                payload = await response.json()
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                turnstile = payload.get("turnstile")
                turnstile = turnstile if isinstance(turnstile, dict) else {}
                proof = payload.get("proofofwork")
                proof = proof if isinstance(proof, dict) else {}
                session.challenge_shapes.append(
                    {
                        "token_length": len(str(payload.get("token") or "")),
                        "turnstile_required": bool(turnstile.get("required")),
                        "turnstile_dx_length": len(str(turnstile.get("dx") or "")),
                        "pow_required": bool(proof.get("required")),
                        "pow_seed_length": len(str(proof.get("seed") or "")),
                        "pow_difficulty_length": len(
                            str(proof.get("difficulty") or "")
                        ),
                    }
                )
        if path == f"/sentinel/{SENTINEL_SDK_VERSION}/sdk.js":
            try:
                session.sdk_sha256 = hashlib.sha256(await response.body()).hexdigest()
            except Exception:
                pass

    async def _install_sdk(self, page: Page) -> None:
        present = await page.evaluate(
            "() => typeof window.SentinelSDK?.init === 'function' && typeof window.SentinelSDK?.token === 'function' && typeof window.SentinelSDK?.sessionObserverToken === 'function'"
        )
        if present:
            return
        await page.add_script_tag(
            url=f"{CHATGPT_ORIGIN}/backend-api/sentinel/sdk.js"
        )
        await page.wait_for_function(
            "() => typeof window.SentinelSDK?.init === 'function' && typeof window.SentinelSDK?.token === 'function' && typeof window.SentinelSDK?.sessionObserverToken === 'function'",
            timeout=30_000,
        )

    async def _capture_page_attestation(self, session: _BrowserSession) -> None:
        try:
            value = await session.page.evaluate(
                """() => {
                    const direct = window.__reactRouterContext?.state?.loaderData?.root?.clientBootstrap?.webDeploymentAttestation;
                    if (direct) return String(direct);
                    const node = document.getElementById('client-bootstrap');
                    if (node) {
                        try {
                            const data = JSON.parse(node.textContent || '{}');
                            if (data.webDeploymentAttestation) return String(data.webDeploymentAttestation);
                        } catch (_) {}
                    }
                    return '';
                }"""
            )
        except Exception:
            value = ""
        if value:
            session.attestation = str(value)

    async def _open_session_async(
        self,
        *,
        access_token: str,
        account_id: str,
        device_id: str,
        session_id: str,
        user_agent: str,
        browser_proxy: str,
        session_token: str,
        language: str,
        timezone: str,
    ) -> dict[str, Any]:
        runtime_id = uuid.uuid4().hex
        profile_path = _profile_root() / _profile_key(device_id)
        profile_path.mkdir(parents=True, exist_ok=True)
        headless = _enabled(os.getenv("OPLL_SENTINEL_HEADLESS", ""), default=False)
        browser_channel = os.getenv(
            "OPLL_GOPAY_SENTINEL_BROWSER_CHANNEL", "chrome"
        ).strip()
        launch_options: dict[str, Any] = {
            "user_data_dir": str(profile_path),
            "headless": headless,
            "proxy": _proxy_options(browser_proxy),
            "user_agent": user_agent,
            "locale": language,
            "timezone_id": timezone,
            "viewport": {"width": 1365, "height": 768},
            "bypass_csp": True,
            "ignore_https_errors": False,
            "args": ["--no-first-run", "--no-default-browser-check"],
        }
        if browser_channel:
            launch_options["channel"] = browser_channel
        try:
            context = await self._playwright.chromium.launch_persistent_context(
                **launch_options
            )
        except Exception:
            if not browser_channel:
                raise
            launch_options.pop("channel", None)
            context = await self._playwright.chromium.launch_persistent_context(
                **launch_options
            )
        browser_version = (
            str(context.browser.version)
            if getattr(context, "browser", None) is not None
            else ""
        )
        (profile_path / "device-profile.json").write_text(
            json.dumps(
                {
                    "device_id": device_id,
                    "language": language,
                    "timezone": timezone,
                    "user_agent": user_agent,
                    "browser_channel": browser_channel or "bundled-chromium",
                    "browser_version": browser_version,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        page = context.pages[0] if context.pages else await context.new_page()
        session = _BrowserSession(
            context=context,
            page=page,
            profile_path=profile_path,
            device_id=device_id,
        )
        self._sessions[runtime_id] = session
        context.on(
            "request",
            lambda request: asyncio.create_task(self._capture_request(runtime_id, request)),
        )
        context.on(
            "response",
            lambda response: asyncio.create_task(self._capture_response(runtime_id, response)),
        )
        cookies = [
            {
                "name": "oai-did",
                "value": device_id,
                "domain": ".chatgpt.com",
                "path": "/",
                "secure": True,
            }
        ]
        if session_token:
            cookies.append(
                {
                    "name": "__Secure-next-auth.session-token",
                    "value": session_token,
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            )
        await context.add_cookies(cookies)
        bootstrap_headers = {
            "Authorization": f"Bearer {access_token}",
            "oai-device-id": device_id,
            "oai-session-id": session_id,
            "oai-language": language,
            "oai-client-build-number": os.getenv(
                "OPLL_OAI_CLIENT_BUILD_NUMBER", "10012890"
            ),
            "oai-client-version": os.getenv(
                "OPLL_OAI_CLIENT_VERSION",
                "prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4",
            ),
        }
        if account_id:
            bootstrap_headers["chatgpt-account-id"] = account_id
        session.bootstrap_headers = dict(bootstrap_headers)
        await context.set_extra_http_headers(bootstrap_headers)
        try:
            await page.goto(
                f"{CHATGPT_ORIGIN}/?promo_campaign=plus-1-month-free",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
        finally:
            # Sentinel req/ping in both HARs are cookie/browser requests and do
            # not carry the API Authorization header.
            await context.set_extra_http_headers({})
        await self._install_sdk(page)
        await self._capture_page_attestation(session)
        await asyncio.sleep(0.15)
        browser_cookies = await context.cookies(CHATGPT_ORIGIN)
        return {
            "runtime_id": runtime_id,
            "attestation": session.attestation,
            "cookie_header": _cookie_header(browser_cookies),
            "latest_receipt": session.latest_receipt,
            "challenge_shapes": list(session.challenge_shapes),
            "sdk_sha256": session.sdk_sha256,
            "profile_path": str(profile_path),
            "headless": headless,
            "browser_channel": browser_channel or "bundled-chromium",
            "browser_version": browser_version,
        }

    def open_session(self, **kwargs: Any) -> dict[str, Any]:
        return self._call(self._open_session_async(**kwargs), timeout=150)

    async def _set_page_url(self, session: _BrowserSession, page_url: str) -> None:
        parsed = urlsplit(str(page_url or CHATGPT_ORIGIN))
        if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
            raise ValueError("Sentinel page URL must remain on chatgpt.com")
        relative = parsed.path or "/"
        if parsed.query:
            relative += "?" + parsed.query
        if parsed.fragment:
            relative += "#" + parsed.fragment
        navigate_checkout = _enabled(
            os.getenv("OPLL_GOPAY_SENTINEL_NAVIGATE_CHECKOUT", ""),
            default=True,
        )
        current = urlsplit(session.page.url)
        if (
            navigate_checkout
            and parsed.path.startswith("/checkout/")
            and session.active_page_url != parsed.geturl()
        ):
            await session.context.set_extra_http_headers(session.bootstrap_headers)
            try:
                await session.page.goto(
                    parsed.geturl(),
                    wait_until="domcontentloaded",
                    timeout=90_000,
                )
            finally:
                await session.context.set_extra_http_headers({})
            await self._install_sdk(session.page)
            await self._capture_page_attestation(session)
            # Auth-only AT sessions may be redirected while the checkout shell
            # loads. Restore the exact Checkout URL before SDK.init so its
            # zero-length ping carries the HAR checkout Referer.
            await session.page.evaluate(
                "url => history.replaceState({}, '', url)",
                relative,
            )
            session.active_page_url = parsed.geturl()
            return
        await session.page.evaluate(
            "url => history.replaceState({}, '', url)",
            relative,
        )

    async def _prepare_flow_async(
        self, runtime_id: str, flow: str, page_url: str
    ) -> dict[str, Any]:
        session = self._sessions[runtime_id]
        await self._set_page_url(session, page_url)
        await self._install_sdk(session.page)
        before = len(session.request_events)
        await session.page.evaluate(
            "async flow => { await window.SentinelSDK.init(flow); return true; }",
            flow,
        )
        await asyncio.sleep(0.1)
        cookies = await session.context.cookies(CHATGPT_ORIGIN)
        return {
            "attestation": session.attestation,
            "cookie_header": _cookie_header(cookies),
            "latest_receipt": session.latest_receipt,
            "request_events": session.request_events[before:],
            "challenge_shapes": list(session.challenge_shapes),
            "sdk_sha256": session.sdk_sha256,
        }

    def prepare_flow(self, runtime_id: str, flow: str, page_url: str) -> dict[str, Any]:
        return self._call(
            self._prepare_flow_async(runtime_id, flow, page_url), timeout=120
        )

    async def _token_async(
        self, runtime_id: str, flow: str, page_url: str
    ) -> dict[str, Any]:
        session = self._sessions[runtime_id]
        await self._set_page_url(session, page_url)
        await self._install_sdk(session.page)
        before = len(session.request_events)
        started = time.perf_counter()
        generated = await session.page.evaluate(
            """async flow => {
                const token = await window.SentinelSDK.token(flow);
                const timing = typeof window.SentinelSDK.timing === 'function'
                    ? window.SentinelSDK.timing()
                    : null;
                return {token, timing};
            }""",
            flow,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        cookies = await session.context.cookies(CHATGPT_ORIGIN)
        return {
            "token": generated.get("token") if isinstance(generated, dict) else "",
            "timing": generated.get("timing") if isinstance(generated, dict) else None,
            "attestation": session.attestation,
            "cookie_header": _cookie_header(cookies),
            "latest_receipt": session.latest_receipt,
            "request_events": session.request_events[before:],
            "elapsed_ms": elapsed_ms,
            "challenge_shapes": list(session.challenge_shapes),
            "sdk_sha256": session.sdk_sha256,
        }

    def token(self, runtime_id: str, flow: str, page_url: str) -> dict[str, Any]:
        return self._call(self._token_async(runtime_id, flow, page_url), timeout=120)

    async def _set_cookie_async(
        self, runtime_id: str, name: str, value: str, http_only: bool
    ) -> dict[str, Any]:
        session = self._sessions[runtime_id]
        await session.context.add_cookies(
            [
                {
                    "name": str(name),
                    "value": str(value),
                    "domain": ".chatgpt.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": bool(http_only),
                }
            ]
        )
        cookies = await session.context.cookies(CHATGPT_ORIGIN)
        return {"cookie_header": _cookie_header(cookies)}

    def set_cookie(
        self, runtime_id: str, name: str, value: str, http_only: bool
    ) -> dict[str, Any]:
        return self._call(
            self._set_cookie_async(runtime_id, name, value, http_only), timeout=30
        )

    async def _close_session_async(self, runtime_id: str) -> None:
        session = self._sessions.pop(runtime_id, None)
        if session is not None:
            await session.context.close()

    def close_session(self, runtime_id: str) -> None:
        if runtime_id:
            self._call(self._close_session_async(runtime_id), timeout=30)

    async def _shutdown_async(self) -> None:
        for runtime_id in list(self._sessions):
            try:
                await self._close_session_async(runtime_id)
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:
                pass

    def shutdown(self) -> None:
        if not self._thread.is_alive():
            return
        for runtime_id in list(self._sessions):
            try:
                self.close_session(runtime_id)
            except Exception:
                pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=10)


_DAEMON: PersistentPlaywrightDaemon | None = None
_DAEMON_LOCK = threading.Lock()


def get_playwright_daemon() -> PersistentPlaywrightDaemon:
    global _DAEMON
    with _DAEMON_LOCK:
        if _DAEMON is None:
            _DAEMON = PersistentPlaywrightDaemon()
        return _DAEMON


def shutdown_playwright_daemon() -> None:
    global _DAEMON
    with _DAEMON_LOCK:
        daemon = _DAEMON
        _DAEMON = None
    if daemon is not None:
        daemon.shutdown()


atexit.register(shutdown_playwright_daemon)
