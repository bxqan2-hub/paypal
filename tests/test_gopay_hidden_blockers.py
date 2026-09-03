from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from payment_link_extractor import cli
from payment_link_extractor import gopay_checkout
from payment_link_extractor import gopay_core
from payment_link_extractor import gopay_cs_live
from payment_link_extractor import gopay_oaics
from payment_link_extractor import gopay_sentinel_playwright
from payment_link_extractor import gopay_transport
from payment_link_extractor.auth import extract_session_token
from payment_link_extractor.errors import ProtocolError
from payment_link_extractor.gopay_checkout import CheckoutCreateError
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.web.tasks import TaskManager


def test_session_token_extracts_cookie_arrays_and_headers() -> None:
    assert extract_session_token(
        {
            "cookies": [
                {"name": "__Secure-next-auth.session-token.1", "value": "part-b"},
                {"name": "__Secure-next-auth.session-token.0", "value": "part-a"},
            ]
        }
    ) == "part-apart-b"
    assert extract_session_token(
        {"cookie": "__Secure-next-auth.session-token.0=part-a; __Secure-next-auth.session-token.1=part-b"}
    ) == "part-apart-b"
    assert extract_session_token(
        '{"cookies":[{"name":"__Secure-next-auth.session-token.1","value":"part-b"},{"name":"__Secure-next-auth.session-token.0","value":"part-a"}]}'
    ) == "part-apart-b"
    assert extract_session_token(
        '{"cookie":"__Secure-next-auth.session-token=part-a"}'
    ) == "part-a"
    assert extract_session_token(
        '"__Secure-next-auth.session-token=part-a"'
    ) == "part-a"
    assert extract_session_token(
        '{"cookie_header":["__Secure-next-auth.session-token=part-a"]}'
    ) == "part-a"
    assert extract_session_token(
        {
            "session_token": '{"cookies":[{"name":"__Secure-next-auth.session-token.0","value":"part-a"}]}'
        }
    ) == "part-a"
    assert extract_session_token(
        {"session_token": '"__Secure-next-auth.session-token=part-a"'}
    ) == "part-a"
    assert extract_session_token({"session_token": '"opaque-session"'}) == "opaque-session"


def test_playwright_session_cookie_chunks_and_version_guard() -> None:
    records = gopay_sentinel_playwright._nextauth_cookie_records("x" * 7600)
    assert [item["name"] for item in records] == [
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
    ]
    assert [len(item["value"]) for item in records] == [3800, 3800]
    assert gopay_sentinel_playwright._browser_version_matches(
        "Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36", "151.0.7922.170"
    )
    assert not gopay_sentinel_playwright._browser_version_matches(
        "Mozilla/5.0 Chrome/150.0.0.0 Safari/537.36", "151.0.7922.170"
    )


def test_playwright_loads_versioned_sentinel_sdk() -> None:
    urls: list[str] = []

    class Page:
        async def evaluate(self, _expression):
            return False

        async def add_script_tag(self, *, url):
            urls.append(url)

        async def wait_for_function(self, *_args, **_kwargs):
            return None

    daemon = object.__new__(gopay_sentinel_playwright.PersistentPlaywrightDaemon)
    asyncio.run(daemon._install_sdk(Page()))
    assert urls == [
        f"https://chatgpt.com/sentinel/{gopay_sentinel_playwright.SENTINEL_SDK_VERSION}/sdk.js"
    ]


def test_playwright_timeout_cancels_pending_call() -> None:
    cancelled: list[bool] = []

    class Future:
        def result(self, timeout):
            raise TimeoutError()

        def cancel(self):
            cancelled.append(True)

    async def noop():
        return None

    daemon = object.__new__(gopay_sentinel_playwright.PersistentPlaywrightDaemon)
    daemon._startup_error = None
    daemon._thread = SimpleNamespace(is_alive=lambda: True)
    daemon._loop = object()
    original = asyncio.run_coroutine_threadsafe
    asyncio.run_coroutine_threadsafe = lambda *_args, **_kwargs: Future()
    coroutine = noop()
    try:
        with pytest.raises(TimeoutError):
            daemon._call(coroutine, 1)
    finally:
        asyncio.run_coroutine_threadsafe = original
        coroutine.close()
    assert cancelled == [True]


