from __future__ import annotations

"""Momo-only HTTP sessions with one proxy and one cookie jar per attempt."""

import json
import os
import re
import time
import uuid
from typing import Any, Mapping
import secrets
from urllib.parse import quote, unquote

import requests

from .auth import account_id
from .config import DEFAULT_USER_AGENT


MOMO_EMPTY_PENDING_UPDATES = '{"v":3,"updates":[]}'


def _browser_proxy_for(proxy: str) -> str:
    """Give a browser a CONNECT-capable proxy without changing Momo's API proxy."""
    try:
        from .web.socks5_bridge import http_proxy_for

        return http_proxy_for(proxy)
    except Exception:
        return proxy


class MomoSentinelProvider:
    """Momo-owned adapter for the shared Sentinel browser primitive.

    The Momo flow owns the context, proxy and lifecycle; the shared primitive
    only performs browser proof generation.  No GoPay/PayPal state is imported.
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
        timezone: str = "",
    ) -> None:
        from .transport import BrowserSentinelProvider

        self._delegate = BrowserSentinelProvider(
            access_token=access_token,
            device_id=device_id,
            session_id=session_id,
            user_agent=user_agent,
            proxy=_browser_proxy_for(proxy),
            transport_session=transport_session,
            enabled_env="OPLL_MOMO_SENTINEL_BROWSER",
            locale=os.getenv("OPLL_MOMO_BROWSER_LOCALE", "").strip() or "vi-VN",
            client_build_number=os.getenv("OPLL_MOMO_OAI_CLIENT_BUILD_NUMBER", "").strip()
            or "10109010",
            client_version=os.getenv("OPLL_MOMO_OAI_CLIENT_VERSION", "").strip()
            or "prod-31e08510fe1189856ad77823ca134a25c60715b5",
            # The enhanced bridge is Momo-only.  It loads the bundled SDK on
            # chatgpt.com, mirrors runtime cookies/receipts, and uses the VN
            # browser timezone rather than the legacy GCash default.
            enhanced=True,
            session_token=session_token,
            timezone=(
                str(timezone or "").strip()
                or os.getenv("OPLL_MOMO_BROWSER_TIMEZONE", "").strip()
                or "Asia/Saigon"
            ),
        )

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._delegate, "enabled", False))

    def headers(self, flow: str, *, referer: str = "") -> dict[str, str]:
        return dict(self._delegate.headers(flow, referer=referer) or {})

    def prepare(self) -> None:
        prepare = getattr(self._delegate, "prepare", None)
        if callable(prepare):
            prepare()

    def prepare_flow(self, *, flow: str, referer: str = "") -> None:
        prepare_flow = getattr(self._delegate, "prepare_flow", None)
        if callable(prepare_flow):
            prepare_flow(flow=flow, referer=referer)

    def set_cookie(self, name: str, value: str, *, http_only: bool = False) -> None:
        setter = getattr(self._delegate, "set_cookie", None)
        if callable(setter):
            setter(name, value, http_only=http_only)

    def close(self) -> None:
        self._delegate.close()


MOMO_BROWSER_PROFILES: tuple[dict[str, str], ...] = (
    {
        "name": "chrome150",
        "impersonate": "chrome150",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not=A?Brand";v="99", "Google Chrome";v="150", "Chromium";v="150"',
    },
    {
        "name": "chrome145",
        "impersonate": "chrome145",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="145", "Google Chrome";v="145", "Not=A?Brand";v="99"',
    },
    {
        "name": "chrome146",
        "impersonate": "chrome146",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="146", "Google Chrome";v="146", "Not=A?Brand";v="99"',
    },
    {
        "name": "chrome136",
        "impersonate": "chrome136",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not:A-Brand";v="99"',
    },
)

# Keep a compatibility alias for callers that recorded the Chrome 152 label;
# the active wire/header pair remains internally consistent and uses the
# supported Chrome 150 profile. Defaults rotate only among matching pairs.
MOMO_BROWSER_ALIASES = {"chrome152": "chrome150"}


class MomoTransportFactory:
    """Create isolated ChatGPT, Stripe and Momo sessions for one attempt."""

    def __init__(self, fingerprint: str = "") -> None:
        requested = str(fingerprint or "").strip().lower()
        requested = MOMO_BROWSER_ALIASES.get(requested, requested)
        matches = [p for p in MOMO_BROWSER_PROFILES if requested == p["name"]]
        if not matches:
            matches = [p for p in MOMO_BROWSER_PROFILES if requested == p["impersonate"]]
        self.profile = dict(matches[0] if matches else secrets.choice(MOMO_BROWSER_PROFILES))

    def chatgpt(self, config: Any, proxy: str) -> requests.Session:
        session = _new_session(self.profile["impersonate"])
        device_id = str(uuid.uuid4())
        session_id = str(uuid.uuid4())
        observation = os.getenv("OPLL_MOMO_OAI_IS_CLIENT_OBSERVATION", "").strip()
        client_build_number = (
            os.getenv("OPLL_MOMO_OAI_CLIENT_BUILD_NUMBER", "").strip()
            or os.getenv("OPLL_OAI_CLIENT_BUILD_NUMBER", "").strip()
            or "10109010"
        )
        client_version = (
            os.getenv("OPLL_MOMO_OAI_CLIENT_VERSION", "").strip()
            or os.getenv("OPLL_OAI_CLIENT_VERSION", "").strip()
            or "prod-31e08510fe1189856ad77823ca134a25c60715b5"
        )
        session.headers.update(
            {
                "Authorization": f"Bearer {config.access_token}",
                "User-Agent": self.profile["user_agent"],
                "Accept": "*/*",
                "Origin": "https://chatgpt.com",
                "Referer": "https://chatgpt.com/",
                "Content-Type": "application/json",
                "Accept-Language": "vi-VN,vi;q=0.9",
                "oai-device-id": device_id,
                "oai-session-id": session_id,
                "oai-language": os.getenv("OPLL_MOMO_OAI_LANGUAGE", "").strip() or "vi-VN",
                "oai-client-build-number": os.getenv(
                    "OPLL_MOMO_OAI_CLIENT_BUILD_NUMBER", ""
                ).strip()
                or client_build_number,
                "oai-client-version": client_version,
                "x-oai-is-pending-updates": os.getenv(
                    "OPLL_X_OAI_IS_PENDING_UPDATES", ""
                ).strip()
                or MOMO_EMPTY_PENDING_UPDATES,
                "x-oai-is-client-observation": observation
                or f"v1.r.p.{secrets.token_urlsafe(12).rstrip('=')}",
                "Sec-CH-UA": self.profile["sec_ch_ua"],
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }
        )
        # Let the HTTP client's cookie jar merge Set-Cookie updates from the
        # account/checkout responses.  A fixed Cookie header would freeze the
        # pre-Sentinel state and break the same-session binding.
        try:
            session.cookies.set("oai-did", device_id, domain=".chatgpt.com", path="/")
        except Exception:
            session.headers["Cookie"] = f"oai-did={device_id}"
        session.momo_cookie_jar_mode = True
        # The browser only adds the selected account header after Checkout
        # opens (taxes/confirm).  Keep it as session metadata and let
        # momo_request_headers add it per route; the initial eligibility and
        # checkout requests in the VN HAR do not carry this header.
        session.openai_account_id = account_id(
            str(getattr(config, "access_token", "") or "")
        )
        session.openai_device_id = device_id
        session.openai_did = device_id
        session.openai_session_id = session_id
        session.openai_proxy = proxy
        session.openai_request_started = time.perf_counter()

        def refresh_momo_request_headers(method: str, url: str) -> dict[str, str]:
            pinned = observation or os.getenv(
                "OPLL_MOMO_OAI_IS_CLIENT_OBSERVATION", ""
            ).strip()
            value = pinned or f"v1.r.p.{secrets.token_urlsafe(12).rstrip('=')}"
            session.headers["x-oai-is-client-observation"] = value
            dynamic: dict[str, str] = {"x-oai-is-client-observation": value}
            if method.upper() == "POST":
                normalized = str(url or "").lower()
                started_at = float(
                    getattr(
                        session,
                        "momo_header_started",
                        session.openai_request_started,
                    )
                )
                if normalized.endswith("/backend-api/payments/checkout"):
                    elapsed = round(
                        (time.perf_counter() - started_at) * 1000,
                        1,
                    )
                    captured = str(
                        getattr(session, "openai_checkout_telemetry", "") or ""
                    ).strip()
                    dynamic["oai-telemetry"] = os.getenv(
                        "OPLL_MOMO_OAI_CHECKOUT_TELEMETRY",
                        captured
                        or json.dumps(
                            [1, elapsed, 8, 96, 48, 2, 0, elapsed + 4],
                            separators=(",", ":"),
                        ),
                    )
                elif normalized.endswith("/backend-api/payments/checkout/confirm"):
                    elapsed = round(
                        (time.perf_counter() - started_at) * 1000,
                        1,
                    )
                    captured = str(
                        getattr(session, "openai_approve_telemetry", "") or ""
                    ).strip()
                    dynamic["oai-telemetry"] = os.getenv(
                        "OPLL_MOMO_OAI_CONFIRM_TELEMETRY",
                        captured
                        or json.dumps(
                            [1, elapsed, 8, 103, 47, 2, 0, elapsed + 5],
                            separators=(",", ":"),
                        ),
                    )
            return dynamic

        session.refresh_momo_request_headers = refresh_momo_request_headers
        attestation = os.getenv("OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION", "").strip()
        if attestation:
            session.headers["oai-web-deployment-attestation"] = attestation
        # A browser-generated proof is preferred. Explicit values remain a
        # useful runtime fallback for deployments that inject their own proof.
        sentinel = os.getenv("OPLL_MOMO_OPENAI_SENTINEL_TOKEN", "").strip() or os.getenv(
            "OPLL_OPENAI_SENTINEL_TOKEN", ""
        ).strip()
        if sentinel:
            session.openai_sentinel_token = sentinel
        sentinel_so = os.getenv("OPLL_MOMO_OPENAI_SENTINEL_SO_TOKEN", "").strip() or os.getenv(
            "OPLL_OPENAI_SENTINEL_SO_TOKEN", ""
        ).strip()
        if sentinel_so:
            session.openai_sentinel_so_token = sentinel_so
        _set_proxy(session, proxy)
        mode = os.getenv("OPLL_MOMO_SENTINEL_BROWSER", "auto").strip().lower()
        if mode not in {"0", "false", "off", "disabled", "no"}:
            try:
                from .transport import _agent_browser_binary

                if _agent_browser_binary():
                    session.openai_sentinel_provider = MomoSentinelProvider(
                        access_token=str(getattr(config, "access_token", "") or ""),
                        device_id=device_id,
                        session_id=session_id,
                        user_agent=self.profile["user_agent"],
                        proxy=normalize_momo_proxy(proxy),
                        transport_session=session,
                        session_token=str(getattr(config, "session_token", "") or ""),
                        timezone=(
                            os.getenv("OPLL_MOMO_BROWSER_TIMEZONE", "").strip()
                            or "Asia/Saigon"
                        ),
                    )
            except Exception:
                # Keep the explicit token fallback and let the API return its
                # actual status when a browser helper cannot be started.
                pass
        return session

    def stripe(self, config: Any) -> requests.Session:
        session = _new_session(self.profile["impersonate"])
        session.headers.update(
            {
                "User-Agent": self.profile["user_agent"],
                # Stripe.js sends these requests from its iframe origin, not
                # from the hosted Checkout origin.  Keep the browser contract
                # used by the captured MoMo flow, including locale headers.
                "Accept": "application/json",
                "Accept-Language": "vi-VN,vi;q=0.9",
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com",
                "Referer": "https://js.stripe.com/",
                "Sec-CH-UA": self.profile["sec_ch_ua"],
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-site",
            }
        )
        _set_proxy(session, config.checkout_proxy)
        return session

    def momo(self, config: Any) -> requests.Session:
        session = _new_session(self.profile["impersonate"])
        session.headers.update(
            {
                "User-Agent": self.profile["user_agent"],
                "Accept": "text/html,application/json",
                "Accept-Language": "vi-VN,vi;q=0.9",
                "Sec-CH-UA": self.profile["sec_ch_ua"],
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
            }
        )
        _set_proxy(session, config.checkout_proxy)
        # The browser gateway sends an empty POST body and carries the
        # session in its cookie jar.  query_gateway retains a JSON fallback
        # for older/fake sessions used by callers and tests.
        session.momo_query_session_bodyless = True
        return session


def _new_session(impersonate: str) -> Any:
    try:
        from curl_cffi.requests import Session as CurlSession
        return CurlSession(impersonate=impersonate)
    except Exception:
        return requests.Session()


def _set_proxy(session: Any, proxy: str) -> None:
    value = normalize_momo_proxy(proxy)
    if value:
        session.proxies.update({"http": value, "https": value})


def normalize_momo_proxy(proxy: str) -> str:
    """Normalize Momo's documented proxy forms without crossing channels.

    The VN 1024proxy export uses ``host:port:user:password`` and exposes a
    SOCKS5 endpoint on port 3000.  Treating that export as an HTTP proxy makes
    the TCP socket open but every HTTPS request hang during proxy negotiation.
    Other bare proxy exports retain the historical HTTP scheme, while explicit
    schemes are passed through unchanged.
    """
    value = str(proxy or "").strip()
    if not value or "://" in value:
        return value
    parts = value.split(":", 3)
    if len(parts) != 4 or not all(parts):
        return value
    host, port, user, password = parts
    try:
        parsed_port = int(port)
    except (TypeError, ValueError):
        parsed_port = 0
    lowered_host = host.lower().rstrip(".")
    is_1024proxy_socks = (
        parsed_port == 3000
        and (lowered_host == "1024proxy.io" or lowered_host.endswith(".1024proxy.io"))
    )
    scheme = "socks5h" if is_1024proxy_socks else "http"
    return f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"


def momo_sentinel_headers(
    session: Any, *, flow: str = "", referer: str = ""
) -> dict[str, str]:
    """Return fresh Momo Sentinel proof plus runtime-injected fallbacks."""
    result: dict[str, str] = {}
    provider = getattr(session, "openai_sentinel_provider", None)
    provider_error: Exception | None = None
    if flow and provider is not None:
        try:
            result.update(provider.headers(flow, referer=referer) or {})
        except Exception as exc:
            provider_error = exc
            # The API response remains the source of truth if the optional
            # browser helper is unavailable; no captured proof is replayed.
            pass
    token = str(
        getattr(session, "openai_sentinel_token", "")
        or os.getenv("OPLL_MOMO_OPENAI_SENTINEL_TOKEN", "")
        or os.getenv("OPLL_OPENAI_SENTINEL_TOKEN", "")
    ).strip()
    if token and not any(
        key.lower() == "openai-sentinel-token" for key in result
    ):
        result["OpenAI-Sentinel-Token"] = token
    so_token = str(
        getattr(session, "openai_sentinel_so_token", "")
        or os.getenv("OPLL_MOMO_OPENAI_SENTINEL_SO_TOKEN", "")
        or os.getenv("OPLL_OPENAI_SENTINEL_SO_TOKEN", "")
    ).strip()
    if so_token and not any(
        key.lower() == "openai-sentinel-so-token" for key in result
    ):
        result["OpenAI-Sentinel-SO-Token"] = so_token
    attestation = str(
        getattr(session, "openai_web_deployment_attestation", "")
        or os.getenv("OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION", "")
    ).strip()
    if attestation and not any(
        key.lower() == "oai-web-deployment-attestation" for key in result
    ):
        result["oai-web-deployment-attestation"] = attestation
    if flow and bool(getattr(session, "momo_sentinel_required", False)):
        has_proof = any(
            key.lower() == "openai-sentinel-token" and str(value).strip()
            for key, value in result.items()
        )
        if not has_proof:
            detail = type(provider_error).__name__ if provider_error else "missing"
            raise RuntimeError(f"Momo Sentinel proof unavailable ({detail})")
    return result


def momo_request_headers(
    session: Any,
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    *,
    flow: str = "",
    referer: str = "",
) -> dict[str, str]:
    """Merge per-request Momo headers without mutating caller dictionaries."""
    merged = dict(headers or {})
    normalized_url = str(url or "").lower()
    if normalized_url.endswith(
        ("/backend-api/payments/checkout/taxes", "/backend-api/payments/checkout/confirm")
    ):
        account = str(getattr(session, "openai_account_id", "") or "").strip()
        if account:
            merged.setdefault("chatgpt-account-id", account)
    if flow:
        # SentinelSDK.token() performs the browser ping and records its live
        # timing tuple.  Generate that proof before refreshing telemetry so
        # the same request carries the measured tuple, not a cumulative
        # process-start elapsed value.
        session.momo_header_started = time.perf_counter()
        merged.update(momo_sentinel_headers(session, flow=flow, referer=referer))
    refresh = getattr(session, "refresh_momo_request_headers", None)
    if callable(refresh):
        dynamic = refresh(str(method).upper(), url) or {}
        merged.update(dynamic)
    return merged


def momo_gateway_page_headers(session: Any, gateway_url: str) -> dict[str, str]:
    """Build document-navigation headers for the initial MoMo gateway GET."""
    # The browser follows Stripe's authorize 302 into a top-level document.
    # Keep this header set separate from the XHR contract used by
    # ``querySession``; in particular, do not send an Origin or JSON content
    # type on the navigation request.
    headers = {
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8,"
            "application/signed-exchange;v=b3;q=0.7"
        ),
        "Referer": str(
            getattr(session, "momo_gateway_referer", "") or "https://chatgpt.com/"
        ),
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "cross-site",
        "Upgrade-Insecure-Requests": "1",
    }
    return headers


def momo_gateway_headers(
    session: Any,
    gateway_url: str,
    *,
    csrf_token: str = "",
    bodyless: bool = False,
) -> dict[str, str]:
    """Build the XHR headers used by MoMo gateway polling.

    ``querySession`` in the captured browser flow has an empty body and no
    Content-Type.  Callers using the legacy JSON fallback can leave
    ``bodyless`` false to retain the explicit JSON content type.
    """
    headers = {
        "Accept": "*/*" if bodyless else "application/json, text/plain, */*",
        "Origin": "https://payment.momo.vn",
        "Referer": gateway_url,
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if not bodyless:
        headers["Content-Type"] = "application/json"
    token = str(csrf_token or "").strip()
    if not token:
        token = str(getattr(session, "momo_csrf_token", "") or "").strip()
    if not token:
        token = os.getenv("OPLL_MOMO_CSRF_TOKEN", "").strip()
    if token:
        headers["X-CSRF-Token"] = token
    return headers


def capture_momo_csrf_token(session: Any, response: Any) -> str:
    """Capture a runtime CSRF value from the gateway response or cookie jar."""
    candidates: list[str] = []
    response_headers = getattr(response, "headers", {}) or {}
    if hasattr(response_headers, "items"):
        for key, value in response_headers.items():
            if str(key).lower() in {"x-csrf-token", "x-xsrf-token"}:
                candidates.append(str(value or ""))
    body = str(getattr(response, "text", "") or "")
    if body:
        # MoMo has used both a meta tag and a bootstrap object across gateway
        # deployments.  Read only the value from the live response; never
        # persist it in logs or source.
        patterns = (
            # Spring's current MoMo gateway uses ``name="_csrf"``; older
            # deployments used csrf-token/xsrf-token.  Support both attribute
            # orders because minifiers are free to reorder meta attributes.
            r"<meta[^>]+name=[\"'](?:_csrf|csrf-token|xsrf-token)[\"'][^>]+content=[\"']([^\"']+)",
            r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+name=[\"'](?:_csrf|csrf-token|xsrf-token)[\"']",
            r"(?:csrfToken|csrf_token|xsrfToken)\s*[:=]\s*[\"']([^\"']+)",
        )
        for pattern in patterns:
            match = re.search(pattern, body, re.I)
            if match:
                candidates.append(match.group(1))
    cookies = getattr(session, "cookies", None)
    if cookies is not None:
        for name in ("XSRF-TOKEN", "xsrf-token", "csrf-token", "CSRF-TOKEN"):
            try:
                value = cookies.get(name)
            except Exception:
                value = ""
            if value:
                candidates.append(unquote(str(value)))
    for value in candidates:
        selected = str(value or "").strip()
        if selected:
            session.momo_csrf_token = selected
            return selected
    return str(getattr(session, "momo_csrf_token", "") or "").strip()


def close(session: Any) -> None:
    if session is None:
        return
    provider = getattr(session, "openai_sentinel_provider", None)
    shutdown = getattr(provider, "close", None)
    if callable(shutdown):
        try:
            shutdown()
        except Exception:
            pass
    if callable(getattr(session, "close", None)):
        session.close()
