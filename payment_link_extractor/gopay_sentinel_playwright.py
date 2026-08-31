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

from .auth import (
    NEXTAUTH_SESSION_COOKIE_PATTERN,
    is_nextauth_session_cookie_name,
)

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
    PlaywrightTimeoutError = TimeoutError  # type: ignore[assignment]
    async_playwright = None  # type: ignore[assignment]


SENTINEL_SDK_VERSION = "20260810913b"
CHATGPT_ORIGIN = "https://chatgpt.com"
PINNED_SENTINEL_SDK_SHA256 = (
    "49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd"
)
OPEN_SESSION_TIMEOUT_SECONDS = 240
FLOW_TIMEOUT_SECONDS = 210


def _is_chatgpt_url(value: str) -> bool:
    parsed = urlsplit(str(value or ""))
    return (
        parsed.scheme == "https"
        and parsed.netloc == "chatgpt.com"
        and not parsed.path.startswith("/auth/")
    )


def _enabled(value: str, *, default: bool) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text not in {"0", "false", "off", "disabled", "no"}


def _checkout_navigation_timeout_ms() -> int:
    try:
        value = int(os.getenv("OPLL_GOPAY_CHECKOUT_NAVIGATION_TIMEOUT_MS", "45000"))
    except ValueError:
        value = 45_000
    return max(5_000, min(90_000, value))


def _configured_cdp_port() -> int:
    """Return the optional existing-browser CDP port for GoPay Sentinel."""
    try:
        value = int(os.getenv("OPLL_GOPAY_SENTINEL_CDP_PORT", "0"))
    except ValueError:
        value = 0
    return value if 0 < value < 65_536 else 0


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


def _cookie_record(
    name: str,
    value: str,
    *,
    http_only: bool | None = None,
) -> dict[str, Any]:
    """Build a valid ChatGPT cookie record, including ``__Host-`` rules."""
    selected_name = str(name or "").strip()
    record: dict[str, Any] = {
        "name": selected_name,
        "value": str(value or ""),
        "secure": True,
        "httpOnly": (
            is_nextauth_session_cookie_name(selected_name)
            if http_only is None
            else bool(http_only)
        ),
    }
    # RFC 6265 forbids Domain on __Host- cookies; Playwright rejects such a
    # record before it reaches Chromium.
    if selected_name.startswith("__Host-"):
        record["url"] = CHATGPT_ORIGIN + "/"
    else:
        record["path"] = "/"
        record["domain"] = ".chatgpt.com"
    return record


def _is_auth_state_cookie(name: str) -> bool:
    """Return whether a cookie belongs to the live OpenAI login state."""
    normalized = str(name or "").strip().lower()
    return is_nextauth_session_cookie_name(name) or (
        "next-auth" in normalized
        or normalized in {
            "__secure-oai-is",
            "oai-client-auth-info",
            "oai-client-session-epoch",
            "_account",
        }
    )


def _cookies_from_header(
    value: str,
    device_id: str,
    session_cookies: tuple[tuple[str, str], ...] = (),
    *,
    preserve_device_id: bool = False,
) -> list[dict[str, Any]]:
    """Convert HTTP and imported NextAuth cookies into Playwright records."""
    values: dict[str, str] = {}
    for item in str(value or "").split(";"):
        name, separator, selected = item.strip().partition("=")
        if separator and name:
            values[name] = selected
    for name, selected in session_cookies:
        if str(name).strip() and str(selected).strip():
            values[str(name).strip()] = str(selected).strip()
    if not preserve_device_id or not any(
        str(name).strip().lower() == "oai-did" for name in values
    ):
        values["oai-did"] = str(device_id)
    return [
        _cookie_record(name, selected)
        for name, selected in values.items()
        if name and selected
    ]


