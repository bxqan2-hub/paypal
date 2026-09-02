from __future__ import annotations

"""Momo-only HTTP sessions with one proxy and one cookie jar per attempt."""

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Mapping
import secrets
from urllib.parse import parse_qs, quote, unquote

import requests

from .auth import account_id


MOMO_EMPTY_PENDING_UPDATES = '{"v":3,"updates":[]}'
MOMO_PENDING_UPDATE_MAX = 32
MOMO_BROWSER_COOKIE_ALLOWLIST = frozenset(
    {
        "oai-did",
        "_account",
        "_account_is_fedramp",
        "oai-client-session-epoch",
        "oai-client-auth-info",
        "oai-sc",
        "oai-asli",
        "oai-hlib",
        "oai-gn",
        "oai-wm-opt-out",
        "__oailb",
        "__secure-oai-is",
        "oai-mweb-route-desktop",
        "oai-mweb-origin",
        "oai-mweb-auth-pending",
        "cf_clearance",
        "__cf_bm",
        "__cfseq",
        "cf_chl_rc_i",
        "cf_chl_rc_ni",
        "cf_chl_rc_m",
        "__stripe_mid",
        "__stripe_sid",
    }
)


def _momo_forbidden_browser_cookie(name: str) -> bool:
    """Identify login-session cookies that must not cross the AT-only bridge."""
    value = str(name or "").strip().lower()
    return (
        "next-auth" in value
        or value.startswith("authjs.")
        or value in {
            "nextauth.session-token",
            "next-auth.session-token",
            "__host-next-auth.csrf-token",
            "__secure-next-auth.callback-url",
            "__host-next-auth.callback-url",
        }
    )


def purge_momo_browser_auth_cookies(session: Any) -> list[str]:
    """Remove browser login/OAuth cookies copied into an AT-only HTTP jar.

    The shared browser helper mirrors its complete cookie view for legacy
    callers.  MoMo deliberately keeps only the non-secret routing/risk
    cookies; a stale NextAuth chunk would otherwise make an AT attempt appear
    to belong to a different account.
    """
    removed: list[str] = []
    jar = getattr(session, "cookies", None)
    if jar is not None:
        try:
            cookies = list(jar)
        except Exception:
            cookies = []
        for cookie in cookies:
            name = str(getattr(cookie, "name", "") or "").strip()
            if not name or not _momo_forbidden_browser_cookie(name):
                continue
            try:
                jar.clear(
                    domain=getattr(cookie, "domain", None),
                    path=getattr(cookie, "path", "/") or "/",
                    name=name,
                )
            except Exception:
                try:
                    jar.clear(name=name)
                except Exception:
                    continue
            removed.append(name)
    headers = getattr(session, "headers", None)
    if headers is not None:
        try:
            jar_mode = bool(getattr(session, "momo_cookie_jar_mode", False))
            raw = str(headers.get("Cookie") or headers.get("cookie") or "")
            if jar_mode:
                # The jar is authoritative for MoMo even when the snapshot is
                # empty; an explicit empty header can suppress jar cookies.
                headers.pop("Cookie", None)
                headers.pop("cookie", None)
            if raw:
                kept: list[str] = []
                for pair in raw.split(";"):
                    key, separator, _ = pair.strip().partition("=")
                    if separator and _momo_forbidden_browser_cookie(key):
                        if key not in removed:
                            removed.append(key)
                        continue
                    if pair.strip():
                        kept.append(pair.strip())
                if not jar_mode and kept:
                    headers["Cookie"] = "; ".join(kept)
                elif not jar_mode:
                    headers.pop("Cookie", None)
                    headers.pop("cookie", None)
        except Exception:
            pass
    try:
        names = {
            str(name)
            for name in (getattr(session, "momo_context_cookie_names", []) or [])
            if not _momo_forbidden_browser_cookie(str(name))
        }
        session.momo_context_cookie_names = sorted(names)
    except Exception:
        pass
    return removed


def momo_target_route(path: str) -> str:
    """Return the route template used by the current ChatGPT web client."""
    value = str(path or "").split("?", 1)[0].strip()
    if value.startswith(("/backend-api/accounts/check/", "/backend-anon/accounts/check/")):
        prefix = "/backend-anon" if value.startswith("/backend-anon/") else "/backend-api"
        return f"{prefix}/accounts/check/{{version}}"
    if value.startswith("/backend-anon/checkout_pricing_config/configs/"):
        return "/backend-anon/checkout_pricing_config/configs/{country_code}"
    if re.fullmatch(r"/backend-api/accounts/[^/]+/customer-balance", value):
        return "/backend-api/accounts/{account_id}/customer-balance"
    if re.fullmatch(r"/backend-api/checkout_pricing_config/configs/[^/]+", value):
        return "/backend-api/checkout_pricing_config/configs/{country_code}"
    if re.fullmatch(
        r"/backend-api/payments/checkout/[^/]+/(?:oaics_|cs_)[A-Za-z0-9_]+",
        value,
    ):
        return "/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}"
    return value


