from __future__ import annotations

from pathlib import Path

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


def test_playwright_runtime_uses_persistent_profile_and_real_sdk_url() -> None:
    source = Path(sentinel.__file__).read_text(encoding="utf-8")
    assert "asyncio.run_coroutine_threadsafe" in source
    assert "launch_persistent_context" in source
    assert "gopay-sentinel-profiles" in source
    assert "/sentinel/{SENTINEL_SDK_VERSION}/sdk.js" in source
    assert '"headless": headless' in source
    assert '"chrome"' in source
    assert "jsdom" not in source.lower()