def test_playwright_preserves_persistent_device_cookie() -> None:
    provider = object.__new__(gopay_transport.PlaywrightSentinelProvider)
    provider.device_id = "derived-device"
    provider._attestation = ""
    provider._cookies = ""
    provider._profile_path = ""
    provider._browser_channel = ""
    provider._browser_version = ""
    provider._challenge_shapes = []
    provider._sdk_sha256 = ""
    provider.transport_session = SimpleNamespace(headers={})
    provider._apply_runtime(
        {
            "device_id": "persisted-device",
            "cookie_header": "oai-did=persisted-device",
        }
    )
    assert provider.device_id == "persisted-device"
    assert provider.transport_session.headers["oai-device-id"] == "persisted-device"
    assert provider.transport_session.headers["Cookie"] == "oai-did=persisted-device"


def test_playwright_syncs_bootstrap_session_id() -> None:
    class Page:
        async def evaluate(self, _expression):
            return {
                "attestation": "a" * 291,
                "sessionId": "33333333-3333-4333-8333-333333333333",
            }

    session = SimpleNamespace(
        page=Page(),
        attestation="",
        session_id="22222222-2222-4222-8222-222222222222",
        bootstrap_headers={
            "oai-session-id": "22222222-2222-4222-8222-222222222222"
        },
    )
    daemon = object.__new__(gopay_sentinel_playwright.PersistentPlaywrightDaemon)
    asyncio.run(daemon._capture_page_attestation(session))
    assert session.attestation == "a" * 291
    assert session.session_id == "33333333-3333-4333-8333-333333333333"
    assert session.bootstrap_headers["oai-web-deployment-attestation"] == "a" * 291
    assert session.bootstrap_headers["oai-session-id"] == session.session_id


def test_playwright_applies_bootstrap_session_id_to_transport() -> None:
    provider = object.__new__(gopay_transport.PlaywrightSentinelProvider)
    provider.device_id = "device-fixture"
    provider.session_id = "old-session"
    provider._attestation = ""
    provider._cookies = ""
    provider._profile_path = ""
    provider._browser_channel = ""
    provider._browser_version = ""
    provider._challenge_shapes = []
    provider._sdk_sha256 = ""
    provider.transport_session = SimpleNamespace(headers={})
    provider._apply_runtime(
        {"session_id": "33333333-3333-4333-8333-333333333333"}
    )
    assert provider.session_id == "33333333-3333-4333-8333-333333333333"
    assert provider.transport_session.openai_session_id == provider.session_id
    assert provider.transport_session.headers["oai-session-id"] == provider.session_id


def test_playwright_runtime_refresh_clears_stale_attestation_and_cookie() -> None:
    provider = object.__new__(gopay_transport.PlaywrightSentinelProvider)
    provider.device_id = "device-fixture"
    provider.session_id = "session-fixture"
    provider._attestation = "stale-attestation"
    provider._cookies = "stale-cookie=value"
    provider._profile_path = ""
    provider._browser_channel = ""
    provider._browser_version = ""
    provider._challenge_shapes = []
    provider._sdk_sha256 = ""
    provider.transport_session = SimpleNamespace(headers={})
    provider._apply_runtime({"attestation": "", "cookie_header": ""})
    assert provider._attestation == ""
    assert provider._cookies == "oai-did=device-fixture"
    assert provider.transport_session.headers["Cookie"] == "oai-did=device-fixture"


def test_playwright_keeps_session_id_when_bootstrap_value_is_invalid() -> None:
    class Page:
        async def evaluate(self, _expression):
            return {"attestation": "", "sessionId": "not-a-uuid"}

    original = "22222222-2222-4222-8222-222222222222"
    session = SimpleNamespace(
        page=Page(),
        attestation="",
        session_id=original,
        bootstrap_headers={"oai-session-id": original},
    )
    daemon = object.__new__(gopay_sentinel_playwright.PersistentPlaywrightDaemon)
    asyncio.run(daemon._capture_page_attestation(session))
    assert session.session_id == original
    assert session.bootstrap_headers["oai-session-id"] == original