def _header_value(headers: Any, name: str) -> str:
    """Read one response/request header without depending on casing."""
    if headers is None:
        return ""
    target = str(name or "").strip().lower()
    if isinstance(headers, (list, tuple)):
        values: list[str] = []
        for item in headers:
            if isinstance(item, dict):
                key = item.get("name") or item.get("key")
                value = item.get("value")
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                key, value = item[0], item[1]
            else:
                continue
            if str(key or "").strip().lower() == target:
                values.append(str(value or "").strip())
        return "\n".join(value for value in values if value)
    try:
        direct = headers.get(name)
        if direct:
            return str(direct).strip()
    except Exception:
        pass
    try:
        items = headers.items()
    except Exception:
        return ""
    for key, value in items:
        if str(key or "").strip().lower() == target:
            return str(value or "").strip()
    return ""


def _pending_update_values(value: Any) -> list[str]:
    """Parse the in-memory pending-update envelope without logging values."""
    if isinstance(value, dict):
        raw = value.get("updates")
    else:
        try:
            raw = json.loads(str(value or "")).get("updates")
        except Exception:
            raw = None
    if not isinstance(raw, list):
        return []
    return [
        str(item).strip()
        for item in raw
        if str(item or "").strip() and len(str(item)) <= 16_384
    ]


