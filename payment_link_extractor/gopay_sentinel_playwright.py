from __future__ import annotations

import asyncio
import atexit
import hashlib
import json
import os
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Coroutine
from urllib.parse import unquote, urlsplit

try:
    from playwright.async_api import (
        BrowserContext,
        Page,
        TimeoutError as PlaywrightTimeoutError,
        async_playwright,
    )
except ImportError:  # pragma: no cover - dependency is validated at runtime
    BrowserContext = Any  # type: ignore[misc,assignment]
    Page = Any  # type: ignore[misc,assignment]
    PlaywrightTimeoutError = TimeoutError
    async_playwright = None  # type: ignore[assignment]

from .gopay_transport import GOPAY_OAI_CLIENT_BUILD_NUMBER, GOPAY_OAI_CLIENT_VERSION


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


def _nextauth_cookie_records(session_token: str) -> list[dict[str, Any]]:
    value = str(session_token or "")
    if not value:
        return []
    base = "__Secure-next-auth.session-token"
    chunk_size = 3800
    chunks = [value[index : index + chunk_size] for index in range(0, len(value), chunk_size)]
    if len(chunks) == 1 and len(base) + len(chunks[0]) <= 4096:
        names = [(base, chunks[0])]
    else:
        names = [(f"{base}.{index}", chunk) for index, chunk in enumerate(chunks)]
    return [
        {
            "name": name,
            "value": chunk,
            "domain": ".chatgpt.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
        }
        for name, chunk in names
        if chunk
    ]


def _browser_version_matches(user_agent: str, browser_version: str) -> bool:
    user_agent_match = re.search(r"(?:Chrome|Chromium)/(\d+)\.", str(user_agent or ""), re.I)
    browser_version_match = re.match(r"(\d+)\.", str(browser_version or ""))
    return bool(
        user_agent_match
        and browser_version_match
        and user_agent_match.group(1) == browser_version_match.group(1)
    )


