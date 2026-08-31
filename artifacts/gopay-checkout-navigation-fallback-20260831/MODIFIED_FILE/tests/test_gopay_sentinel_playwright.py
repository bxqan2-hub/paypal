from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

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


def test_session_cookie_binding_probe_uses_cookies_without_bearer_header() -> None:
    calls: list[dict[str, object]] = []

    class Page:
        async def evaluate(self, expression, expected):
            calls.append({"expression": expression, "expected": expected})
            return "matched"

    page = Page()
    assert asyncio.run(
        sentinel._verify_session_cookie_binding(page, "user-fixture")
    ) == "matched"
    assert calls[0]["expected"] == "user-fixture"
    assert "credentials: 'include'" in calls[0]["expression"]
    assert "authorization" not in calls[0]["expression"].lower()


def test_session_cookie_binding_probe_rejects_a_different_user() -> None:
    class Page:
        async def evaluate(self, *_args):
            return "mismatched"

    assert asyncio.run(
        sentinel._verify_session_cookie_binding(Page(), "expected-user")
    ) == "mismatched"


def test_session_cookie_binding_probe_keeps_unavailable_distinct() -> None:
    class Page:
        async def evaluate(self, *_args):
            raise TimeoutError("fixture timeout")

    assert asyncio.run(
        sentinel._verify_session_cookie_binding(Page(), "expected-user")
    ) == "unavailable"
    assert asyncio.run(
        sentinel._verify_session_cookie_binding(Page(), "")
    ) == "identity_missing"


def test_session_cookie_binding_retries_unavailable_in_same_runtime() -> None:
    class Page:
        def __init__(self):
            self.states = iter(("unavailable", "unavailable", "matched"))
            self.calls = 0

        async def evaluate(self, *_args):
            self.calls += 1
            return next(self.states)

    page = Page()
    state = asyncio.run(
        sentinel._verify_session_cookie_binding_with_retry(
            page,
            "expected-user",
            cookie_backed=True,
            attempts=3,
            delay_seconds=0,
        )
    )
    assert state == "matched"
    assert page.calls == 3


def test_session_cookie_binding_skips_network_without_nextauth() -> None:
    class Page:
        async def evaluate(self, *_args):
            raise AssertionError("AT-only binding must not issue cookie probe")

    assert asyncio.run(
        sentinel._verify_session_cookie_binding_with_retry(
            Page(),
            "expected-user",
            cookie_backed=False,
        )
    ) == "not_present"


def test_imported_session_cookies_replace_stale_nextauth_chunks_only() -> None:
    events: list[tuple[str, object]] = []

    class Context:
        async def clear_cookies(self, **kwargs):
            events.append(("clear", kwargs["name"]))

        async def add_cookies(self, cookies):
            events.append(("add", cookies))

    imported = [
        {
            "name": "__Secure-next-auth.session-token.0",
            "value": "chunk-zero",
            "domain": ".chatgpt.com",
            "path": "/",
        }
    ]
    asyncio.run(
        sentinel._replace_imported_session_cookies(
            Context(), imported, replace_nextauth=True
        )
    )
    assert [name for name, _value in events] == ["clear", "add"]
    assert events[0][1].match("__Secure-next-auth.session-token.2")
    assert not events[0][1].match("__cf_bm")
    assert events[1][1] == imported


def test_explicit_nextauth_chunks_do_not_reimport_stale_header_chunks() -> None:
    cookies, replace_nextauth, explicit_nextauth = sentinel._browser_session_cookies(
        (
            "__cf_bm=cloudflare; "
            "__Secure-next-auth.session-token.0=stale-zero; "
            "__Secure-next-auth.session-token.2=stale-two"
        ),
        "device-fixture",
        "",
        (
            ("__Secure-next-auth.session-token.0", "fresh-zero"),
            ("__Secure-next-auth.session-token.1", "fresh-one"),
        ),
    )
    by_name = {item["name"]: item["value"] for item in cookies}
    assert replace_nextauth is True
    assert explicit_nextauth is True
    assert by_name["__cf_bm"] == "cloudflare"
    assert by_name["__Secure-next-auth.session-token.0"] == "fresh-zero"
    assert by_name["__Secure-next-auth.session-token.1"] == "fresh-one"
    assert "__Secure-next-auth.session-token.2" not in by_name


def test_auxiliary_browser_cookies_do_not_replace_profile_nextauth() -> None:
    cookies, replace_nextauth, explicit_nextauth = sentinel._browser_session_cookies(
        "__Secure-next-auth.session-token.0=profile-zero",
        "device-fixture",
        "",
        (("__Secure-next-auth.callback-url", "callback"),),
    )
    by_name = {item["name"]: item for item in cookies}
    assert replace_nextauth is False
    assert explicit_nextauth is False
    assert by_name["__Secure-next-auth.session-token.0"]["value"] == "profile-zero"
    assert by_name["__Secure-next-auth.callback-url"]["httpOnly"] is False


def test_at_bound_fallback_discards_stale_profile_nextauth() -> None:
    cookies, replace_nextauth, explicit_nextauth = sentinel._browser_session_cookies(
        "__Secure-next-auth.session-token.0=stale; __cf_bm=cloudflare",
        "device-fixture",
        "",
        (),
        discard_nextauth=True,
    )
    names = {item["name"] for item in cookies}
    assert replace_nextauth is True
    assert explicit_nextauth is False
    assert "__Secure-next-auth.session-token.0" not in names
    assert "__cf_bm" in names


