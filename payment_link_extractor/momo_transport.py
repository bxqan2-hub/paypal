from __future__ import annotations

"""Momo-only HTTP sessions with one proxy and one cookie jar per attempt."""

from typing import Any
from urllib.parse import quote

import requests

from .config import DEFAULT_USER_AGENT


class MomoTransportFactory:
    """Create isolated ChatGPT, Stripe and Momo sessions for one attempt."""

    def chatgpt(self, config: Any, proxy: str) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "Authorization": f"Bearer {config.access_token}",
                "User-Agent": DEFAULT_USER_AGENT,
                "Accept": "application/json",
                "Origin": "https://chatgpt.com",
            }
        )
        _set_proxy(session, proxy)
        return session

    def stripe(self, config: Any) -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": DEFAULT_USER_AGENT,
                "Origin": "https://checkout.stripe.com",
                "Referer": "https://checkout.stripe.com/",
            }
        )
        _set_proxy(session, config.checkout_proxy)
        return session

    def momo(self, config: Any) -> requests.Session:
        session = requests.Session()
        session.headers.update({"User-Agent": DEFAULT_USER_AGENT, "Accept": "text/html,application/json"})
        _set_proxy(session, config.checkout_proxy)
        return session


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