def _browser_session_cookies(
    cookie_header: str,
    device_id: str,
    session_token: str,
    session_cookies: tuple[tuple[str, str], ...],
    *,
    discard_nextauth: bool = False,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Merge browser cookies without re-importing stale NextAuth chunks."""
    selected = tuple(session_cookies or ())
    explicit_nextauth = bool(session_token) or any(
        is_nextauth_session_cookie_name(name) for name, _value in selected
    )
    replace_nextauth = bool(explicit_nextauth or discard_nextauth)
    cookies = _cookies_from_header(
        cookie_header,
        device_id,
        (),
        preserve_device_id=explicit_nextauth,
    )
    if replace_nextauth:
        cookies = [
            item
            for item in cookies
            if not is_nextauth_session_cookie_name(item.get("name"))
        ]
        if discard_nextauth:
            cookies = [
                item
                for item in cookies
                if not _is_auth_state_cookie(item.get("name"))
            ]
    if selected:
        for name, value in selected:
            cookies.append(_cookie_record(str(name), str(value)))
    if session_token and not any(
        is_nextauth_session_cookie_name(name) for name, _value in selected
    ):
        cookies.append(
            _cookie_record(
                "__Secure-next-auth.session-token", str(session_token)
            )
        )
    return cookies, replace_nextauth, explicit_nextauth


@dataclass
class _BrowserSession:
    context: BrowserContext
    page: Page
    profile_path: Path
    device_id: str
    expected_user_id: str = ""
    expected_email: str = ""
    attestation: str = ""
    request_events: list[dict[str, Any]] = field(default_factory=list)
    latest_receipt: str = ""
    challenge_shapes: list[dict[str, Any]] = field(default_factory=list)
    sdk_sha256: str = ""
    bootstrap_headers: dict[str, str] = field(default_factory=dict)
    active_page_url: str = ""
    account_binding_verified: bool = False
    session_cookie_binding_verified: bool = False
    session_cookie_binding_state: str = "unverified"
    cookie_backed: bool = False
    checkout_navigation_fallback: bool = False
    checkout_navigation_error: str = ""
    external_cdp: bool = False
    access_token: str = ""
    account_id: str = ""
    session_id: str = ""
    effective_user_agent: str = ""
    effective_accept_language: str = ""
    effective_language: str = ""
    effective_timezone: str = ""
    effective_sec_ch_ua: str = ""
    effective_sec_ch_ua_mobile: str = "?0"
    effective_sec_ch_ua_platform: str = '"Windows"'
    request_handler: Any = None
    response_handler: Any = None
    cookie_fingerprint: str = ""
    cookie_state_changed: bool = False
    capture_tasks: set[asyncio.Task[Any]] = field(default_factory=set)


async def _verify_session_cookie_binding(
    page: Page,
    expected_user_id: str,
    expected_email: str = "",
    expected_account_id: str = "",
) -> str:
    """Verify the imported browser login independently from the bearer AT.

    A bearer-authenticated API request only proves that the AT is valid. GoPay
    approval also depends on the NextAuth browser session, so this browser-native
    probe reads the cookie-backed NextAuth ``/api/auth/session`` endpoint without
    an Authorization header.
    It returns a state so network/response failures are not mislabeled as a
    confirmed account mismatch.
    """
    expected = str(expected_user_id or "").strip()
    expected_email_value = str(expected_email or "").strip().lower()
    expected_account = str(expected_account_id or "").strip()
    if not expected and not expected_email_value and not expected_account:
        return "identity_missing"
    try:
        state = await page.evaluate(
            """async ({expected, expectedEmail, expectedAccount}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), 10000);
                try {
                    const response = await fetch('/api/auth/session', {
                        credentials: 'include',
                        cache: 'no-store',
                        headers: {accept: 'application/json'},
                        signal: controller.signal,
                    });
                    if (!response.ok) return 'unavailable';
                    let payload;
                    try { payload = await response.json(); }
                    catch (_) { return 'unavailable'; }
                    const actual = String(
                        payload?.user?.id
                        || payload?.user?.sub
                        || payload?.id
                        || payload?.sub
                        || ''
                    );
                    const actualEmail = String(payload?.user?.email || '').toLowerCase();
                    if (expectedAccount) {
                        const accountCookie = document.cookie
                            .split(';')
                            .map(item => item.trim().split('='))
                            .find(item => item[0] === '_account');
                        if (accountCookie && accountCookie.length > 1) {
                            let actualAccount = accountCookie.slice(1).join('=');
                            try { actualAccount = decodeURIComponent(actualAccount); }
                            catch (_) {}
                            if (/^[0-9a-f-]{36}$/i.test(actualAccount)
                                && actualAccount !== expectedAccount) {
                                return 'mismatched';
                            }
                        }
                    }
                    if (expected && actual) {
                        return actual === expected ? 'matched' : 'mismatched';
                    }
                    if (expectedEmail && actualEmail) {
                        return actualEmail === String(expectedEmail).toLowerCase()
                            ? 'matched' : 'mismatched';
                    }
                    return 'unavailable';
                } catch (_) {
                    return 'unavailable';
                } finally {
                    clearTimeout(timer);
                }
            }""",
            {
                "expected": expected,
                "expectedEmail": expected_email_value,
                "expectedAccount": expected_account,
            },
        )
    except Exception:
        return "unavailable"
    normalized = str(state or "").strip().lower()
    if normalized in {"matched", "mismatched"}:
        return normalized
    return "unavailable"


async def _verify_session_cookie_binding_with_retry(
    page: Page,
    expected_user_id: str,
    *,
    expected_email: str = "",
    expected_account_id: str = "",
    cookie_backed: bool,
    attempts: int = 3,
    delay_seconds: float = 0.2,
) -> str:
    """Retry only inconclusive probes inside the same browser runtime."""
    if not cookie_backed:
        return "not_present"
    budget = max(1, min(4, int(attempts)))
    state = "unavailable"
    for attempt in range(budget):
        state = await _verify_session_cookie_binding(
            page, expected_user_id, expected_email, expected_account_id
        )
        if state != "unavailable" or attempt + 1 >= budget:
            return state
        await asyncio.sleep(max(0.0, float(delay_seconds)) * (attempt + 1))
    return state


async def _replace_imported_session_cookies(
    context: BrowserContext,
    cookies: list[dict[str, Any]],
    *,
    replace_nextauth: bool,
) -> None:
    """Replace stale NextAuth chunks while preserving unrelated profile state."""
    if replace_nextauth:
        await context.clear_cookies(name=NEXTAUTH_SESSION_COOKIE_PATTERN)
    await context.add_cookies(cookies)


def _bootstrap_headers(
    *,
    access_token: str,
    account_id: str,
    device_id: str,
    session_id: str,
    language: str,
    attestation: str,
    cookie_backed: bool,
) -> dict[str, str]:
    """Build page headers without leaking bearer identity into cookie sessions."""
    headers = {
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
    if not cookie_backed:
        headers["Authorization"] = f"Bearer {access_token}"
        if account_id:
            headers["chatgpt-account-id"] = account_id
    if attestation:
        headers["oai-web-deployment-attestation"] = attestation
    return headers


async def _wait_for_sdk_ready(page: Page, timeout_ms: int = 30_000) -> None:
    """Poll SDK readiness through CDP-safe evaluate calls.

    ``page.wait_for_function`` evaluates a predicate through ``eval`` in the
    document and is rejected by ChatGPT's strict CSP on an attached browser.
    CDP-backed ``page.evaluate`` does not require ``unsafe-eval``.
    """
    deadline = time.monotonic() + max(1, int(timeout_ms)) / 1000
    expression = (
        "() => typeof window.SentinelSDK?.init === 'function' "
        "&& typeof window.SentinelSDK?.token === 'function' "
        "&& typeof window.SentinelSDK?.sessionObserverToken === 'function'"
    )
    while time.monotonic() < deadline:
        try:
            if await page.evaluate(expression):
                return
        except Exception:
            pass
        await asyncio.sleep(0.1)
    raise RuntimeError("Sentinel SDK did not become ready")


async def _page_fingerprint(
    page: Page,
    *,
    user_agent: str,
    language: str,
    timezone: str,
) -> dict[str, str]:
    """Read the effective fingerprint from an attached browser page.

    A CDP-attached context owns its own UA/locale/timezone and cannot be
    relaunch-configured by the provider.  Mirroring the values into the HTTP
    transport keeps the proof and the approval request on one fingerprint.
    """
    fallback = {
        "user_agent": str(user_agent or "").strip(),
        "accept_language": str(language or "").strip(),
        "language": str(language or "").strip(),
        "timezone": str(timezone or "").strip(),
        "sec_ch_ua": "",
        "sec_ch_ua_mobile": "?0",
        "sec_ch_ua_platform": '"Windows"',
    }
    try:
        value = await page.evaluate(
            """() => {
                const uaData = navigator.userAgentData;
                const brands = Array.isArray(uaData?.brands)
                    ? uaData.brands
                        .filter(item => item && item.brand && item.version)
                        .map(item => `\"${item.brand}\";v=\"${item.version}\"`)
                        .join(', ')
                    : '';
                const languages = Array.isArray(navigator.languages)
                    ? navigator.languages.filter(Boolean).map(String)
                    : [];
                return {
                    userAgent: String(navigator.userAgent || ''),
                    language: String(navigator.language || ''),
                    languages,
                    timezone: String(
                        Intl.DateTimeFormat().resolvedOptions().timeZone || ''
                    ),
                    secChUa: brands,
                    secChUaMobile: uaData?.mobile ? '?1' : '?0',
                    secChUaPlatform: uaData?.platform
                        ? `\"${uaData.platform}\"`
                        : '',
                };
            }"""
        )
    except Exception:
        return fallback
    if not isinstance(value, dict):
        return fallback
    selected_ua = str(value.get("userAgent") or "").strip() or fallback["user_agent"]
    selected_language = (
        str(value.get("language") or "").strip() or fallback["language"]
    )
    languages = value.get("languages")
    if isinstance(languages, list):
        clean_languages = [
            str(item).strip() for item in languages if str(item).strip()
        ]
        weighted: list[str] = []
        for index, item in enumerate(clean_languages):
            if index == 0:
                weighted.append(item)
            else:
                quality = max(0.1, 1.0 - 0.1 * index)
                weighted.append(f"{item};q={quality:.1f}")
        selected_accept = ",".join(weighted)
    else:
        selected_accept = ""
    selected_accept = selected_accept or selected_language or fallback["accept_language"]
    selected_timezone = (
        str(value.get("timezone") or "").strip() or fallback["timezone"]
    )
    return {
        "user_agent": selected_ua,
        "accept_language": selected_accept,
        "language": selected_language,
        "timezone": selected_timezone,
        "sec_ch_ua": str(value.get("secChUa") or "").strip()
        or fallback["sec_ch_ua"],
        "sec_ch_ua_mobile": str(value.get("secChUaMobile") or "").strip()
        or fallback["sec_ch_ua_mobile"],
        "sec_ch_ua_platform": str(value.get("secChUaPlatform") or "").strip()
        or fallback["sec_ch_ua_platform"],
    }


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
            # Propagate cancellation to the daemon loop so a timed-out
            # navigation cannot continue mutating a live browser tab.
            future.cancel()
            raise

    async def _capture_request(self, session_id: str, request: Any) -> None:
        session = self._sessions.get(session_id)
        if session is None:
            return
        if session.external_cdp:
            try:
                if request.frame.page is not session.page:
                    return
            except Exception:
                # The browser-level recorder owns service-worker targets.  The
                # provider keeps only its page/iframe events to avoid mixing
                # another live tab's receipts into this runtime.
                return
        try:
            parsed = urlsplit(request.url)
            headers = await request.all_headers()
        except Exception:
            return
        if parsed.scheme != "https" or parsed.hostname not in {
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
        if session.external_cdp:
            try:
                if response.frame.page is not session.page:
                    return
            except Exception:
                return
        try:
            response_url = response.url
            headers = await response.all_headers()
        except Exception:
            return
        parsed_response = urlsplit(response_url)
        if parsed_response.scheme != "https" or parsed_response.hostname not in {
            "chatgpt.com",
            "www.chatgpt.com",
        }:
            return
        path = parsed_response.path
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
        if path in {
            f"/sentinel/{SENTINEL_SDK_VERSION}/sdk.js",
            "/backend-api/sentinel/sdk.js",
        }:
            try:
                session.sdk_sha256 = hashlib.sha256(await response.body()).hexdigest()
            except Exception:
                pass

    async def _install_local_sdk(
        self,
        page: Page,
        source: str,
        bootstrap_headers: dict[str, str] | None = None,
    ) -> str:
        """Install the pinned SDK even when the page has a strict CSP.

        ``add_script_tag(content=...)`` is intentionally blocked by a normal
        ChatGPT document's CSP.  Page.setBypassCSP takes effect on the next
        navigation, so enable it for this page, reload the same URL, and then
        inject the verified source after DOMContentLoaded.  The page is a
        provider-owned tab (including external-CDP mode), never the user's
        existing tab.
        """
        cdp = None
        try:
            context = getattr(page, "context", None)
            new_cdp_session = getattr(context, "new_cdp_session", None)
            if not callable(new_cdp_session):
                # Lightweight test/page adapters may not expose CDP.  Real
                # Playwright pages always take the CSP-aware path below.
                await page.add_script_tag(content=source)
                await _wait_for_sdk_ready(page)
                return PINNED_SENTINEL_SDK_SHA256
            if bootstrap_headers:
                await page.set_extra_http_headers(dict(bootstrap_headers))
            cdp = await new_cdp_session(page)
            await cdp.send("Page.setBypassCSP", {"enabled": True})
            current_url = str(page.url or CHATGPT_ORIGIN)
            parsed = urlsplit(current_url)
            if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
                current_url = CHATGPT_ORIGIN + "/"
            await page.goto(
                current_url,
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            if not _is_chatgpt_url(page.url):
                raise RuntimeError("Sentinel SDK fallback navigation left chatgpt.com")
            await page.add_script_tag(content=source)
            await _wait_for_sdk_ready(page)
            return PINNED_SENTINEL_SDK_SHA256
        finally:
            if bootstrap_headers and callable(getattr(page, "set_extra_http_headers", None)):
                try:
                    await page.set_extra_http_headers({})
                except Exception:
                    pass
            if cdp is not None:
                try:
                    await cdp.send("Page.setBypassCSP", {"enabled": False})
                except Exception:
                    pass
                try:
                    await cdp.detach()
                except Exception:
                    pass

    async def _install_sdk(
        self,
        page: Page,
        bootstrap_headers: dict[str, str] | None = None,
    ) -> str:
        present = await page.evaluate(
            "() => typeof window.SentinelSDK?.init === 'function' && typeof window.SentinelSDK?.token === 'function' && typeof window.SentinelSDK?.sessionObserverToken === 'function'"
        )
        if present:
            return ""
        try:
            await page.add_script_tag(
                url=f"{CHATGPT_ORIGIN}/backend-api/sentinel/sdk.js"
            )
            try:
                await _wait_for_sdk_ready(page)
                return ""
            except Exception:
                # A stale/partially loaded remote asset is not usable. Reload
                # once and fall through to the verified local implementation.
                pass
        except Exception:
            pass
        # The fallback is CSP-aware and also handles a remote script that
        # loaded but failed before exposing the expected API.
        local_path = Path(__file__).resolve().parent / "sentinel_assets" / "sentinel_sdk.js"
        source = local_path.read_text(encoding="utf-8")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if digest != PINNED_SENTINEL_SDK_SHA256:
            raise RuntimeError("local Sentinel SDK checksum mismatch")
        return await self._install_local_sdk(page, source, bootstrap_headers)

    async def _wait_for_capture_settle(
        self, session: _BrowserSession, timeout_ms: int = 1500
    ) -> None:
        """Allow asynchronous request/response listeners to finish parsing."""
        deadline = time.monotonic() + max(0, int(timeout_ms)) / 1000
        previous = (-1, -1, "")
        while time.monotonic() < deadline:
            current = (
                len(session.request_events),
                len(session.challenge_shapes),
                session.latest_receipt,
            )
            if current == previous:
                await asyncio.sleep(0.15)
                if current == (
                    len(session.request_events),
                    len(session.challenge_shapes),
                    session.latest_receipt,
                ):
                    pending = list(session.capture_tasks)
                    if pending:
                        await asyncio.gather(*pending, return_exceptions=True)
                    return
            previous = current
            await asyncio.sleep(0.1)
        pending = list(session.capture_tasks)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

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
                            attestation = attestation || String(
                                data.webDeploymentAttestation || ''
                            );
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
            page_session_id = str(value.get("sessionId") or "").strip()
            if attestation:
                session.attestation = attestation
            if page_session_id:
                session.session_id = page_session_id

    async def _refresh_cookie_state(
        self, session: _BrowserSession
    ) -> list[dict[str, Any]]:
        """Refresh live cookies before every flow and approval decision."""
        cookies_method = getattr(session.context, "cookies", None)
        if not callable(cookies_method):
            return []
        current = await cookies_method(CHATGPT_ORIGIN)
        if not isinstance(current, list):
            current = []
        browser_device_id = next(
            (
                str(item.get("value") or "").strip()
                for item in current
                if str(item.get("name") or "").strip().lower() == "oai-did"
            ),
            "",
        )
        if browser_device_id:
            # Imported cookie-backed profiles carry the device id that signed
            # their attestation; do not replace it with the AT-derived UUID.
            session.device_id = browser_device_id
        session.cookie_backed = any(
            is_nextauth_session_cookie_name(item.get("name")) for item in current
        )
        auth_material = "\x00".join(
            sorted(
                f"{str(item.get('name') or '').strip()}={str(item.get('value') or '')}"
                for item in current
                if _is_auth_state_cookie(item.get("name"))
            )
        )
        fingerprint = hashlib.sha256(
            auth_material.encode("utf-8")
        ).hexdigest()
        session.cookie_state_changed = fingerprint != session.cookie_fingerprint
        session.cookie_fingerprint = fingerprint
        return current

    def _rebuild_bootstrap_headers(self, session: _BrowserSession) -> None:
        """Rebuild page headers after cookies/device/attestation rotate."""
        session.bootstrap_headers = _bootstrap_headers(
            access_token=session.access_token,
            account_id=session.account_id,
            device_id=session.device_id,
            session_id=session.session_id,
            language=session.effective_language,
            attestation=session.attestation,
            cookie_backed=session.cookie_backed,
        )

    async def _refresh_binding_state(
        self, session: _BrowserSession, *, attempts: int = 1
    ) -> str:
        """Re-check the cookie account when its NextAuth material changes."""
        if not session.cookie_backed:
            session.session_cookie_binding_state = "not_present"
            session.session_cookie_binding_verified = False
            return session.session_cookie_binding_state
        state = await _verify_session_cookie_binding_with_retry(
            session.page,
            session.expected_user_id,
            expected_email=session.expected_email,
            expected_account_id=session.account_id,
            cookie_backed=True,
            attempts=attempts,
        )
        session.session_cookie_binding_state = state
        session.session_cookie_binding_verified = state == "matched"
        return state

    async def _open_session_body_async(
        self,
        *,
        access_token: str,
        account_id: str,
        expected_user_id: str,
        expected_email: str = "",
        device_id: str,
        session_id: str,
        user_agent: str,
        browser_proxy: str,
        session_token: str,
        session_cookies: tuple[tuple[str, str], ...],
        deployment_attestation: str,
        cookie_header: str,
        language: str,
        timezone: str,
        _runtime_holder: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        runtime_id = uuid.uuid4().hex
        if _runtime_holder is not None:
            _runtime_holder["runtime_id"] = runtime_id
        headless = _enabled(os.getenv("OPLL_SENTINEL_HEADLESS", ""), default=False)
        browser_channel = os.getenv(
            "OPLL_GOPAY_SENTINEL_BROWSER_CHANNEL", "chrome"
        ).strip()
        configured_cdp_port = _configured_cdp_port()
        external_cdp = bool(configured_cdp_port)
        browser = None
        if external_cdp:
            endpoint = f"http://127.0.0.1:{configured_cdp_port}"
            browser = await self._playwright.chromium.connect_over_cdp(endpoint)
            if not browser.contexts:
                raise RuntimeError("configured GoPay CDP browser has no context")
            context = next(
                (
                    candidate
                    for candidate in browser.contexts
                    if any(
                        _is_chatgpt_url(str(existing.url or ""))
                        for existing in candidate.pages
                    )
                ),
                browser.contexts[0],
            )
            page = await context.new_page()
            # This is a diagnostic marker, not a second browser profile.  Keep
            # it absolute so callers do not mistake the attached context for a
            # newly launched persistent profile.
            profile_path = (
                Path(__file__).resolve().parents[1]
                / "data"
                / "gopay-sentinel-external-cdp"
                / str(configured_cdp_port)
            ).resolve()
        else:
            profile_path = _profile_root() / _profile_key(device_id)
            profile_path.mkdir(parents=True, exist_ok=True)
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
            page = context.pages[0] if context.pages else await context.new_page()
        if _runtime_holder is not None:
            _runtime_holder["context"] = context
            _runtime_holder["page"] = page
            _runtime_holder["external_cdp"] = external_cdp
        fingerprint = await _page_fingerprint(
            page,
            user_agent=user_agent,
            language=language,
            timezone=timezone,
        )
        effective_user_agent = fingerprint["user_agent"]
        effective_accept_language = fingerprint["accept_language"]
        effective_language = fingerprint["language"]
        effective_timezone = fingerprint["timezone"]
        effective_sec_ch_ua = fingerprint["sec_ch_ua"]
        effective_sec_ch_ua_mobile = fingerprint["sec_ch_ua_mobile"]
        effective_sec_ch_ua_platform = fingerprint["sec_ch_ua_platform"]
        browser_version = (
            str(context.browser.version)
            if getattr(context, "browser", None) is not None
            else ""
        )
        if not external_cdp:
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
        session = _BrowserSession(
            context=context,
            page=page,
            profile_path=profile_path,
            device_id=device_id,
            expected_user_id=str(expected_user_id or "").strip(),
            expected_email=str(expected_email or "").strip(),
            attestation=str(deployment_attestation or "").strip(),
            external_cdp=external_cdp,
            access_token=str(access_token or ""),
            account_id=str(account_id or ""),
            session_id=str(session_id or ""),
            effective_user_agent=effective_user_agent,
            effective_accept_language=effective_accept_language,
            effective_language=effective_language,
            effective_timezone=effective_timezone,
            effective_sec_ch_ua=effective_sec_ch_ua,
            effective_sec_ch_ua_mobile=effective_sec_ch_ua_mobile,
            effective_sec_ch_ua_platform=effective_sec_ch_ua_platform,
        )
        self._sessions[runtime_id] = session

        def schedule_capture(coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
            task = asyncio.create_task(coro)
            session.capture_tasks.add(task)
            task.add_done_callback(session.capture_tasks.discard)
            return task

        request_handler = lambda request: schedule_capture(
            self._capture_request(runtime_id, request)
        )
        response_handler = lambda response: schedule_capture(
            self._capture_response(runtime_id, response)
        )
        session.request_handler = request_handler
        session.response_handler = response_handler
        context.on("request", request_handler)
        context.on("response", response_handler)
        explicit_nextauth = bool(session_token) or any(
            is_nextauth_session_cookie_name(name)
            for name, _value in tuple(session_cookies or ())
        )
        allow_at_bound_browser = _enabled(
            os.getenv("OPLL_GOPAY_ALLOW_AT_BOUND_BROWSER", ""),
            default=False,
        )
        if external_cdp:
            # The attached browser already owns the authenticated Cookie jar;
            # never clear or overwrite the user's live context.
            cookies = []
            explicit_nextauth = False
        else:
            cookies, replace_nextauth, explicit_nextauth = _browser_session_cookies(
                cookie_header,
                device_id,
                session_token,
                tuple(session_cookies or ()),
                discard_nextauth=bool(allow_at_bound_browser and not explicit_nextauth),
            )
            await _replace_imported_session_cookies(
                context,
                cookies,
                replace_nextauth=replace_nextauth,
            )
        await self._refresh_cookie_state(session)
        self._rebuild_bootstrap_headers(session)
        bootstrap_headers = dict(session.bootstrap_headers)
        if external_cdp:
            await page.set_extra_http_headers(bootstrap_headers)
        else:
            await context.set_extra_http_headers(bootstrap_headers)
        try:
            await page.goto(
                f"{CHATGPT_ORIGIN}/?promo_campaign=plus-1-month-free",
                wait_until="domcontentloaded",
                timeout=90_000,
            )
            if not _is_chatgpt_url(page.url):
                raise RuntimeError("Sentinel browser navigation left chatgpt.com")
        finally:
            # Sentinel req/ping in both HARs are cookie/browser requests and do
            # not carry the API Authorization header.
            if external_cdp:
                await page.set_extra_http_headers({})
            else:
                await context.set_extra_http_headers({})
        sdk_hash = await self._install_sdk(page, session.bootstrap_headers)
        if sdk_hash:
            session.sdk_sha256 = sdk_hash
        await self._capture_page_attestation(session)
        await self._refresh_cookie_state(session)
        self._rebuild_bootstrap_headers(session)
        try:
            binding_attempts = int(
                os.getenv("OPLL_GOPAY_BINDING_PROBE_ATTEMPTS", "3")
            )
        except ValueError:
            binding_attempts = 3
        session.session_cookie_binding_state = (
            await _verify_session_cookie_binding_with_retry(
                page,
                expected_user_id,
                expected_email=expected_email,
                expected_account_id=account_id,
                cookie_backed=session.cookie_backed,
                attempts=binding_attempts,
            )
        )
        session.session_cookie_binding_verified = (
            session.session_cookie_binding_state == "matched"
        )
        try:
            binding = await page.evaluate(
                """async ({token, expected, expectedEmail, account, cookieBacked}) => {
                    const controller = new AbortController();
                    const timer = setTimeout(() => controller.abort(), 10000);
                    try {
                        const response = await fetch('/backend-api/me', {
                            // AT-only mode has already cleared NextAuth. Keep
                            // its Cloudflare/browser cookies; cookie-backed
                            // mode omits all cookies so Bearer is independent.
                            credentials: cookieBacked ? 'omit' : 'include',
                            headers: {
                                accept: 'application/json',
                                authorization: `Bearer ${token}`,
                                ...(account ? {'chatgpt-account-id': account} : {}),
                            },
                            signal: controller.signal,
                        });
                        if (!response.ok) return false;
                        const payload = await response.json();
                        const actual = String(payload?.id || '');
                        const actualEmail = String(
                            payload?.email || payload?.user?.email || ''
                        ).toLowerCase();
                        return Boolean(
                            (expected && actual === expected)
                            || (!expected && expectedEmail && actualEmail === expectedEmail)
                        );
                    } catch (_) {
                        return false;
                    } finally {
                        clearTimeout(timer);
                    }
                }""",
                {
                    "token": access_token,
                    "expected": expected_user_id,
                    "expectedEmail": str(expected_email or "").strip().lower(),
                    "account": account_id,
                    "cookieBacked": session.cookie_backed,
                },
            )
            session.account_binding_verified = bool(binding)
        except Exception:
            session.account_binding_verified = False
        await self._wait_for_capture_settle(session)
        browser_cookies = await context.cookies(CHATGPT_ORIGIN)
        session.active_page_url = str(page.url or "")
        return {
            "runtime_id": runtime_id,
            "device_id": session.device_id,
            "session_id": session.session_id,
            "external_cdp": external_cdp,
            "attestation": session.attestation,
            "cookie_header": _cookie_header(browser_cookies),
            "latest_receipt": session.latest_receipt,
            "challenge_shapes": list(session.challenge_shapes),
            "sdk_sha256": session.sdk_sha256,
            "profile_path": str(profile_path),
            "headless": headless,
            "browser_channel": "external-cdp"
            if external_cdp
            else browser_channel or "bundled-chromium",
            "browser_version": browser_version,
            "user_agent": effective_user_agent,
            "accept_language": effective_accept_language,
            "language": effective_language,
            "timezone": effective_timezone,
            "sec_ch_ua": effective_sec_ch_ua,
            "sec_ch_ua_mobile": effective_sec_ch_ua_mobile,
            "sec_ch_ua_platform": effective_sec_ch_ua_platform,
            "cookie_backed": session.cookie_backed,
            "account_binding_verified": session.account_binding_verified,
            "session_cookie_binding_verified": (
                session.session_cookie_binding_verified
            ),
            "session_cookie_binding_state": session.session_cookie_binding_state,
            "session_cookie_source": (
                "external"
                if external_cdp
                else "explicit"
                if explicit_nextauth
                else "profile" if session.cookie_backed else "none"
            ),
        }

    async def _open_session_async(self, **kwargs: Any) -> dict[str, Any]:
        holder = kwargs.get("_runtime_holder")
        if not isinstance(holder, dict):
            holder = {}
            kwargs["_runtime_holder"] = holder
        try:
            return await self._open_session_body_async(**kwargs)
        except BaseException:
            runtime_id = str(holder.get("runtime_id") or "")
            if runtime_id and runtime_id in self._sessions:
                try:
                    await self._close_session_async(runtime_id)
                except Exception:
                    pass
            else:
                page = holder.get("page")
                context = holder.get("context")
                if page is not None:
                    try:
                        await page.close()
                    except Exception:
                        pass
                elif context is not None:
                    try:
                        await context.close()
                    except Exception:
                        pass
            raise

    async def _cleanup_opening_holder(self, holder: dict[str, Any]) -> None:
        runtime_id = str(holder.get("runtime_id") or "")
        if runtime_id and runtime_id in self._sessions:
            await self._close_session_async(runtime_id)
            return
        page = holder.get("page")
        context = holder.get("context")
        if page is not None:
            try:
                await page.close()
            except Exception:
                pass
        elif context is not None:
            try:
                await context.close()
            except Exception:
                pass

    def open_session(self, **kwargs: Any) -> dict[str, Any]:
        runtime_holder: dict[str, Any] = {}
        kwargs = dict(kwargs)
        kwargs["_runtime_holder"] = runtime_holder
        try:
            return self._call(
                self._open_session_async(**kwargs),
                timeout=OPEN_SESSION_TIMEOUT_SECONDS,
            )
        except BaseException:
            try:
                self._call(
                    self._cleanup_opening_holder(runtime_holder),
                    timeout=30,
                )
            except Exception:
                pass
            raise

    async def _set_page_url(self, session: _BrowserSession, page_url: str) -> None:
        parsed = urlsplit(str(page_url or CHATGPT_ORIGIN))
        if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
            raise ValueError("Sentinel page URL must remain on chatgpt.com")
        await self._refresh_cookie_state(session)
        await self._capture_page_attestation(session)
        self._rebuild_bootstrap_headers(session)
        if session.cookie_state_changed and session.cookie_backed:
            await self._refresh_binding_state(session, attempts=1)
        relative = parsed.path or "/"
        if parsed.query:
            relative += "?" + parsed.query
        if parsed.fragment:
            relative += "#" + parsed.fragment
        navigate_checkout = _enabled(
            os.getenv("OPLL_GOPAY_SENTINEL_NAVIGATE_CHECKOUT", ""),
            default=True,
        )
        if (
            navigate_checkout
            and parsed.path.startswith("/checkout/")
            and session.active_page_url != parsed.geturl()
        ):
            await self._capture_page_attestation(session)
            self._rebuild_bootstrap_headers(session)
            if session.external_cdp:
                await session.page.set_extra_http_headers(session.bootstrap_headers)
            else:
                await session.context.set_extra_http_headers(session.bootstrap_headers)
            try:
                try:
                    await session.page.goto(
                        parsed.geturl(),
                        wait_until="domcontentloaded",
                        timeout=_checkout_navigation_timeout_ms(),
                    )
                except PlaywrightTimeoutError:
                    if session.cookie_backed:
                        raise
                    current_page = urlsplit(str(session.page.url or ""))
                    if (
                        current_page.scheme != "https"
                        or current_page.netloc != "chatgpt.com"
                    ):
                        raise
                    # AT-only browser sessions can leave the Checkout document
                    # pending even though the existing ChatGPT page and SDK are
                    # still usable. Keep the same runtime and repair only the
                    # history URL; never issue another Checkout POST.
                    await session.page.evaluate("() => { window.stop(); return true; }")
                    session.checkout_navigation_fallback = True
                    session.checkout_navigation_error = "timeout"
                if not _is_chatgpt_url(session.page.url):
                    raise RuntimeError("Sentinel checkout navigation left chatgpt.com")
            finally:
                if session.external_cdp:
                    await session.page.set_extra_http_headers({})
                else:
                    await session.context.set_extra_http_headers({})
            sdk_hash = await self._install_sdk(
                session.page, session.bootstrap_headers
            )
            if sdk_hash:
                session.sdk_sha256 = sdk_hash
            await self._capture_page_attestation(session)
            await self._refresh_cookie_state(session)
            self._rebuild_bootstrap_headers(session)
            await self._refresh_binding_state(session, attempts=3)
            # Auth-only AT sessions may be redirected while the checkout shell
            # loads. Restore the exact Checkout URL before SDK.init so its
            # zero-length ping carries the HAR checkout Referer.
            await session.page.evaluate(
                "url => history.replaceState({}, '', url)",
                relative,
            )
            session.active_page_url = parsed.geturl()
            return
        if not _is_chatgpt_url(session.page.url):
            raise RuntimeError("Sentinel page is not on chatgpt.com")
        await session.page.evaluate(
            "url => history.replaceState({}, '', url)",
            relative,
        )
        session.active_page_url = parsed.geturl()

    async def _prepare_flow_async(
        self, runtime_id: str, flow: str, page_url: str
    ) -> dict[str, Any]:
        session = self._sessions[runtime_id]
        await self._set_page_url(session, page_url)
        sdk_hash = await self._install_sdk(
            session.page, session.bootstrap_headers
        )
        if sdk_hash:
            session.sdk_sha256 = sdk_hash
        await self._capture_page_attestation(session)
        await self._refresh_cookie_state(session)
        self._rebuild_bootstrap_headers(session)
        if session.cookie_backed and (
            session.cookie_state_changed
            or session.session_cookie_binding_state not in {"matched", "mismatched"}
        ):
            await self._refresh_binding_state(session, attempts=2)
        before = len(session.request_events)
        await session.page.evaluate(
            "async flow => { await window.SentinelSDK.init(flow); return true; }",
            flow,
        )
        await self._wait_for_capture_settle(session)
        cookies = await session.context.cookies(CHATGPT_ORIGIN)
        return {
            "device_id": session.device_id,
            "session_id": session.session_id,
            "attestation": session.attestation,
            "cookie_header": _cookie_header(cookies),
            "user_agent": session.effective_user_agent,
            "accept_language": session.effective_accept_language,
            "language": session.effective_language,
            "timezone": session.effective_timezone,
            "sec_ch_ua": session.effective_sec_ch_ua,
            "sec_ch_ua_mobile": session.effective_sec_ch_ua_mobile,
            "sec_ch_ua_platform": session.effective_sec_ch_ua_platform,
            "cookie_backed": session.cookie_backed,
            "latest_receipt": session.latest_receipt,
            "request_events": session.request_events[before:],
            "challenge_shapes": list(session.challenge_shapes),
            "sdk_sha256": session.sdk_sha256,
            "session_cookie_binding_verified": (
                session.session_cookie_binding_verified
            ),
            "session_cookie_binding_state": session.session_cookie_binding_state,
            "checkout_navigation_fallback": session.checkout_navigation_fallback,
            "checkout_navigation_error": session.checkout_navigation_error,
        }

    def prepare_flow(self, runtime_id: str, flow: str, page_url: str) -> dict[str, Any]:
        return self._call(
            self._prepare_flow_async(runtime_id, flow, page_url),
            timeout=FLOW_TIMEOUT_SECONDS,
        )

    async def _token_async(
        self, runtime_id: str, flow: str, page_url: str
    ) -> dict[str, Any]:
        session = self._sessions[runtime_id]
        await self._set_page_url(session, page_url)
        sdk_hash = await self._install_sdk(
            session.page, session.bootstrap_headers
        )
        if sdk_hash:
            session.sdk_sha256 = sdk_hash
        await self._capture_page_attestation(session)
        await self._refresh_cookie_state(session)
        self._rebuild_bootstrap_headers(session)
        if session.cookie_backed and (
            session.cookie_state_changed
            or session.session_cookie_binding_state not in {"matched", "mismatched"}
        ):
            await self._refresh_binding_state(session, attempts=2)
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
        await self._wait_for_capture_settle(session)
        elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
        cookies = await session.context.cookies(CHATGPT_ORIGIN)
        return {
            "device_id": session.device_id,
            "session_id": session.session_id,
            "token": generated.get("token") if isinstance(generated, dict) else "",
            "timing": generated.get("timing") if isinstance(generated, dict) else None,
            "attestation": session.attestation,
            "cookie_header": _cookie_header(cookies),
            "user_agent": session.effective_user_agent,
            "accept_language": session.effective_accept_language,
            "language": session.effective_language,
            "timezone": session.effective_timezone,
            "sec_ch_ua": session.effective_sec_ch_ua,
            "sec_ch_ua_mobile": session.effective_sec_ch_ua_mobile,
            "sec_ch_ua_platform": session.effective_sec_ch_ua_platform,
            "cookie_backed": session.cookie_backed,
            "latest_receipt": session.latest_receipt,
            "request_events": session.request_events[before:],
            "elapsed_ms": elapsed_ms,
            "challenge_shapes": list(session.challenge_shapes),
            "sdk_sha256": session.sdk_sha256,
            "session_cookie_binding_verified": (
                session.session_cookie_binding_verified
            ),
            "session_cookie_binding_state": session.session_cookie_binding_state,
            "checkout_navigation_fallback": session.checkout_navigation_fallback,
            "checkout_navigation_error": session.checkout_navigation_error,
        }

    async def _browser_request_async(
        self,
        runtime_id: str,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout_ms: int = 45_000,
    ) -> dict[str, Any]:
        """Send a ChatGPT API request from the provider-owned browser tab.

        External-CDP mode must keep the protected ChatGPT mutations on the
        browser's actual proxy/IP and cookie jar.  This bridge deliberately
        strips browser-forbidden headers (Cookie/User-Agent/Sec-*) and lets
        Chromium supply them from the attached profile.
        """
        session = self._sessions[runtime_id]
        parsed = urlsplit(str(url or ""))
        if parsed.scheme != "https" or parsed.netloc != "chatgpt.com":
            raise ValueError("browser request URL must remain on chatgpt.com")
        if not _is_chatgpt_url(session.page.url):
            raise RuntimeError("Sentinel browser request page is not on chatgpt.com")
        await self._refresh_cookie_state(session)
        if session.cookie_backed and (
            session.cookie_state_changed
            or session.session_cookie_binding_state not in {"matched", "mismatched"}
        ):
            await self._refresh_binding_state(session, attempts=2)
        if session.cookie_backed and session.session_cookie_binding_state != "matched":
            raise RuntimeError("Sentinel browser request session binding is not matched")

        source_headers = dict(headers or {})
        referer = ""
        for key, value in source_headers.items():
            if str(key).lower() == "referer":
                referer = str(value or "").strip()
                break
        if referer:
            referer_url = urlsplit(referer)
            if referer_url.scheme != "https" or referer_url.netloc != "chatgpt.com":
                raise ValueError("browser request referer must remain on chatgpt.com")
            relative = referer_url.path or "/"
            if referer_url.query:
                relative += "?" + referer_url.query
            if referer_url.fragment:
                relative += "#" + referer_url.fragment
            await session.page.evaluate(
                "url => history.replaceState({}, '', url)", relative
            )
            session.active_page_url = referer_url.geturl()

        forbidden = {
            "accept-encoding",
            "connection",
            "content-length",
            "cookie",
            "host",
            "origin",
            "priority",
            "referer",
            "sec-ch-ua",
            "sec-ch-ua-mobile",
            "sec-ch-ua-platform",
            "sec-fetch-dest",
            "sec-fetch-mode",
            "sec-fetch-site",
            "user-agent",
        }
        safe_headers = {
            str(key): str(value)
            for key, value in source_headers.items()
            if str(key).lower() not in forbidden
        }
        result = await session.page.evaluate(
            """async ({url, method, headers, body, timeoutMs}) => {
                const controller = new AbortController();
                const timer = setTimeout(() => controller.abort(), timeoutMs);
                try {
                    const response = await fetch(url, {
                        method,
                        credentials: 'include',
                        cache: 'no-store',
                        redirect: 'follow',
                        headers,
                        body: body == null ? undefined : body,
                        signal: controller.signal,
                    });
                    const text = await response.text();
                    const responseHeaders = {};
                    response.headers.forEach((value, key) => {
                        responseHeaders[key] = value;
                    });
                    return {
                        status: response.status,
                        statusText: response.statusText,
                        url: response.url,
                        text,
                        headers: responseHeaders,
                    };
                } finally {
                    clearTimeout(timer);
                }
            }""",
            {
                "url": str(url),
                "method": str(method or "GET").upper(),
                "headers": safe_headers,
                "body": body,
                "timeoutMs": max(1_000, min(120_000, int(timeout_ms))),
            },
        )
        if not isinstance(result, dict) or "status" not in result:
            raise RuntimeError("browser request returned an invalid response")
        await self._capture_page_attestation(session)
        await self._refresh_cookie_state(session)
        self._rebuild_bootstrap_headers(session)
        return {
            "status_code": int(result.get("status") or 0),
            "reason": str(result.get("statusText") or ""),
            "url": str(result.get("url") or url),
            "text": str(result.get("text") or ""),
            "headers": dict(result.get("headers") or {}),
            "device_id": session.device_id,
            "session_id": session.session_id,
            "attestation": session.attestation,
            "cookie_header": _cookie_header(
                await session.context.cookies(CHATGPT_ORIGIN)
            ),
            "cookie_backed": session.cookie_backed,
            "latest_receipt": session.latest_receipt,
        }

    def browser_request(
        self,
        runtime_id: str,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: str | None = None,
        timeout_ms: int = 45_000,
    ) -> dict[str, Any]:
        return self._call(
            self._browser_request_async(
                runtime_id,
                method,
                url,
                headers,
                body,
                timeout_ms,
            ),
            timeout=max(60, min(180, int(timeout_ms / 1000) + 30)),
        )

    def token(self, runtime_id: str, flow: str, page_url: str) -> dict[str, Any]:
        return self._call(
            self._token_async(runtime_id, flow, page_url),
            timeout=FLOW_TIMEOUT_SECONDS,
        )

    async def _set_cookie_async(
        self, runtime_id: str, name: str, value: str, http_only: bool
    ) -> dict[str, Any]:
        session = self._sessions[runtime_id]
        selected_name = str(name or "").strip()
        # An attached browser owns the user's authentication jar.  Never
        # overwrite its NextAuth or device cookie from the HTTP side; Stripe
        # correlation cookies are still allowed and are intentionally shared
        # with the provider-owned tab.
        if session.external_cdp and (
            _is_auth_state_cookie(selected_name)
            or selected_name.lower() == "oai-did"
        ):
            cookies = await self._refresh_cookie_state(session)
            self._rebuild_bootstrap_headers(session)
            return {
                "device_id": session.device_id,
                "session_id": session.session_id,
                "cookie_header": _cookie_header(cookies),
                "session_cookie_binding_verified": (
                    session.session_cookie_binding_verified
                ),
                "session_cookie_binding_state": session.session_cookie_binding_state,
            }
        await session.context.add_cookies(
            [_cookie_record(selected_name, str(value), http_only=http_only)]
        )
        cookies = await self._refresh_cookie_state(session)
        self._rebuild_bootstrap_headers(session)
        return {
            "device_id": session.device_id,
            "session_id": session.session_id,
            "cookie_header": _cookie_header(cookies),
            "session_cookie_binding_verified": (
                session.session_cookie_binding_verified
            ),
            "session_cookie_binding_state": session.session_cookie_binding_state,
        }

    def set_cookie(
        self, runtime_id: str, name: str, value: str, http_only: bool
    ) -> dict[str, Any]:
        return self._call(
            self._set_cookie_async(runtime_id, name, value, http_only), timeout=30
        )

    async def _close_session_async(self, runtime_id: str) -> None:
        session = self._sessions.pop(runtime_id, None)
        if session is not None:
            for event_name, handler in (
                ("request", session.request_handler),
                ("response", session.response_handler),
            ):
                if handler is not None:
                    try:
                        session.context.remove_listener(event_name, handler)
                    except Exception:
                        pass
            pending = list(session.capture_tasks)
            for task in pending:
                if not task.done():
                    task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if session.external_cdp:
                try:
                    await session.page.close()
                except Exception:
                    pass
            else:
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
