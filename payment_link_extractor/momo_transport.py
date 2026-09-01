from __future__ import annotations

"""Momo-only HTTP sessions with one proxy and one cookie jar per attempt."""

from typing import Any
import secrets
from urllib.parse import quote

import requests

from .config import DEFAULT_USER_AGENT


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
        "name": "chrome136",
        "impersonate": "chrome136",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="136", "Google Chrome";v="136", "Not:A-Brand";v="99"',
    },
)


class MomoTransportFactory:
    """Create isolated ChatGPT, Stripe and Momo sessions for one attempt."""

    def __init__(self, fingerprint: str = "") -> None:
        requested = str(fingerprint or "").strip().lower()
        matches = [p for p in MOMO_BROWSER_PROFILES if requested in {p["name"], p["impersonate"]}]
        self.profile = dict(matches[0] if matches else secrets.choice(MOMO_BROWSER_PROFILES))

    def chatgpt(self, config: Any, proxy: str) -> requests.Session:
        session = _new_session(self.profile["impersonate"])
        session.headers.update(
            {
                "Authorization": f"Bearer {config.access_token}",
                "User-Agent": self.profile["user_agent"],
                "Accept": "application/json",
                "Origin": "https://chatgpt.com",
                "Accept-Language": "vi-VN,vi;q=0.9,en-US;q=0.8,en;q=0.7",
                "Sec-CH-UA": self.profile["sec_ch_ua"],
                "Sec-CH-UA-Mobile": "?0",
                "Sec-CH-UA-Platform": '"Windows"',
            }
        )
        _set_proxy(session, proxy)
        return session

    def stripe(self, config: Any) -> requests.Session:
        session = _new_session(self.profile["impersonate"])
        session.headers.update(
            {
                "User-Agent": self.profile["user_agent"],
                "Origin": "https://checkout.stripe.com",
                "Referer": "https://checkout.stripe.com/",
            }
        )
        _set_proxy(session, config.checkout_proxy)
        return session

    def momo(self, config: Any) -> requests.Session:
        session = _new_session(self.profile["impersonate"])
        session.headers.update({"User-Agent": self.profile["user_agent"], "Accept": "text/html,application/json", "Accept-Language": "vi-VN,vi;q=0.9,en;q=0.8"})
        _set_proxy(session, config.checkout_proxy)
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


def close(session: Any) -> None:
    if session is not None and callable(getattr(session, "close", None)):
        session.close()