def test_playwright_ignores_non_payment_receipts() -> None:
    class Response:
        def __init__(self, url: str):
            self.url = url

        async def all_headers(self):
            return {"x-oai-is-receipt": "receipt-fixture"}

    daemon = object.__new__(gopay_sentinel_playwright.PersistentPlaywrightDaemon)
    daemon._sessions = {
        "runtime": SimpleNamespace(latest_receipt="")
    }
    asyncio.run(
        daemon._capture_response(
            "runtime", Response("https://evil.example/anything")
        )
    )
    assert daemon._sessions["runtime"].latest_receipt == ""
    asyncio.run(
        daemon._capture_response(
            "runtime",
            Response("https://chatgpt.com/backend-api/payments/checkout"),
        )
    )
    assert daemon._sessions["runtime"].latest_receipt == "receipt-fixture"


def test_playwright_ignores_foreign_attestation_headers() -> None:
    class Request:
        url = "https://evil.example/anything"
        method = "GET"
        post_data = ""

        async def all_headers(self):
            return {"oai-web-deployment-attestation": "foreign-attestation"}

    daemon = object.__new__(gopay_sentinel_playwright.PersistentPlaywrightDaemon)
    daemon._sessions = {
        "runtime": SimpleNamespace(attestation="", request_events=[])
    }
    asyncio.run(daemon._capture_request("runtime", Request()))
    assert daemon._sessions["runtime"].attestation == ""


@pytest.mark.parametrize("session_token", ["session-fixture", ""])
def test_playwright_cleans_failed_open_session_and_separates_cookie_auth(
    monkeypatch, tmp_path, session_token
) -> None:
    captured_headers: list[dict[str, str]] = []
    launch_kwargs: dict[str, object] = {}

    class Page:
        url = "https://chatgpt.com/"

        async def goto(self, *_args, **_kwargs):
            raise RuntimeError("goto-fixture")

        async def evaluate(self, *_args, **_kwargs):
            return True

    class Context:
        browser = SimpleNamespace(version="151.0.7922.170")
        pages = [Page()]
        closed = False
        clear_calls: list[dict[str, str]] = []

        async def cookies(self, *_args):
            return [
                {
                    "name": "__Secure-next-auth.session-token.0",
                    "value": "old",
                }
            ]

        async def new_page(self):
            return self.pages[0]

        def on(self, *_args):
            return None

        async def add_cookies(self, _cookies):
            return None

        async def clear_cookies(self, **kwargs):
            self.clear_calls.append(dict(kwargs))

        async def set_extra_http_headers(self, headers):
            captured_headers.append(dict(headers))

        async def close(self):
            self.closed = True

    context = Context()

    class Chromium:
        async def launch_persistent_context(self, **kwargs):
            launch_kwargs.update(kwargs)
            return context

    daemon = object.__new__(gopay_sentinel_playwright.PersistentPlaywrightDaemon)
    daemon._playwright = SimpleNamespace(chromium=Chromium())
    daemon._sessions = {}
    monkeypatch.setenv("OPLL_GOPAY_SENTINEL_PROFILE_DIR", str(tmp_path))
    monkeypatch.setenv("OPLL_GOPAY_SENTINEL_BROWSER_CHANNEL", "")
    with pytest.raises(RuntimeError, match="goto-fixture"):
        asyncio.run(
            daemon._open_session_async(
                access_token="at-fixture",
                account_id="account-fixture",
                device_id="11111111-1111-4111-8111-111111111111",
                session_id="22222222-2222-4222-8222-222222222222",
                user_agent="Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36",
                browser_proxy="",
                session_token=session_token,
                language="id-ID",
                timezone="Asia/Jakarta",
            )
        )
    assert daemon._sessions == {}
    assert context.closed is True
    assert launch_kwargs["channel"] == "chrome"
    assert captured_headers
    assert "Authorization" not in captured_headers[0]
    assert "chatgpt-account-id" not in captured_headers[0]
    if session_token:
        assert context.clear_calls == [
            {
                "name": "__Secure-next-auth.session-token.0",
            }
        ]
    else:
        assert context.clear_calls == []


def test_gopay_client_contract_is_shared_by_http_and_browser() -> None:
    assert gopay_transport.GOPAY_OAI_CLIENT_BUILD_NUMBER == "10012890"
    assert gopay_transport.GOPAY_OAI_CLIENT_VERSION == (
        "prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4"
    )
    assert gopay_sentinel_playwright.GOPAY_OAI_CLIENT_BUILD_NUMBER == (
        gopay_transport.GOPAY_OAI_CLIENT_BUILD_NUMBER
    )
    assert gopay_sentinel_playwright.GOPAY_OAI_CLIENT_VERSION == (
        gopay_transport.GOPAY_OAI_CLIENT_VERSION
    )