def _set_pending_update_values(session: Any, values: list[str]) -> str:
    """Store the bounded runtime receipt queue on the ChatGPT session."""
    deduped: list[str] = []
    seen: set[str] = set()
    for item in values:
        value = str(item or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    deduped = deduped[-MOMO_PENDING_UPDATE_MAX:]
    session.momo_pending_updates = deduped
    envelope = (
        MOMO_EMPTY_PENDING_UPDATES
        if not deduped
        else json.dumps({"v": 3, "updates": deduped}, separators=(",", ":"))
    )
    headers = getattr(session, "headers", None)
    if headers is not None:
        headers["x-oai-is-pending-updates"] = envelope
    return envelope


def _current_pending_update_values(session: Any) -> list[str]:
    values = getattr(session, "momo_pending_updates", None)
    if isinstance(values, list):
        return [str(item).strip() for item in values if str(item or "").strip()]
    headers = getattr(session, "headers", None)
    return _pending_update_values(_header_value(headers, "x-oai-is-pending-updates"))


def current_momo_pending_updates_header(session: Any) -> str:
    """Return the current receipt envelope, creating an empty one if needed."""
    return _set_pending_update_values(session, _current_pending_update_values(session))


def clear_momo_pending_updates(session: Any) -> str:
    """Consume the pre-checkout receipt batch at the Checkout boundary.

    The VN browser sends an empty envelope on the route-data, tax and confirm
    requests immediately after the Checkout response ACK.  Keep the operation
    explicit so callers can still inspect the pre-checkout queue before it is
    consumed.
    """
    return _set_pending_update_values(session, [])


def sync_momo_response_cookies(session: Any, response: Any | None) -> list[str]:
    """Merge runtime ChatGPT routing cookies returned by a response."""
    if response is None:
        return []
    allowed = {
        "_account",
        "_account_is_fedramp",
        "oai-client-session-epoch",
        "oai-client-auth-info",
        "oai-sc",
        "oai-asli",
        "oai-hlib",
        "oai-gn",
        "oai-wm-opt-out",
        "__oailb",
        "__secure-oai-is",
        "oai-mweb-route-desktop",
        "oai-mweb-origin",
        "oai-mweb-auth-pending",
        "oai-did",
        "cf_clearance",
        "__cf_bm",
        "__cfseq",
        "cf_chl_rc_i",
        "cf_chl_rc_ni",
        "cf_chl_rc_m",
    }
    jar = getattr(session, "cookies", None)
    learned: list[str] = []
    response_cookies = getattr(response, "cookies", None)
    try:
        iterable = list(response_cookies or [])
    except Exception:
        iterable = []
    for cookie in iterable:
        name = str(getattr(cookie, "name", "") or "").strip()
        value = str(getattr(cookie, "value", "") or "")
        if name.lower() not in allowed or not value or jar is None:
            continue
        try:
            jar.set(name, value, domain=".chatgpt.com", path="/")
            learned.append(name)
        except Exception:
            continue
    headers = getattr(response, "headers", None)
    raw = _header_value(headers, "set-cookie")
    if raw and jar is not None:
        parts = re.split(r"(?:\r?\n)+|,\s*(?=[A-Za-z0-9_.-]+=)", raw)
        for part in parts:
            pair = str(part).split(";", 1)[0].strip()
            name, separator, value = pair.partition("=")
            name = name.strip()
            value = value.strip()
            if name.lower() not in allowed or not separator or not value:
                continue
            try:
                jar.set(name, value, domain=".chatgpt.com", path="/")
                if name not in learned:
                    learned.append(name)
            except Exception:
                continue
    if learned:
        try:
            session.momo_context_cookie_names = sorted(
                set(getattr(session, "momo_context_cookie_names", []) or [])
                | set(learned)
            )
            session.momo_account_cookie_present = "_account" in (
                set(getattr(session, "momo_context_cookie_names", []) or [])
            )
        except Exception:
            pass
    return learned


def seed_momo_account_cookie(session: Any, account: str) -> bool:
    """Seed the non-secret account routing cookie after Checkout creation."""
    value = str(account or "").strip()
    if not value:
        return False
    jar = getattr(session, "cookies", None)
    if jar is None:
        return False
    try:
        jar.set("_account", value, domain=".chatgpt.com", path="/")
        names = set(getattr(session, "momo_context_cookie_names", []) or [])
        names.add("_account")
        session.momo_context_cookie_names = sorted(names)
        session.momo_account_cookie_present = True
        return True
    except Exception:
        return False


def record_momo_pending_updates(
    session: Any,
    response: Any | None = None,
    *,
    receipts: list[str] | tuple[str, ...] = (),
) -> str:
    """Advance MoMo's runtime ``x-oai-is-pending-updates`` state.

    ChatGPT returns an opaque ``x-oai-is-receipt`` that the next platform
    request may echo inside the v3 envelope.  ``x-oai-is-update`` is a
    different response artifact and is intentionally never echoed.  The ACK
    is retained as metadata while the receipt queue remains available until
    the route-specific payment boundary consumes it; the canonical VN HAR
    still sends queued receipts on the request immediately following an ACK.
    Values remain process-local and are bounded/deduplicated.
    """
    sync_momo_response_cookies(session, response)
    response_headers = getattr(response, "headers", None) if response is not None else None
    receipt = _header_value(response_headers, "x-oai-is-receipt")
    ack = _header_value(response_headers, "x-oai-is-pending-updates-ack")
    queue = _current_pending_update_values(session)
    if ack:
        try:
            session.momo_pending_update_ack = ack
        except Exception:
            pass
    candidates = [receipt, *list(receipts)]
    queue.extend(
        str(item).strip()
        for item in candidates
        if str(item or "").strip()
    )
    return _set_pending_update_values(session, queue)


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
        self._transport_session = transport_session
        self._seen_browser_receipts: set[str] = set()
        self._last_browser_receipts_sync = 0.0
        # Do not import receipts from the browser's startup shell into the
        # first AT account request.  The canonical HAR sends an empty pending
        # envelope there; the receipt bridge is enabled only after Checkout
        # creates the session and a baseline monitor snapshot is primed.
        self._browser_receipts_enabled = False
        self._timezone_applied = False
        self._browser_state_checked = False
        self._backend_auth_config = {
            "token": str(access_token or ""),
            "account": account_id(access_token),
            "device": str(device_id or ""),
            "session": str(session_id or ""),
            "language": os.getenv("OPLL_MOMO_OAI_LANGUAGE", "").strip() or "vi-VN",
            "build": os.getenv("OPLL_MOMO_OAI_CLIENT_BUILD_NUMBER", "").strip()
            or "10109010",
            "version": os.getenv("OPLL_MOMO_OAI_CLIENT_VERSION", "").strip()
            or "prod-31e08510fe1189856ad77823ca134a25c60715b5",
            "observation": str(
                getattr(transport_session, "headers", {}).get(
                    "x-oai-is-client-observation", ""
                )
                if getattr(transport_session, "headers", None) is not None
                else ""
            ),
            "attestation": str(
                getattr(transport_session, "openai_web_deployment_attestation", "")
                or os.getenv("OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION", "")
                or ""
            ).strip(),
            "pending": current_momo_pending_updates_header(transport_session),
        }
        self._backend_auth_bridge_enabled = self._install_backend_auth_bridge()
        self._node_fallback_enabled = os.getenv(
            "OPLL_MOMO_SENTINEL_NODE_FALLBACK", "1"
        ).strip().lower() not in {"0", "false", "off", "no"}
        self._node_fallback_used = False
        self._browser_error = ""

    def _node_sentinel_headers(self, flow: str, referer: str) -> dict[str, str]:
        """Generate a fresh Momo-owned Node/V8 proof when browser startup stalls."""
        if not self._node_fallback_enabled:
            return {}
        try:
            import sentinel as node_sentinel

            cookie_header = ""
            jar = getattr(self._transport_session, "cookies", None)
            if jar is not None:
                try:
                    cookie_header = "; ".join(
                        f"{getattr(cookie, 'name', '')}={getattr(cookie, 'value', '')}"
                        for cookie in jar
                        if getattr(cookie, "name", "") and getattr(cookie, "value", "")
                    )
                except Exception:
                    cookie_header = ""
            main, observer = node_sentinel.mint_sentinel_sync(
                flow=str(flow or "chatgpt_checkout"),
                device_id=str(self._backend_auth_config.get("device") or ""),
                user_agent=str(self._backend_auth_config.get("user_agent") or "")
                or getattr(self._delegate, "user_agent", ""),
                proxy=str(getattr(self._delegate, "proxy", "") or ""),
                page_url=str(referer or "https://chatgpt.com/"),
                language=str(self._backend_auth_config.get("language") or "vi-VN"),
                timezone=str(getattr(self._delegate, "timezone", "Asia/Saigon")),
                cookie_header=cookie_header,
                timeout_s=90,
            )
            result = {"OpenAI-Sentinel-Token": main}
            if observer:
                result["OpenAI-Sentinel-SO-Token"] = observer
            self._node_fallback_used = True
            setattr(self._transport_session, "momo_sentinel_provider_mode", "node_fallback")
            return result
        except Exception as exc:
            self._browser_error = type(exc).__name__
            return {}

    def _install_backend_auth_bridge(self) -> bool:
        """Inject AT headers into every ChatGPT backend XHR/fetch in this context.

        GCash's AT-only browser path applies the bearer header at the network
        boundary, not through an OAuth login.  agent-browser exposes navigation
        headers only, so the Momo init script supplies the same narrow bridge
        for backend requests while explicitly excluding Sentinel endpoints.
        """
        script_path = getattr(self._delegate, "sentinel_init_script", None)
        if script_path is None:
            return False
        script_path = Path(script_path)
        bridge = (
            "\n;(() => { try {\n"
            "  const cfg = () => window.__opllMomoAuthConfig || {};\n"
            "  const backend = raw => { try {\n"
            "    const u = new URL(String(raw || ''), location.href);\n"
            "    return u.origin === 'https://chatgpt.com' && "
            "u.pathname.startsWith('/backend-api/') && "
            "!u.pathname.startsWith('/backend-api/sentinel/') && "
            "!u.pathname.endsWith('.data');\n"
            "  } catch (_) { return false; } };\n"
            "  const apply = (raw, headers) => {\n"
            "    if (!backend(raw)) return headers;\n"
            "    const u = new URL(String(raw || ''), location.href);\n"
            "    const config = cfg();\n"
            "    const h = headers instanceof Headers ? headers : new Headers(headers || {});\n"
            "    if (config.token) h.set('Authorization', 'Bearer ' + config.token);\n"
            "    const accountPhase = u.pathname.endsWith('/payments/checkout/taxes') || "
            "u.pathname.endsWith('/payments/checkout/confirm') || "
            "/^\\/backend-api\\/accounts\\/[^/]+\\/customer-balance$/.test(u.pathname) || "
            "/^\\/backend-api\\/payments\\/checkout\\/[^/]+\\/(?:oaics_|cs_)[A-Za-z0-9_]+$/.test(u.pathname);\n"
             "    if (config.account && accountPhase) h.set('chatgpt-account-id', config.account);\n"
             "    const attestationPhase = u.pathname.endsWith('/payments/checkout') || u.pathname.includes('/payments/checkout/');\n"
             "    if (config.attestation && attestationPhase) h.set('oai-web-deployment-attestation', config.attestation);\n"
             "    if (config.device) h.set('oai-device-id', config.device);\n"
            "    if (config.session) h.set('oai-session-id', config.session);\n"
            "    if (config.language) h.set('oai-language', config.language);\n"
            "    if (config.build) h.set('oai-client-build-number', config.build);\n"
             "    if (config.version) h.set('oai-client-version', config.version);\n"
             "    if (config.observation) h.set('x-oai-is-client-observation', config.observation);\n"
             "    if (!h.has('x-oai-is-pending-updates')) h.set('x-oai-is-pending-updates', config.pending || '{\"v\":3,\"updates\":[]}');\n"
             "    h.set('x-openai-target-path', u.pathname);\n"
            "    let route = u.pathname;\n"
            "    if (u.pathname.startsWith('/backend-api/accounts/check/')) route = '/backend-api/accounts/check/{version}';\n"
            "    else if (/^\\/backend-api\\/accounts\\/[^/]+\\/customer-balance$/.test(u.pathname)) route = '/backend-api/accounts/{account_id}/customer-balance';\n"
            "    else if (/^\\/backend-api\\/checkout_pricing_config\\/configs\\/[^/]+$/.test(u.pathname)) route = '/backend-api/checkout_pricing_config/configs/{country_code}';\n"
            "    else if (/^\\/backend-api\\/payments\\/checkout\\/[^/]+\\/(?:oaics_|cs_)[A-Za-z0-9_]+$/.test(u.pathname)) route = '/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}';\n"
            "    h.set('x-openai-target-route', route);\n"
            "    return h;\n"
            "  };\n"
            "  if (!window.__opllMomoAuthFetchWrapped && window.fetch) {\n"
            "    const originalFetch = window.fetch.bind(window);\n"
            "    window.fetch = (input, init) => {\n"
            "      const raw = input && input.url ? input.url : String(input || '');\n"
            "      const headers = apply(raw, input instanceof Request ? input.headers : (init || {}).headers);\n"
            "      const next = Object.assign({}, init || {}, {headers});\n"
            "      const request = input instanceof Request ? new Request(input, next) : input;\n"
            "      return originalFetch(request, next);\n"
            "    };\n"
            "    window.__opllMomoAuthFetchWrapped = true;\n"
            "  }\n"
            "  if (!window.__opllMomoAuthXhrWrapped && window.XMLHttpRequest) {\n"
            "    const open = XMLHttpRequest.prototype.open;\n"
            "    const send = XMLHttpRequest.prototype.send;\n"
            "    XMLHttpRequest.prototype.open = function(method, url) {\n"
            "      this.__opllMomoUrl = url;\n"
            "      return open.apply(this, arguments);\n"
            "    };\n"
            "    XMLHttpRequest.prototype.send = function(body) {\n"
            "      if (backend(this.__opllMomoUrl)) {\n"
            "        const h = apply(this.__opllMomoUrl, {});\n"
            "        h.forEach((value, key) => { try { this.setRequestHeader(key, value); } catch (_) {} });\n"
            "      }\n"
            "      return send.call(this, body);\n"
            "    };\n"
            "    window.__opllMomoAuthXhrWrapped = true;\n"
            "  }\n"
            "} catch (_) {} })();\n"
        )
        try:
            existing = script_path.read_text(encoding="utf-8")
            script_path.write_text(existing + bridge, encoding="utf-8")
            return True
        except Exception:
            return False

    def _activate_backend_auth_bridge(self) -> None:
        """Set AT bridge data after browser startup without writing it to disk."""
        evaluator = getattr(self._delegate, "_eval", None)
        if not callable(evaluator):
            return
        try:
            transport_headers = getattr(self._transport_session, "headers", None)
            if transport_headers is not None:
                observation = _header_value(
                    transport_headers, "x-oai-is-client-observation"
                )
                if observation:
                    self._backend_auth_config["observation"] = observation
            self._backend_auth_config["pending"] = current_momo_pending_updates_header(
                self._transport_session
            )
            evaluator(
                "((cfg) => { window.__opllMomoAuthConfig = cfg; return true; })("
                + json.dumps(self._backend_auth_config, separators=(",", ":"))
                + ")",
                timeout=10,
            )
            setattr(self._transport_session, "momo_backend_auth_bridge_enabled", True)
        except Exception:
            setattr(self._transport_session, "momo_backend_auth_bridge_enabled", False)

    def _sync_transport_cookies_to_browser(self) -> None:
        """Seed the browser with cookies learned by the AT HTTP session."""
        purge_momo_browser_auth_cookies(self._transport_session)
        jar = getattr(self._transport_session, "cookies", None)
        setter = getattr(self._delegate, "set_cookie", None)
        if jar is None or not callable(setter):
            return
        try:
            cookies = list(jar)
        except Exception:
            return
        for cookie in cookies:
            name = str(getattr(cookie, "name", "") or "").strip()
            value = str(getattr(cookie, "value", "") or "")
            domain = str(getattr(cookie, "domain", "") or "").lower()
            if not name or not value:
                continue
            if domain and "chatgpt.com" not in domain and "openai.com" not in domain:
                continue
            try:
                setter(
                    name,
                    value,
                    http_only=bool(cookie.has_nonstandard_attr("HttpOnly")),
                )
            except Exception:
                continue

    def _reset_stale_browser_context(self) -> None:
        """Remove OpenAI state from a persisted profile before binding a new AT."""
        if self._browser_state_checked:
            return
        self._browser_state_checked = True
        if not bool(getattr(self._delegate, "browser_profile", "")):
            return
        runner = getattr(self._delegate, "_run", None)
        if not callable(runner):
            return
        try:
            raw = runner(["cookies", "get", "--json"], timeout=10)
        except Exception:
            return
        data = raw.get("data") if isinstance(raw, dict) else None
        cookies = data.get("cookies") if isinstance(data, dict) else data
        if not isinstance(cookies, list):
            return
        # agent-browser currently exposes clear-all rather than per-cookie
        # deletion. Keep Cloudflare continuity only when the profile has no
        # OpenAI account/risk cookies that could belong to another AT.
        stale_names = {
            "_account",
            "_account_is_fedramp",
            "oai-sc",
            "oai-client-auth-info",
            "oai-client-session-epoch",
            "__secure-oai-is",
            "__secure-next-auth.session-token",
        }
        has_stale = any(
            str(item.get("name") or "").lower().split(".", 1)[0]
            in stale_names
            or str(item.get("name") or "").lower().startswith(
                "__secure-next-auth.session-token."
            )
            for item in cookies
            if isinstance(item, dict)
        )
        if has_stale:
            try:
                runner(["cookies", "clear"], timeout=10)
                setattr(self._transport_session, "momo_browser_state_reset", True)
            except Exception:
                pass

    def _sync_attestation_to_transport(self, value: str = "") -> None:
        """Expose the browser-captured deployment attestation to all MoMo API phases."""
        value = str(
            value or getattr(self._delegate, "_attestation", "") or ""
        ).strip()
        if not value:
            return
        try:
            config = getattr(self, "_backend_auth_config", None)
            if isinstance(config, dict):
                config["attestation"] = value
            self._transport_session.openai_web_deployment_attestation = value
            headers = getattr(self._transport_session, "headers", None)
            if headers is not None:
                headers["oai-web-deployment-attestation"] = value
        except Exception:
            pass

    def _browser_receipt_values(self) -> list[str]:
        """Read receipt headers from the monitor without mutating queue state."""
        runner = getattr(self._delegate, "_run", None)
        if not callable(runner):
            return []
        try:
            captured = runner(
                [
                    "network",
                    "requests",
                    "--json",
                    "--filter",
                    "chatgpt.com/backend-api",
                ],
                timeout=5,
            )
        except Exception:
            return []
        data = captured.get("data") if isinstance(captured, dict) else None
        requests = data.get("requests") if isinstance(data, dict) else None
        if not isinstance(requests, list):
            return []
        values: list[str] = []
        for item in requests[-64:]:
            if not isinstance(item, dict):
                continue
            response_headers = item.get("responseHeaders")
            if response_headers is None and isinstance(item.get("response"), dict):
                response_headers = item["response"].get("headers")
            if isinstance(response_headers, list):
                response_headers = {
                    str(header.get("name") or ""): str(header.get("value") or "")
                    for header in response_headers
                    if isinstance(header, dict)
                }
            receipt = _header_value(response_headers, "x-oai-is-receipt")
            if receipt:
                values.append(receipt)
        return values

    def enable_browser_receipts(self) -> None:
        """Start receipt mirroring after a clean monitor baseline."""
        if bool(getattr(self, "_browser_receipts_enabled", False)):
            return
        if not isinstance(getattr(self, "_seen_browser_receipts", None), set):
            self._seen_browser_receipts = set()
        # Mark receipts that predate the checkout context as seen, but do not
        # send them on the AT account/eligibility request.
        self._seen_browser_receipts.update(self._browser_receipt_values())
        self._browser_receipts_enabled = True
        setattr(self._transport_session, "momo_browser_receipts_enabled", True)

    def _sync_browser_receipts(self) -> None:
        """Merge receipts visible in the Momo browser monitor into the HTTP jar."""
        if not bool(getattr(self, "_browser_receipts_enabled", False)):
            return
        if not isinstance(getattr(self, "_seen_browser_receipts", None), set):
            self._seen_browser_receipts = set()
        now = time.monotonic()
        if now - float(getattr(self, "_last_browser_receipts_sync", 0.0)) < 0.5:
            return
        self._last_browser_receipts_sync = now
        receipts: list[str] = []
        for receipt in self._browser_receipt_values():
            if receipt in self._seen_browser_receipts:
                continue
            self._seen_browser_receipts.add(receipt)
            receipts.append(receipt)
        if receipts:
            record_momo_pending_updates(self._transport_session, receipts=receipts)

    @staticmethod
    def _network_items(value: Any) -> list[dict[str, Any]]:
        data = value.get("data") if isinstance(value, dict) else None
        items = data.get("requests") if isinstance(data, dict) else None
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    @staticmethod
    def _request_body(item: dict[str, Any]) -> str:
        for key in ("requestBody", "postData", "body"):
            value = item.get(key)
            if isinstance(value, dict):
                value = value.get("text") or value.get("body") or value.get("value")
            if isinstance(value, str) and value:
                return value
        request = item.get("request")
        if isinstance(request, dict) and request is not item:
            return MomoSentinelProvider._request_body(request)
        return ""

    @staticmethod
    def _request_url(item: dict[str, Any]) -> str:
        for key in ("url", "requestUrl"):
            value = item.get(key)
            if isinstance(value, str) and value:
                return value
        request = item.get("request")
        if isinstance(request, dict):
            value = request.get("url")
            if isinstance(value, str):
                return value
        return ""

    def _capture_browser_stripe_context(self) -> dict[str, Any]:
        """Capture only structural Stripe browser observations from the live page."""
        runner = getattr(self._delegate, "_run", None)
        if not callable(runner):
            return {}
        try:
            value = runner(
                ["network", "requests", "--json", "--filter", "stripe.com"],
                timeout=10,
            )
        except Exception:
            return {}
        items = self._network_items(value)
        lookups = 0
        fraud_events = 0
        hcaptcha = ""
        for item in items[-256:]:
            url = self._request_url(item)
            lower = url.lower()
            if "/consumers/sessions/lookup" in lower:
                lookups += 1
            if "m.stripe.com/6" in lower:
                fraud_events += 1
            body = self._request_body(item)
            if body and not hcaptcha and "confirmation_tokens" in lower:
                try:
                    parsed = parse_qs(body, keep_blank_values=True)
                    for key in (
                        "payment_method_data[radar_options][hcaptcha_token]",
                        "hcaptcha_token",
                    ):
                        candidate = str(parsed.get(key, [""])[0] or "").strip()
                        if len(candidate) >= 64:
                            hcaptcha = candidate
                            break
                except Exception:
                    pass
                if not hcaptcha:
                    try:
                        parsed_json = json.loads(body)
                        candidate = str(
                            parsed_json.get("hcaptcha_token")
                            or parsed_json.get("captcha_token")
                            or ""
                        ).strip() if isinstance(parsed_json, dict) else ""
                        if len(candidate) >= 64:
                            hcaptcha = candidate
                    except Exception:
                        pass
        result = {
            "lookup_count": lookups,
            "fraud_telemetry_count": fraud_events,
            "hcaptcha_present": bool(hcaptcha),
        }
        if hcaptcha:
            self._transport_session.momo_stripe_hcaptcha_token = hcaptcha
            self._transport_session.momo_stripe_hcaptcha_captured_at = time.time()
        self._transport_session.momo_browser_stripe_context = result
        return result

    def prepare_checkout_page(
        self, checkout_url: str, *, referer: str = ""
    ) -> dict[str, Any]:
        """Let the AT-injected real browser initialize checkout/Stripe Elements."""
        self.enable_browser_receipts()
        runner = getattr(self._delegate, "_run", None)
        open_args = getattr(self._delegate, "_enhanced_open_args", None)
        if not callable(runner) or not callable(open_args):
            return {}
        opened = False
        navigation_headers = {
            "Authorization": "Bearer " + str(self._backend_auth_config.get("token") or ""),
            "oai-device-id": str(self._backend_auth_config.get("device") or ""),
            "oai-session-id": str(self._backend_auth_config.get("session") or ""),
            "oai-language": str(self._backend_auth_config.get("language") or "vi-VN"),
            "oai-client-build-number": str(self._backend_auth_config.get("build") or "10109010"),
            "oai-client-version": str(self._backend_auth_config.get("version") or ""),
        }
        account = str(self._backend_auth_config.get("account") or "").strip()
        if account:
            navigation_headers["chatgpt-account-id"] = account
        try:
            runner(open_args(str(checkout_url), headers=navigation_headers))
            opened = True
        except Exception:
            # The route may keep a pending document navigation while its
            # background requests have already initialized; inspect it anyway.
            pass
        self._activate_backend_auth_bridge()
        self._apply_browser_timezone()
        self._warm_browser(
            max(
                250,
                min(
                    5000,
                    int(os.getenv("OPLL_MOMO_BROWSER_CHECKOUT_WARMUP_MS", "1500") or "1500"),
                ),
            )
        )
        self._sync_attestation_to_transport()
        try:
            self._delegate._sync_cookies()
        except Exception:
            pass
        purge_momo_browser_auth_cookies(self._transport_session)
        self._sync_browser_receipts()
        result = self._capture_browser_stripe_context()
        result["checkout_page_opened"] = opened
        self._transport_session.momo_browser_checkout_page = str(checkout_url)
        return result

    def _warm_browser(self, default_ms: int = 700) -> None:
        """Allow the AT-injected ChatGPT shell to finish read-only bootstrap calls."""
        runner = getattr(self._delegate, "_run", None)
        if not callable(runner):
            return
        try:
            value = int(
                os.getenv("OPLL_MOMO_BROWSER_WARMUP_MS", str(default_ms))
                or default_ms
            )
        except (TypeError, ValueError):
            value = default_ms
        value = max(0, min(value, 5000))
        if value:
            try:
                runner(["wait", str(value)], timeout=10)
            except Exception:
                pass

    def _apply_browser_timezone(self) -> None:
        """Apply the VN IANA timezone through the active Chrome CDP target."""
        if self._timezone_applied:
            return
        runner = getattr(self._delegate, "_run", None)
        if not callable(runner):
            return
        try:
            raw = runner(["get", "cdp-url", "--json"], timeout=10)
            data = raw.get("data") if isinstance(raw, dict) else None
            cdp_url = str(data.get("cdpUrl") or "") if isinstance(data, dict) else ""
            if not cdp_url:
                return
            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(cdp_url)
                pages = [
                    page
                    for context in browser.contexts
                    for page in context.pages
                ]
                page = next(
                    (
                        candidate
                        for candidate in pages
                        if "chatgpt.com" in str(candidate.url or "")
                    ),
                    pages[0] if pages else None,
                )
                if page is None:
                    return
                context = page.context
                cdp_session = context.new_cdp_session(page)
                cdp_session.send(
                    "Emulation.setTimezoneOverride",
                    {"timezoneId": str(self._delegate.timezone)},
                )
            self._timezone_applied = True
            setattr(self._transport_session, "momo_timezone_applied", True)
        except Exception:
            setattr(self._transport_session, "momo_timezone_applied", False)

    @property
    def enabled(self) -> bool:
        return bool(getattr(self._delegate, "enabled", False))

    def headers(self, flow: str, *, referer: str = "") -> dict[str, str]:
        self._activate_backend_auth_bridge()
        self._sync_transport_cookies_to_browser()
        try:
            result = dict(self._delegate.headers(flow, referer=referer) or {})
        except Exception as exc:
            self._browser_error = type(exc).__name__
            result = self._node_sentinel_headers(flow, referer)
        self._activate_backend_auth_bridge()
        self._sync_attestation_to_transport(
            result.get("oai-web-deployment-attestation", "")
        )
        purge_momo_browser_auth_cookies(self._transport_session)
        self._sync_browser_receipts()
        # The shared provider may replace the transport header with its latest
        # receipt.  Re-apply the Momo queue after merging browser observations.
        current_momo_pending_updates_header(self._transport_session)
        # In jar mode the HTTP session is authoritative for ChatGPT cookies.
        # An explicit browser snapshot would otherwise replace a newly learned
        # `_account` cookie on the wire.
        if bool(getattr(self._transport_session, "momo_cookie_jar_mode", False)):
            result.pop("Cookie", None)
            result.pop("cookie", None)
        return result

    def prepare(self) -> None:
        self._reset_stale_browser_context()
        self._sync_transport_cookies_to_browser()
        prepare = getattr(self._delegate, "prepare", None)
        if callable(prepare):
            try:
                prepare()
            except Exception as exc:
                self._browser_error = type(exc).__name__
                if not self._node_fallback_enabled:
                    raise
        self._activate_backend_auth_bridge()
        self._apply_browser_timezone()
        self._warm_browser()
        self._sync_attestation_to_transport()
        purge_momo_browser_auth_cookies(self._transport_session)
        self._sync_browser_receipts()
        current_momo_pending_updates_header(self._transport_session)

    def prepare_flow(self, *, flow: str, referer: str = "") -> None:
        self._sync_transport_cookies_to_browser()
        prepare_flow = getattr(self._delegate, "prepare_flow", None)
        if callable(prepare_flow):
            try:
                prepare_flow(flow=flow, referer=referer)
            except Exception as exc:
                self._browser_error = type(exc).__name__
                if not self._node_fallback_enabled:
                    raise
        self._activate_backend_auth_bridge()
        self._apply_browser_timezone()
        self._warm_browser(250)
        self._sync_attestation_to_transport()
        purge_momo_browser_auth_cookies(self._transport_session)
        self._sync_browser_receipts()
        current_momo_pending_updates_header(self._transport_session)

    def set_cookie(self, name: str, value: str, *, http_only: bool = False) -> None:
        setter = getattr(self._delegate, "set_cookie", None)
        if callable(setter):
            setter(name, value, http_only=http_only)

    def close(self) -> None:
        self._delegate.close()


MOMO_BROWSER_PROFILES: tuple[dict[str, str], ...] = (
    {
        "name": "chrome152",
        "impersonate": "chrome152",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="152", "Not?A_Brand";v="24", "Google Chrome";v="152"',
    },
    {
        "name": "chrome150",
        "impersonate": "chrome150",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="150", "Not?A_Brand";v="24", "Google Chrome";v="150"',
    },
    {
        "name": "chrome145",
        "impersonate": "chrome145",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="145", "Not?A_Brand";v="24", "Google Chrome";v="145"',
    },
    {
        "name": "chrome146",
        "impersonate": "chrome146",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="146", "Not?A_Brand";v="24", "Google Chrome";v="146"',
    },
    {
        "name": "chrome136",
        "impersonate": "chrome136",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="136", "Not?A_Brand";v="24", "Google Chrome";v="136"',
    },
)

# curl_cffi 0.16.1 has no chrome152 impersonator. Keep the captured label as
# an explicit compatibility alias to the fully supported Chrome150 pair until
# the HTTP runtime adds that profile.
MOMO_BROWSER_ALIASES: dict[str, str] = {"chrome152": "chrome150"}
MOMO_HTTP_PROFILE_NAMES = frozenset({"chrome136", "chrome145", "chrome146", "chrome150"})


class MomoTransportFactory:
    """Create isolated ChatGPT, Stripe and Momo sessions for one attempt."""

    def __init__(self, fingerprint: str = "") -> None:
        requested = str(fingerprint or "").strip().lower()
        requested = MOMO_BROWSER_ALIASES.get(requested, requested)
        matches = [p for p in MOMO_BROWSER_PROFILES if requested == p["name"]]
        if not matches:
            matches = [p for p in MOMO_BROWSER_PROFILES if requested == p["impersonate"]]
        # Rotate only among curl_cffi-supported coherent TLS/UA pairs when no
        # explicit profile was requested.  The captured Chrome152 label remains
        # an alias because curl_cffi 0.16.1 raises at request time for that
        # impersonator.  The installed browser can still be selected separately
        # for a native-UA experiment through OPLL_MOMO_NATIVE_BROWSER_UA.
        supported = tuple(
            item
            for item in MOMO_BROWSER_PROFILES
            if item["name"] in MOMO_HTTP_PROFILE_NAMES
        )
        self.profile = dict(matches[0] if matches else secrets.choice(supported))

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
                "Priority": "u=1, i",
                "sec-fetch-dest": "empty",
                "sec-fetch-mode": "cors",
                "sec-fetch-site": "same-origin",
            }
        )
        session.momo_pending_updates = _pending_update_values(
            session.headers.get("x-oai-is-pending-updates")
        )
        current_momo_pending_updates_header(session)
        # Let the HTTP client's cookie jar merge Set-Cookie updates from the
        # account/checkout responses.  A fixed Cookie header would freeze the
        # pre-Sentinel state and break the same-session binding.
        try:
            session.cookies.set("oai-did", device_id, domain=".chatgpt.com", path="/")
        except Exception:
            session.headers["Cookie"] = f"oai-did={device_id}"
        session.momo_cookie_jar_mode = True
        # BrowserSentinelProvider is shared with the legacy channels.  Give it
        # a data-only allowlist so an AT-only MoMo run never copies login/OAuth
        # cookie chunks back into the HTTP jar.
        session.momo_cookie_allowlist = MOMO_BROWSER_COOKIE_ALLOWLIST
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
                    session.momo_backend_auth_bridge_enabled = bool(
                        getattr(
                            session.openai_sentinel_provider,
                            "_backend_auth_bridge_enabled",
                            False,
                        )
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
                "Priority": "u=1, i",
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
                "Priority": "u=1, i",
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
    is_chatgpt_backend = "chatgpt.com/backend-api/" in normalized_url
    if is_chatgpt_backend:
        target_path = str(merged.get("x-openai-target-path") or "").strip()
        if target_path:
            merged["x-openai-target-route"] = momo_target_route(target_path)
    if is_chatgpt_backend and str(method).upper() == "GET":
        # Browser GET/XHR requests carry no JSON entity headers.
        merged.setdefault("Accept", "*/*")
        merged["Origin"] = None
        merged["Content-Type"] = None
    payment_phase = is_chatgpt_backend and normalized_url.endswith(
        (
            "/backend-api/payments/checkout/taxes",
            "/backend-api/payments/checkout/confirm",
        )
    )
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
    attestation_phase = is_chatgpt_backend and (
        normalized_url.endswith("/backend-api/payments/checkout")
        or "/backend-api/payments/checkout/" in normalized_url
    )
    if attestation_phase:
        attestation = str(
            getattr(session, "openai_web_deployment_attestation", "")
            or _header_value(getattr(session, "headers", None), "oai-web-deployment-attestation")
            or os.getenv("OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION", "")
        ).strip()
        if attestation:
            merged["oai-web-deployment-attestation"] = attestation
    if is_chatgpt_backend:
        # Apply the envelope after Sentinel/browser synchronization so receipts
        # observed during the same flow are present on the outgoing request.
        echo_payment_pending = os.getenv(
            "OPLL_MOMO_ECHO_PAYMENT_PENDING_UPDATES", "0"
        ).strip().lower() in {"1", "true", "yes", "on"}
        pending_header = (
            _set_pending_update_values(session, [])
            if payment_phase and not echo_payment_pending
            else current_momo_pending_updates_header(session)
        )
        merged["x-oai-is-pending-updates"] = pending_header
        try:
            session.momo_last_request_pending_updates = len(
                _pending_update_values(pending_header)
            )
        except Exception:
            pass
    try:
        session.momo_last_request_headers = dict(merged)
    except Exception:
        pass
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