@dataclass
class _BrowserSession:
    context: BrowserContext
    page: Page
    profile_path: Path
    device_id: str
    session_id: str = ""
    attestation: str = ""
    request_events: list[dict[str, Any]] = field(default_factory=list)
    latest_receipt: str = ""
    challenge_shapes: list[dict[str, Any]] = field(default_factory=list)
    sdk_sha256: str = ""
    bootstrap_headers: dict[str, str] = field(default_factory=dict)
    active_page_url: str = ""
    capture_tasks: set[asyncio.Task[Any]] = field(default_factory=set)


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
        try:
            return future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            raise

    async def _capture_request(self, session_id: str, request: Any) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            parsed = urlsplit(request.url)
            headers = await request.all_headers()
        except Exception:
            return
        if parsed.scheme != "https" or parsed.netloc not in {
            "chatgpt.com",
            "www.chatgpt.com",
        }:
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
        parsed = urlsplit(response_url)
        if parsed.scheme != "https" or parsed.netloc not in {
            "chatgpt.com",
            "www.chatgpt.com",
        }:
            return
        path = parsed.path
        receipt = (
            str(headers.get("x-oai-is-receipt") or "").strip()
            if path.startswith("/backend-api/payments/")
            else ""
        )
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

    async def _settle_capture_tasks(self, session: _BrowserSession) -> None:
        for _ in range(3):
            tasks = tuple(session.capture_tasks)
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _install_sdk(self, page: Page) -> None:
        present = await page.evaluate(
            "() => typeof window.SentinelSDK?.init === 'function' && typeof window.SentinelSDK?.token === 'function' && typeof window.SentinelSDK?.sessionObserverToken === 'function'"
        )
        if present:
            return
        await page.add_script_tag(
            url=f"{CHATGPT_ORIGIN}/sentinel/{SENTINEL_SDK_VERSION}/sdk.js"
        )
        await page.wait_for_function(
            "() => typeof window.SentinelSDK?.init === 'function' && typeof window.SentinelSDK?.token === 'function' && typeof window.SentinelSDK?.sessionObserverToken === 'function'",
            timeout=30_000,
        )

    async def _capture_page_attestation(self, session: _BrowserSession) -> None:
        try:
            value = await session.page.evaluate(
                """() => {
                    const direct = window.__reactRouterContext?.state?.loaderData?.root?.clientBootstrap;
                    let attestation = String(direct?.webDeploymentAttestation || '');
                    let sessionId = String(direct?.sessionId || '');
                    const node = document.getElementById('client-bootstrap');
                    if (node) {
                        try {
                            const data = JSON.parse(node.textContent || '{}');
                            attestation = attestation || String(data.webDeploymentAttestation || '');
                            sessionId = sessionId || String(data.sessionId || '');
                        } catch (_) {}
                    }
                    return {attestation, sessionId};
                }"""
            )
        except Exception:
            value = {}
        if isinstance(value, dict):
            attestation = str(value.get("attestation") or "").strip()
            if attestation:
                session.attestation = attestation
                headers = getattr(session, "bootstrap_headers", None)
                if isinstance(headers, dict):
                    headers["oai-web-deployment-attestation"] = attestation
            candidate = str(value.get("sessionId") or "").strip()
            try:
                session.session_id = str(uuid.UUID(candidate))
            except (TypeError, ValueError):
                pass
            if session.session_id:
                headers = getattr(session, "bootstrap_headers", None)
                if isinstance(headers, dict):
                    headers["oai-session-id"] = session.session_id

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
        promo_campaign: bool = True,
    ) -> dict[str, Any]:
        runtime_id = uuid.uuid4().hex
        profile_path = _profile_root() / _profile_key(device_id)
        profile_path.mkdir(parents=True, exist_ok=True)
        # Sentinel still runs in the same persistent Playwright context, but
        # the Chromium window stays hidden unless explicitly disabled.
        headless = _enabled(os.getenv("OPLL_SENTINEL_HEADLESS", ""), default=True)
        browser_channel = os.getenv(
            "OPLL_GOPAY_SENTINEL_BROWSER_CHANNEL", "chrome"
        ).strip() or "chrome"
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
        context = await self._playwright.chromium.launch_persistent_context(
            **launch_options
        )
        browser_version = (
            str(context.browser.version)
            if getattr(context, "browser", None) is not None
            else ""
        )
        user_agent_match = re.search(r"(?:Chrome|Chromium)/(\d+)\.", user_agent, re.I)
        if not _browser_version_matches(user_agent, browser_version):
            await context.close()
            raise RuntimeError(
                f"GoPay browser version mismatch: browser={browser_version or '?'}, ua={user_agent_match.group(1) if user_agent_match else '?'}"
            )
        try:
            existing_cookies = await context.cookies(CHATGPT_ORIGIN)
        except Exception:
            existing_cookies = []
        if not isinstance(existing_cookies, list):
            existing_cookies = []
        cookie_backed = any(
            (
                str(item.get("name") or "") == "__Secure-next-auth.session-token"
                or str(item.get("name") or "").startswith(
                    "__Secure-next-auth.session-token."
                )
            )
            and str(item.get("value") or "").strip()
            for item in existing_cookies
            if isinstance(item, dict)
        )
        has_valid_device_cookie = False
        for item in existing_cookies:
            if str(item.get("name") or "").strip().lower() != "oai-did":
                continue
            candidate = str(item.get("value") or "").strip()
            try:
                uuid.UUID(candidate)
            except (TypeError, ValueError):
                continue
            device_id = candidate
            has_valid_device_cookie = True
            break
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
            session_id=session_id,
        )
        self._sessions[runtime_id] = session
        def on_request(request: Any) -> None:
            task = asyncio.create_task(self._capture_request(runtime_id, request))
            session.capture_tasks.add(task)
            task.add_done_callback(session.capture_tasks.discard)

        def on_response(response: Any) -> None:
            task = asyncio.create_task(self._capture_response(runtime_id, response))
            session.capture_tasks.add(task)
            task.add_done_callback(session.capture_tasks.discard)

        try:
            context.on("request", on_request)
            context.on("response", on_response)
            cookies: list[dict[str, Any]] = []
            if not has_valid_device_cookie:
                if any(
                    str(item.get("name") or "").strip().lower() == "oai-did"
                    for item in existing_cookies
                ):
                    await context.clear_cookies(name="oai-did")
                cookies.append(
                    {
                        "name": "oai-did",
                        "value": device_id,
                        "domain": ".chatgpt.com",
                        "path": "/",
                        "secure": True,
                    }
                )
            if session_token:
                cookie_backed = True
                for item in existing_cookies:
                    name = str(item.get("name") or "")
                    if name == "__Secure-next-auth.session-token" or name.startswith(
                        "__Secure-next-auth.session-token."
                    ):
                        await context.clear_cookies(name=name)
                cookies.extend(_nextauth_cookie_records(session_token))
            await context.add_cookies(cookies)
            bootstrap_headers = {
                "oai-device-id": device_id,
                "oai-session-id": session_id,
                "oai-language": language,
                "oai-client-build-number": os.getenv(
                    "OPLL_GOPAY_OAI_CLIENT_BUILD_NUMBER",
                    GOPAY_OAI_CLIENT_BUILD_NUMBER,
                ).strip()
                or GOPAY_OAI_CLIENT_BUILD_NUMBER,
                "oai-client-version": os.getenv(
                    "OPLL_GOPAY_OAI_CLIENT_VERSION", GOPAY_OAI_CLIENT_VERSION
                ).strip()
                or GOPAY_OAI_CLIENT_VERSION,
            }
            configured_attestation = os.getenv(
                "OPLL_GOPAY_OAI_WEB_DEPLOYMENT_ATTESTATION", ""
            ).strip()
            if configured_attestation:
                bootstrap_headers["oai-web-deployment-attestation"] = configured_attestation
            if not cookie_backed:
                bootstrap_headers["Authorization"] = f"Bearer {access_token}"
                if account_id:
                    bootstrap_headers["chatgpt-account-id"] = account_id
            session.bootstrap_headers = dict(bootstrap_headers)
            session.attestation = str(
                bootstrap_headers.get("oai-web-deployment-attestation") or ""
            )
            await context.set_extra_http_headers(bootstrap_headers)
            try:
                await page.goto(
                    (
                        f"{CHATGPT_ORIGIN}/?promo_campaign=plus-1-month-free"
                        if promo_campaign
                        else f"{CHATGPT_ORIGIN}/"
                    ),
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
            await self._settle_capture_tasks(session)
            browser_cookies = await context.cookies(CHATGPT_ORIGIN)
            return {
                "runtime_id": runtime_id,
                "device_id": device_id,
                "session_id": session.session_id,
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
        except BaseException:
            self._sessions.pop(runtime_id, None)
            try:
                await context.close()
            except Exception:
                pass
            raise

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
                    timeout=max(
                        5_000,
                        int(
                            os.getenv(
                                "OPLL_GOPAY_SENTINEL_NAVIGATION_TIMEOUT_MS",
                                "30000",
                            )
                        ),
                    ),
                )
            except (TimeoutError, PlaywrightTimeoutError):
                # A slow proxy can time out after the response has already
                # established a valid chatgpt.com document. Sentinel only
                # needs that same-origin document plus the exact Checkout
                # Referer, so retain it and continue with SDK injection.
                current_after_timeout = urlsplit(session.page.url)
                if (
                    current_after_timeout.netloc != "chatgpt.com"
                    or parsed.path.startswith("/checkout/")
                    and not current_after_timeout.path.startswith("/checkout/")
                ):
                    raise
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
        await self._settle_capture_tasks(session)
        cookies = await session.context.cookies(CHATGPT_ORIGIN)
        return {
            "session_id": session.session_id,
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
        await self._settle_capture_tasks(session)
        cookies = await session.context.cookies(CHATGPT_ORIGIN)
        return {
            "token": generated.get("token") if isinstance(generated, dict) else "",
            "timing": generated.get("timing") if isinstance(generated, dict) else None,
            "session_id": session.session_id,
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
            await self._settle_capture_tasks(session)
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
