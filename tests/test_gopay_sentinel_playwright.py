from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from payment_link_extractor import gopay_sentinel_playwright as sentinel


def test_playwright_proxy_and_profile_are_stable() -> None:
    assert sentinel._proxy_options("http://user:pass@127.0.0.1:8080") == {
        "server": "http://127.0.0.1:8080",
        "username": "user",
        "password": "pass",
    }
    first = sentinel._profile_key("device-fixture")
    assert first == sentinel._profile_key("device-fixture")
    assert first != sentinel._profile_key("other-device")


def test_playwright_cookie_header_contains_no_empty_names() -> None:
    assert sentinel._cookie_header(
        [
            {"name": "oai-did", "value": "device"},
            {"name": "", "value": "ignored"},
            {"name": "__stripe_mid", "value": "mid"},
        ]
    ) == "oai-did=device; __stripe_mid=mid"


def test_playwright_headless_defaults_to_enabled(monkeypatch) -> None:
    monkeypatch.delenv("OPLL_SENTINEL_HEADLESS", raising=False)
    assert sentinel._enabled("", default=True) is True


def test_playwright_runtime_uses_persistent_profile_and_real_sdk_url() -> None:
    source = Path(sentinel.__file__).read_text(encoding="utf-8")
    assert "asyncio.run_coroutine_threadsafe" in source
    assert "launch_persistent_context" in source
    assert "gopay-sentinel-profiles" in source
    assert "/sentinel/{SENTINEL_SDK_VERSION}/sdk.js" in source
    assert '"headless": headless' in source
    assert '"chrome"' in source
    assert "jsdom" not in source.lower()


def test_checkout_navigation_timeout_falls_back_to_same_origin(monkeypatch) -> None:
    events: list[object] = []

    class Context:
        async def set_extra_http_headers(self, headers):
            events.append(("headers", headers))

    class Page:
        url = "https://chatgpt.com/checkout/openai_llc/cs_fixture"

        async def goto(self, *_args, **_kwargs):
            raise TimeoutError()

        async def evaluate(self, _expression, value):
            events.append(("history", value))

    daemon = object.__new__(sentinel.PersistentPlaywrightDaemon)

    async def install(_page):
        events.append("sdk")

    async def attestation(_session):
        events.append("attestation")

    monkeypatch.setattr(daemon, "_install_sdk", install)
    monkeypatch.setattr(daemon, "_capture_page_attestation", attestation)
    session = SimpleNamespace(
        context=Context(),
        page=Page(),
        active_page_url="https://chatgpt.com/",
        bootstrap_headers={"Authorization": "Bearer fixture"},
    )
    asyncio.run(
        daemon._set_page_url(
            session,
            "https://chatgpt.com/checkout/openai_llc/cs_fixture",
        )
    )
    assert events == [
        ("headers", {"Authorization": "Bearer fixture"}),
        ("headers", {}),
        "sdk",
        "attestation",
        ("history", "/checkout/openai_llc/cs_fixture"),
    ]
