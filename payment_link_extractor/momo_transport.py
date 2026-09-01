from __future__ import annotations

"""Momo-only HTTP sessions with one proxy and one cookie jar per attempt."""

from typing import Any
import secrets
from urllib.parse import quote

import requests

from .config import DEFAULT_USER_AGENT


MOMO_BROWSER_PROFILES: tuple[dict[str, str], ...] = (
    {
        "name": "chrome152",
        "impersonate": "chrome152",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Chromium";v="152", "Google Chrome";v="152", "Not(A:Brand";v="99"',
    },
    {
        "name": "chrome150",
        "impersonate": "chrome150",
        "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36",
        "sec_ch_ua": '"Not=A?Brand";v="99", "Google Chrome";v="150", "Chromium";v="150"',
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
    value = str(proxy or "").strip()
    if value and "://" not in value:
        parts = value.split(":")
        if len(parts) == 4 and all(parts):
            host, port, user, password = parts
            value = f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    if value:
        session.proxies.update({"http": value, "https": value})


def close(session: Any) -> None:
    if session is not None and callable(getattr(session, "close", None)):
        session.close()