def test_gopay_ignores_generic_cross_channel_identity_overrides(monkeypatch) -> None:
    class Session:
        def __init__(self, *_args, **_kwargs):
            self.headers: dict[str, str] = {}

    monkeypatch.setattr(gopay_transport, "new_session", Session)
    monkeypatch.setenv("OPLL_OAI_CLIENT_BUILD_NUMBER", "momo-build")
    monkeypatch.setenv("OPLL_OAI_CLIENT_VERSION", "momo-version")
    monkeypatch.setenv("OPLL_OAI_WEB_DEPLOYMENT_ATTESTATION", "momo-attestation")
    monkeypatch.setenv("OPLL_X_OAI_IS_PENDING_UPDATES", '{"v":3,"updates":["stale"]}')
    monkeypatch.setenv("OPLL_OAI_IS_CLIENT_OBSERVATION", "stale-observation")
    monkeypatch.setenv("OPLL_HTTP_IMPERSONATE", "chrome131")
    monkeypatch.setenv(
        "OPLL_USER_AGENT",
        "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36",
    )
    monkeypatch.setenv(
        "OPLL_SEC_CH_UA",
        '"Google Chrome";v="131", "Chromium";v="131"',
    )
    monkeypatch.setenv("OPLL_SEC_CH_UA_PLATFORM", '"Linux"')
    for name in (
        "OPLL_GOPAY_OAI_CLIENT_BUILD_NUMBER",
        "OPLL_GOPAY_OAI_CLIENT_VERSION",
        "OPLL_GOPAY_OAI_WEB_DEPLOYMENT_ATTESTATION",
        "OPLL_GOPAY_OAI_IS_CLIENT_OBSERVATION",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPLL_SENTINEL_BROWSER", "off")
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    session = gopay_transport.GoPayTransportFactory().chatgpt(
        config, config.checkout_proxy
    )
    assert session.headers["oai-client-build-number"] == "10012890"
    assert session.headers["oai-client-version"] == (
        "prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4"
    )
    assert session.headers["x-oai-is-pending-updates"] == gopay_transport.EMPTY_PENDING_UPDATES
    assert "oai-web-deployment-attestation" not in session.headers
    assert session.headers["x-oai-is-client-observation"] != "stale-observation"
    assert session.headers["User-Agent"] == gopay_transport.GOPAY_BROWSER_PROFILES[0]["user_agent"]
    assert session.headers["sec-ch-ua"] == gopay_transport.GOPAY_BROWSER_PROFILES[0]["sec_ch_ua"]
    assert session.headers["sec-ch-ua-platform"] == '"Windows"'
    assert session.gopay_tls_impersonate == "chrome151"


def test_gopay_device_id_is_stable_for_same_account_different_jwt_nonce(monkeypatch) -> None:
    import base64

    def token(nonce: str) -> str:
        payload = {
            "https://api.openai.com/auth": {"chatgpt_account_id": "account-fixture"},
            "nonce": nonce,
        }
        encoded = base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii").rstrip("=")
        return f"header.{encoded}.signature"

    monkeypatch.setenv("OPLL_HTTP_IMPERSONATE", "chrome151")
    first = ExtractionConfig(
        access_token=token("one"),
        checkout_proxy="",
        update_proxy="",
        country="ID",
        payment_method="gopay",
    )
    second = ExtractionConfig(
        access_token=token("two"),
        checkout_proxy="",
        update_proxy="",
        country="ID",
        payment_method="gopay",
    )
    first_device, first_profile = gopay_transport.gopay_browser_identity(first)
    second_device, second_profile = gopay_transport.gopay_browser_identity(second)
    assert first_device == second_device
    assert first_profile == second_profile


def test_web_route_uses_session_token_environment_fallback(monkeypatch) -> None:
    from payment_link_extractor.web import routes

    monkeypatch.setenv("OPLL_SESSION_TOKEN", "session-from-env")
    config = routes._config_from_payload(
        {
            "access_token": "at-fixture",
            "checkout_proxy": "http://proxy.example:8080",
            "update_proxy": "http://proxy.example:8080",
            "country": "ID",
            "payment_method": "gopay",
            "gopay_zero_trial_validation": False,
        }
    )
    assert config.session_token == "session-from-env"


def test_gopay_identity_does_not_fall_back_to_plain_requests(monkeypatch) -> None:
    monkeypatch.setattr(gopay_transport, "CurlCffiSession", None)
    with pytest.raises(Exception, match="curl_cffi is required"):
        gopay_transport.new_session("chrome151")


@pytest.mark.parametrize("method", ["chatgpt", "stripe"])
def test_gopay_session_constructor_type_error_is_not_downgraded(monkeypatch, method) -> None:
    calls: list[str] = []

    def failing_session(impersonate=None):
        calls.append(str(impersonate or ""))
        raise TypeError("unsupported profile")

    monkeypatch.delenv("OPLL_HTTP_IMPERSONATE", raising=False)
    monkeypatch.setattr(gopay_transport, "new_session", failing_session)
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    with pytest.raises(TypeError, match="unsupported profile"):
        if method == "chatgpt":
            gopay_transport.DefaultTransportFactory().chatgpt(config, config.checkout_proxy)
        else:
            gopay_transport.DefaultTransportFactory().stripe(config)
    assert calls == ["chrome151"]


@pytest.mark.parametrize("sec_ch_ua", ["", '"Not=A?Brand";v="99"'])
def test_gopay_client_hints_require_chrome_brands(sec_ch_ua) -> None:
    with pytest.raises(Exception, match="client-hints version mismatch"):
        gopay_transport.validate_gopay_client_hints(
            "Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36",
            sec_ch_ua,
        )


def test_required_sentinel_headers_reject_partial_provider_result(monkeypatch) -> None:
    session = SimpleNamespace(
        gopay_browser_profile="chrome151",
        openai_sentinel_provider=SimpleNamespace(
            headers=lambda *_args, **_kwargs: {"oai-device-id": "device-fixture"}
        ),
        headers={},
    )
    monkeypatch.delenv("OPLL_GOPAY_OPENAI_SENTINEL_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="browser Sentinel proof generation failed"):
        gopay_transport.openai_sentinel_headers(
            session,
            flow="chatgpt_checkout",
            required=True,
        )


def test_gopay_client_hints_reject_missing_chromium_brand() -> None:
    with pytest.raises(Exception, match="client-hints version mismatch"):
        gopay_transport.validate_gopay_client_hints(
            "Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36",
            '"Google Chrome";v="151"',
        )


def test_cli_keeps_gopay_switch_session_and_momo_captcha_separate(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_GOPAY_ZERO_TRIAL_VALIDATION", "false")
    monkeypatch.setenv("OPLL_SESSION_TOKEN", "session-fixture")
    monkeypatch.setenv("OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN", "momo-fixture")
    monkeypatch.delenv("OPLL_STRIPE_HCAPTCHA_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["opll", "--at", "at-fixture", "--payment-method", "gopay"],
    )
    args = cli.parse_args()
    assert args.gopay_zero_trial_validation is False
    assert args.session_token == "session-fixture"
    assert args.stripe_hcaptcha_token == ""


def test_gopay_checkout_adds_account_header_after_checkout_response(monkeypatch) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {
                "x-oai-is-pending-updates": '{"v":3,"updates":["stale"]}'
            }
            self.openai_pending_receipts = ["stale"]

    class Response:
        status_code = 200
        text = "{}"
        headers: dict[str, str] = {}

        def json(self):
            return {"checkout_session_id": "cs_fixture"}

    observed: list[bool] = []

    def request(session, *_args, **_kwargs):
        observed.append("chatgpt-account-id" in session.headers)
        return Response()

    monkeypatch.setattr(gopay_checkout, "account_id", lambda _token: "account-fixture")
    monkeypatch.setattr(
        gopay_checkout,
        "openai_sentinel_headers",
        lambda *_args, **_kwargs: {"OpenAI-Sentinel-Token": "proof-fixture"},
    )
    monkeypatch.setattr(gopay_checkout, "stage_http_request", request)
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
        gopay_zero_trial_validation=False,
    )
    session = Session()
    gopay_checkout.create_checkout(config, session, None)
    assert observed == [False]
    assert session.headers["chatgpt-account-id"] == "account-fixture"
    assert session.openai_pending_receipts == []
    assert session.headers["x-oai-is-pending-updates"] == gopay_transport.EMPTY_PENDING_UPDATES


def test_gopay_pending_receipts_are_bounded_and_acknowledged() -> None:
    class Response:
        status_code = 200
        text = "{}"

        def __init__(self, headers):
            self.headers = headers

    class Session:
        def __init__(self):
            self.headers = {"x-oai-is-pending-updates": '{"v":3,"updates":[]}'}
            self.responses = [
                Response({"x-oai-is-receipt": "receipt-a"}),
                Response({"x-oai-is-receipt": "receipt-b"}),
                Response({"x-oai-is-pending-updates-ack": "ack"}),
            ]

        def request(self, *_args, **_kwargs):
            return self.responses.pop(0)

    session = Session()
    gopay_transport.stage_http_request(session, "one", "GET", "https://chatgpt.com/one")
    gopay_transport.stage_http_request(session, "two", "GET", "https://chatgpt.com/two")
    assert json.loads(session.headers["x-oai-is-pending-updates"]) == {
        "v": 3,
        "updates": ["receipt-a", "receipt-b"],
    }
    gopay_transport.stage_http_request(session, "three", "GET", "https://chatgpt.com/three")
    assert session.headers["x-oai-is-pending-updates"] == gopay_transport.EMPTY_PENDING_UPDATES
    assert session.openai_pending_receipts == []


def test_gopay_approve_refresh_clears_pending_receipt_state(monkeypatch) -> None:
    class Session:
        def __init__(self, *_args, **_kwargs):
            self.headers: dict[str, str] = {}

    monkeypatch.setattr(gopay_transport, "new_session", Session)
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    session = gopay_transport.GoPayTransportFactory().chatgpt(
        config, config.checkout_proxy
    )
    session.openai_pending_receipts = ["old-a", "old-b"]
    session.headers["x-oai-is-pending-updates"] = (
        '{"v":3,"updates":["old-a","old-b"]}'
    )
    dynamic = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout/approve"
    )
    assert dynamic["x-oai-is-pending-updates"] == gopay_transport.EMPTY_PENDING_UPDATES
    assert session.openai_pending_receipts == []
    assert session.headers["x-oai-is-pending-updates"] == gopay_transport.EMPTY_PENDING_UPDATES


def test_gopay_tax_second_request_uses_session_pending_receipts(monkeypatch) -> None:
    calls: list[dict[str, str]] = []

    class Response:
        status_code = 200
        text = "{}"
        headers: dict[str, str] = {}

        def json(self):
            return {}

    def request(_session, *_args, **kwargs):
        calls.append(dict(kwargs["headers"]))
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", request)
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    checkout = {"cs_id": "cs_fixture", "billing_country": "ID"}
    billing = {
        "name": "Fixture",
        "email": "fixture@example.invalid",
        "country": "ID",
        "line1": "Street",
        "city": "Jakarta",
        "state": "Jawa",
        "postal_code": "10000",
    }
    gopay_cs_live.cs_checkout_taxes(config, object(), checkout, billing, None)
    gopay_cs_live.cs_checkout_taxes(
        config,
        object(),
        checkout,
        billing,
        None,
        use_pending_updates=True,
    )
    assert calls[0]["x-oai-is-pending-updates"] == gopay_transport.EMPTY_PENDING_UPDATES
    assert "x-oai-is-pending-updates" not in calls[1]


def test_gopay_core_missing_amount_is_explicit_when_validation_is_off(monkeypatch) -> None:
    class Session:
        def close(self):
            return None

    class Factory:
        def chatgpt(self, *_args):
            return Session()

        def stripe(self, *_args):
            return Session()

    monkeypatch.setattr(
        gopay_core,
        "create_checkout",
        lambda *_args, **_kwargs: {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "ID",
            "currency": "IDR",
        },
    )
    monkeypatch.setattr(gopay_core, "require_country_currency", lambda *_args: None)
    monkeypatch.setattr(gopay_core, "update_checkout", lambda *_args: {})
    monkeypatch.setattr(
        gopay_core,
        "extract_cs_live_provider",
        lambda *_args, **_kwargs: {"gopay_url": "https://app.midtrans.com/fixture"},
    )
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
        gopay_zero_trial_validation=False,
    )
    with pytest.raises(ProtocolError, match="did not return a payable amount"):
        gopay_core.extract_gopay_payment_link(config, transport_factory=Factory())