def test_cookie_backed_bootstrap_omits_bearer_and_account_headers() -> None:
    headers = sentinel._bootstrap_headers(
        access_token="token-fixture",
        account_id="account-fixture",
        device_id="device-fixture",
        session_id="session-fixture",
        language="id-ID",
        attestation="a" * 291,
        cookie_backed=True,
    )
    assert "Authorization" not in headers
    assert "chatgpt-account-id" not in headers
    assert headers["oai-device-id"] == "device-fixture"
    assert len(headers["oai-web-deployment-attestation"]) == 291


def test_at_only_bootstrap_keeps_explicit_bearer_fallback() -> None:
    headers = sentinel._bootstrap_headers(
        access_token="token-fixture",
        account_id="account-fixture",
        device_id="device-fixture",
        session_id="session-fixture",
        language="id-ID",
        attestation="",
        cookie_backed=False,
    )
    assert headers["Authorization"] == "Bearer token-fixture"
    assert headers["chatgpt-account-id"] == "account-fixture"


def test_at_only_bearer_probe_keeps_non_nextauth_browser_cookies() -> None:
    source = Path(sentinel.__file__).read_text(encoding="utf-8")
    assert "credentials: cookieBacked ? 'omit' : 'include'" in source
    assert "'chatgpt-account-id': account" in source


def test_at_only_checkout_navigation_timeout_uses_same_page_fallback(
    monkeypatch,
) -> None:
    events: list[object] = []

    class Page:
        url = "https://chatgpt.com/?promo_campaign=plus-1-month-free"

        async def goto(self, url, **kwargs):
            events.append(("goto", url, kwargs["timeout"]))
            raise sentinel.PlaywrightTimeoutError("fixture timeout")

        async def evaluate(self, expression, *args):
            events.append(("evaluate", expression, args))
            return None

    class Context:
        async def set_extra_http_headers(self, headers):
            events.append(("headers", dict(headers)))

    async def no_op(*_args, **_kwargs):
        return None

    async def binding(*_args, **_kwargs):
        return "unavailable"

    daemon = object.__new__(sentinel.PersistentPlaywrightDaemon)
    monkeypatch.setattr(daemon, "_install_sdk", no_op)
    monkeypatch.setattr(daemon, "_capture_page_attestation", no_op)
    monkeypatch.setattr(
        sentinel, "_verify_session_cookie_binding_with_retry", binding
    )
    monkeypatch.setenv("OPLL_GOPAY_CHECKOUT_NAVIGATION_TIMEOUT_MS", "5000")
    session = sentinel._BrowserSession(
        context=Context(),
        page=Page(),
        profile_path=Path("runtime-profile"),
        device_id="device-fixture",
        expected_user_id="user-fixture",
        bootstrap_headers={"Authorization": "Bearer fixture"},
        cookie_backed=False,
    )
    target = "https://chatgpt.com/checkout/openai_llc/cs_fixture"
    asyncio.run(daemon._set_page_url(session, target))
    assert session.checkout_navigation_fallback is True
    assert session.checkout_navigation_error == "timeout"
    assert session.active_page_url == target
    assert ("headers", {}) in events
    assert sum(item[0] == "goto" for item in events) == 1
    stop_index = next(
        index
        for index, item in enumerate(events)
        if item[0] == "evaluate" and "window.stop" in item[1]
    )
    history_index = next(
        index
        for index, item in enumerate(events)
        if item[0] == "evaluate" and "history.replaceState" in item[1]
    )
    assert stop_index < history_index
    assert any(
        item[0] == "evaluate" and "history.replaceState" in item[1]
        for item in events
    )


def test_cookie_backed_checkout_navigation_timeout_still_fails(
    monkeypatch,
) -> None:
    class Page:
        url = "https://chatgpt.com/"

        async def goto(self, *_args, **_kwargs):
            raise sentinel.PlaywrightTimeoutError("fixture timeout")

    class Context:
        async def set_extra_http_headers(self, _headers):
            return None

    daemon = object.__new__(sentinel.PersistentPlaywrightDaemon)
    session = sentinel._BrowserSession(
        context=Context(),
        page=Page(),
        profile_path=Path("runtime-profile"),
        device_id="device-fixture",
        expected_user_id="user-fixture",
        cookie_backed=True,
    )
    with pytest.raises(sentinel.PlaywrightTimeoutError):
        asyncio.run(
            daemon._set_page_url(
                session,
                "https://chatgpt.com/checkout/openai_llc/cs_fixture",
            )
        )
    assert session.checkout_navigation_fallback is False


def test_checkout_navigation_timeout_configuration_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_GOPAY_CHECKOUT_NAVIGATION_TIMEOUT_MS", "invalid")
    assert sentinel._checkout_navigation_timeout_ms() == 45_000
    monkeypatch.setenv("OPLL_GOPAY_CHECKOUT_NAVIGATION_TIMEOUT_MS", "1")
    assert sentinel._checkout_navigation_timeout_ms() == 5_000
    monkeypatch.setenv("OPLL_GOPAY_CHECKOUT_NAVIGATION_TIMEOUT_MS", "999999")
    assert sentinel._checkout_navigation_timeout_ms() == 90_000