def test_gopay_core_marks_post_checkout_protocol_failure_retryable(monkeypatch) -> None:
    class Session:
        def close(self):
            return None

    class Factory:
        def chatgpt(self, *_args):
            return Session()

        def stripe(self, *_args):
            return Session()

    monkeypatch.setattr(
        gopay_core,
        "create_checkout",
        lambda *_args, **_kwargs: {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "ID",
            "currency": "IDR",
        },
    )
    monkeypatch.setattr(gopay_core, "require_country_currency", lambda *_args: None)
    monkeypatch.setattr(gopay_core, "update_checkout", lambda *_args: {})

    def fail(*_args, **_kwargs):
        raise ProtocolError(409, "approval blocked")

    monkeypatch.setattr(gopay_core, "extract_cs_live_provider", fail)
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
        gopay_zero_trial_validation=False,
    )
    with pytest.raises(ProtocolError) as caught:
        gopay_core.extract_gopay_payment_link(config, transport_factory=Factory())
    assert caught.value.retryable is True
    assert caught.value.failure_mode == "gopay_protocol_error"


def test_gopay_core_prepares_browser_identity_before_eligibility(monkeypatch) -> None:
    seen: list[str] = []
    flow_calls: list[dict[str, str]] = []

    class Session:
        def __init__(self) -> None:
            self.headers = {"oai-device-id": "derived-device"}
            self.openai_sentinel_provider = SimpleNamespace(
                prepare=lambda: self.headers.__setitem__("oai-device-id", "profile-device"),
                prepare_flow=lambda **kwargs: flow_calls.append(
                    {key: str(value) for key, value in kwargs.items()}
                ),
            )

        def close(self) -> None:
            return None

    class Factory:
        def chatgpt(self, *_args):
            return Session()

        def stripe(self, *_args):
            return Session()

    def eligibility(_config, chatgpt, _log):
        seen.append(chatgpt.headers["oai-device-id"])
        return {"state": "eligible"}

    def checkout(_config, _chatgpt, _log, **_kwargs):
        seen.append(_chatgpt.headers["oai-device-id"])
        return {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "ID",
            "currency": "IDR",
            "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
        }

    monkeypatch.setattr(gopay_core, "check_coupon_eligibility", eligibility)
    monkeypatch.setattr(gopay_core, "create_checkout", checkout)
    monkeypatch.setattr(gopay_core, "require_country_currency", lambda *_args: None)
    monkeypatch.setattr(gopay_core, "update_checkout", lambda *_args: {})
    monkeypatch.setattr(
        gopay_core,
        "extract_cs_live_provider",
        lambda *_args, **_kwargs: {
            "gopay_url": "https://app.midtrans.com/fixture",
        },
    )
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    gopay_core.extract_gopay_payment_link(config, transport_factory=Factory())
    assert seen == ["profile-device", "profile-device"]
    assert flow_calls == [
        {
            "flow": "chatgpt_checkout",
            "referer": "https://chatgpt.com/checkout/openai_ie/cs_fixture",
        }
    ]


def test_gopay_core_uses_payload_account_email_when_token_has_none(monkeypatch) -> None:
    class Session:
        def close(self):
            return None

    class Factory:
        def chatgpt(self, *_args):
            return Session()

        def stripe(self, *_args):
            return Session()

    monkeypatch.setattr(gopay_core, "account_email", lambda _token: "")
    monkeypatch.setattr(
        gopay_core,
        "create_checkout",
        lambda *_args, **_kwargs: {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "ID",
            "currency": "IDR",
            "checkout_state": {
                "total": {"total": {"minorUnitsAmount": 0}},
            },
        },
    )
    monkeypatch.setattr(gopay_core, "require_country_currency", lambda *_args: None)
    monkeypatch.setattr(gopay_core, "update_checkout", lambda *_args: {})
    monkeypatch.setattr(
        gopay_core,
        "extract_cs_live_provider",
        lambda *_args, **_kwargs: {
            "gopay_url": "https://app.midtrans.com/fixture",
        },
    )
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
        gopay_zero_trial_validation=False,
        account_email="payload@example.invalid",
    )
    result = gopay_core.extract_gopay_payment_link(
        config, transport_factory=Factory()
    )
    assert result.billing.email == "payload@example.invalid"


def test_gopay_core_rejects_provider_currency_drift(monkeypatch) -> None:
    class Session:
        def close(self) -> None:
            return None

    class Factory:
        def chatgpt(self, *_args):
            return Session()

        def stripe(self, *_args):
            return Session()

    monkeypatch.setattr(
        gopay_core,
        "create_checkout",
        lambda *_args, **_kwargs: {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "ID",
            "currency": "IDR",
            "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
        },
    )
    monkeypatch.setattr(
        gopay_core,
        "extract_cs_live_provider",
        lambda _config, _chatgpt, _stripe, checkout, *_args, **_kwargs: (
            checkout.update(currency="GBP")
            or {"gopay_url": "https://app.midtrans.com/fixture"}
        ),
    )
    monkeypatch.setattr(gopay_core, "update_checkout", lambda *_args: {})
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
        gopay_zero_trial_validation=False,
    )
    with pytest.raises(ProtocolError, match="checkout currency is not IDR"):
        gopay_core.extract_gopay_payment_link(config, transport_factory=Factory())


def test_task_redacts_gopay_session_token() -> None:
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
        session_token="session-fixture",
    )
    assert "session-fixture" in TaskManager._secrets(config)


def test_gopay_retryable_checkout_response_can_rotate_after_commit() -> None:
    calls: list[str] = []

    def extractor(config, *, cancel_event, stage_callback):
        del cancel_event
        calls.append(config.checkout_proxy)
        stage_callback("checkout_committed")
        if len(calls) == 1:
            raise CheckoutCreateError(
                429,
                "rate limited",
                failure_mode="rate_limited",
                retryable=True,
            )
        return {"ok": True, "amount_due_minor": 0}

    proxies = ("http://proxy-a.example:8080", "http://proxy-b.example:8080")
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy=proxies[0],
        update_proxy=proxies[0],
        country="ID",
        payment_method="gopay",
        retry_count=1,
        checkout_proxy_attempts=proxies,
        update_proxy_attempts=proxies,
        proxy_pool=proxies,
    )
    manager = TaskManager(extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        deadline = time.time() + 3
        snapshot = {}
        while time.time() < deadline:
            snapshot = manager.get(task_id) or {}
            if snapshot.get("status") in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        assert snapshot["status"] == "succeeded"
        assert snapshot["attempt"] == 2
        assert len(calls) == 2
    finally:
        manager.close()


def test_gopay_oaics_confirmation_uses_context_fingerprint(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        text = "{}"
        headers: dict[str, str] = {}

        def json(self):
            return {"id": "ctoken_fixture"}

    def request(_session, *_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_oaics, "stage_http_request", request)
    config = ExtractionConfig(
        access_token="at-fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    ctx = {
        "stripe_js_id": "js-fixture",
        "guid": "guid-fixture",
        "muid": "muid-fixture",
        "sid": "sid-fixture",
        "payment_method_types": ["gopay"],
        "elements_session_id": "elements-fixture",
        "elements_session_config_id": "config-fixture",
    }
    checkout = {"cs_id": "cs_fixture", "publishable_key": "pk_live_fixture"}
    billing = {
        "name": "Fixture",
        "email": "fixture@example.invalid",
        "phone": "000",
        "country": "ID",
        "line1": "Street",
        "city": "Jakarta",
        "state": "Jawa",
        "postal_code": "10000",
    }
    assert gopay_oaics.openai_confirmation_token(
        object(), config, checkout, billing, ctx, "gopay", None
    ) == "ctoken_fixture"
    body = captured["data"]
    assert body["payment_method_data[guid]"] == "guid-fixture"
    assert body["payment_method_data[muid]"] == "muid-fixture"
    assert body["payment_method_data[sid]"] == "sid-fixture"
    assert body["payment_method_data[billing_details][email]"] == "fixture@example.invalid"


def test_gopay_confirm_does_not_generate_unbound_fingerprint(monkeypatch) -> None:
    monkeypatch.setattr(
        gopay_cs_live,
        "stage_http_request",
        lambda *_args, **_kwargs: pytest.fail("confirm must not be sent"),
    )
    with pytest.raises(ProtocolError, match="fingerprint is incomplete"):
        gopay_cs_live.stripe_confirm_cs_live(
            object(),
            {"cs_id": "cs_fixture", "publishable_key": "pk_live_fixture"},
            {"id": "ppage_fixture"},
            {"stripe_js_id": "js-fixture"},
            "https://checkout.stripe.com/c/pay/cs_fixture",
            "gopay",
            {
                "name": "Fixture",
                "email": "fixture@example.invalid",
                "country": "ID",
                "line1": "Street",
                "city": "Jakarta",
                "postal_code": "10000",
            },
            None,
        )
