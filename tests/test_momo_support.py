from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest

from payment_link_extractor.application import _normalize_config
from payment_link_extractor.auth import extract_session_token
from payment_link_extractor.channels import PAYMENT_CHANNELS
from payment_link_extractor.config import billing_for_country, currency_minor_scale
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.momo_core import _gateway_session_id, query_gateway, validate_momo_amount
from payment_link_extractor.momo_eligibility import probe_momo_trial_eligibility
from payment_link_extractor.momo_stripe import MomoConfirmBlockedError, checkout_confirm, elements_session, intent_confirm, prepare_momo_link_context, resolve_momo_redirect, synchronize_momo_stripe_browser_ids, validate_momo_url
from payment_link_extractor.momo_checkout import create_checkout, hydrate_checkout_route, refresh_momo_customer_balance, taxes
from payment_link_extractor.momo_transport import MOMO_BROWSER_PROFILES, MOMO_EMPTY_PENDING_UPDATES, MomoTransportFactory, _set_proxy, capture_momo_csrf_token, clear_momo_pending_updates, current_momo_pending_updates_header, momo_gateway_headers, momo_gateway_page_headers, momo_request_headers, momo_target_route, normalize_momo_proxy, purge_momo_browser_auth_cookies, record_momo_pending_updates, seed_momo_account_cookie
from payment_link_extractor.web.app import create_app
from payment_link_extractor.web.tasks import TaskManager
from payment_link_extractor.web.routes import _config_from_payload


def test_momo_registry_and_fixed_country() -> None:
    channel = PAYMENT_CHANNELS["momo"]
    assert channel.adapter_module == "payment_link_extractor.momo_channel"
    assert channel.result_field == "momo_url"
    assert (channel.country, channel.currency) == ("VN", "VND")
    assert channel.uses_legacy_transport is False
    assert channel.uses_checkout_update is False
    config = _normalize_config(ExtractionConfig("token", "http://proxy", "", country="US", payment_method="momo"))
    assert (config.country, config.payment_method) == ("VN", "momo")
    assert config.momo_zero_trial_validation is True
    assert config.momo_trial_eligibility_check is True


def test_session_cookie_chunks_are_reassembled_for_momo_browser_context() -> None:
    assert extract_session_token(
        {
            "__Secure-next-auth.session-token.1": "part-b",
            "__Secure-next-auth.session-token.0": "part-a",
        }
    ) == "part-apart-b"
    assert extract_session_token(
        {"cookie": "__Secure-next-auth.session-token=session-cookie; x=y"}
    ) == "session-cookie"


def test_momo_route_and_ui_defaults() -> None:
    config = _config_from_payload({"access_token": "token", "proxy_pool": ["http://proxy"], "payment_method": "momo", "country": "US"})
    assert (config.country, config.payment_method, config.update_proxy) == ("VN", "momo", "http://proxy")
    app = create_app({"TESTING": True})
    data = app.test_client().get("/api/defaults", headers={"X-Workbench-Password": "test-password"}).get_json()
    methods = {item["value"]: item for item in data["payment_methods"]}
    assert methods["momo"]["label"] == "MoMo"
    assert data["payment_method_countries"]["momo"] == "VN"


def test_momo_route_accepts_explicit_fingerprint() -> None:
    config = _config_from_payload(
        {
            "access_token": "token",
            "proxy_pool": ["http://proxy"],
            "payment_method": "momo",
            "momo_fingerprint": "chrome146",
        }
    )
    assert config.momo_fingerprint == "chrome146"


def test_momo_url_and_gateway_session_contract() -> None:
    url = "https://payment.momo.vn/v2/gateway/pay?t=" + base64.urlsafe_b64encode(b"MOMO_SESSION|opaque").decode().rstrip("=") + "&s=signature"
    assert validate_momo_url(url)
    assert not validate_momo_url(url.replace("payment.momo.vn", "example.com"))
    assert _gateway_session_id(url) == "MOMO_SESSION"

    class Session:
        def request(self, method, endpoint, **kwargs):
            if method == "GET":
                assert endpoint.startswith("https://payment.momo.vn/v2/gateway/pay")
                return SimpleNamespace(status_code=200, headers={})
            assert method == "POST"
            assert endpoint.endswith("/querySession")
            assert kwargs["json"] == {"sessionId": "MOMO_SESSION"}
            assert kwargs["headers"]["Origin"] == "https://payment.momo.vn"
            return SimpleNamespace(status_code=200, json=lambda: {"sessionId": "MOMO_SESSION", "status_code": 1000, "redirect": False})

    assert query_gateway(Session(), url)["status_code"] == 1000


def test_momo_gateway_uses_bodyless_browser_poll_when_marked() -> None:
    calls = []

    class Session:
        momo_query_session_bodyless = True
        cookies = {}

        def request(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            if method == "GET":
                return SimpleNamespace(status_code=200, headers={}, text="")
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"sessionId": "MOMO_SESSION", "status_code": 9000, "redirect": True},
            )

    url = "https://payment.momo.vn/v2/gateway/pay?t=" + base64.urlsafe_b64encode(b"MOMO_SESSION|opaque").decode().rstrip("=") + "&s=signature"
    result = query_gateway(Session(), url)
    assert result["redirect"] is True
    assert "json" not in calls[1][2]


def test_momo_gateway_headers_split_navigation_and_xhr_contract() -> None:
    page = momo_gateway_page_headers(
        SimpleNamespace(),
        "https://payment.momo.vn/v2/gateway/pay?t=opaque&s=sig",
    )
    assert page["Accept"].startswith("text/html,")
    assert page["Sec-Fetch-Site"] == "cross-site"
    assert page["Sec-Fetch-Mode"] == "navigate"
    assert page["Sec-Fetch-Dest"] == "document"
    assert page["Upgrade-Insecure-Requests"] == "1"
    assert "Origin" not in page
    assert "Content-Type" not in page

    xhr = momo_gateway_headers(
        SimpleNamespace(),
        "https://payment.momo.vn/v2/gateway/pay?t=opaque&s=sig",
        bodyless=True,
    )
    assert xhr["Accept"] == "*/*"
    assert "Content-Type" not in xhr
    assert xhr["Origin"] == "https://payment.momo.vn"


def test_momo_gateway_core_requires_terminal_redirect(monkeypatch) -> None:
    calls = []

    class Session:
        momo_query_session_bodyless = True
        cookies = {}

        def request(self, method, endpoint, **kwargs):
            calls.append((method, endpoint, kwargs))
            if method == "GET":
                return SimpleNamespace(status_code=200, headers={}, text="")
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"sessionId": "fixture", "status_code": 1000, "redirect": False},
            )

    url = (
        "https://payment.momo.vn/v2/gateway/pay?t="
        + base64.urlsafe_b64encode(b"MOMO_SESSION|opaque").decode().rstrip("=")
        + "&s=signature"
    )
    monkeypatch.setattr("payment_link_extractor.momo_core.time.sleep", lambda _: None)
    try:
        query_gateway(
            Session(),
            url,
            polls=2,
            poll_interval=0,
            require_redirect=True,
        )
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 504
        assert "redirect" in str(exc)
    else:
        raise AssertionError("non-terminal gateway state was accepted")
    assert len(calls) == 3  # one page GET + two bodyless polls


def test_momo_gateway_core_accepts_terminal_redirect_with_interval(monkeypatch) -> None:
    sleeps = []

    class Session:
        momo_query_session_bodyless = True
        cookies = {}
        poll = 0

        def request(self, method, endpoint, **kwargs):
            if method == "GET":
                return SimpleNamespace(status_code=200, headers={}, text="")
            self.poll += 1
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "sessionId": "fixture",
                    "status_code": 9000 if self.poll == 2 else 1000,
                    "redirect": self.poll == 2,
                },
            )

    monkeypatch.setattr(
        "payment_link_extractor.momo_core.time.sleep", lambda value: sleeps.append(value)
    )
    url = (
        "https://payment.momo.vn/v2/gateway/pay?t="
        + base64.urlsafe_b64encode(b"MOMO_SESSION|opaque").decode().rstrip("=")
        + "&s=signature"
    )
    result = query_gateway(
        Session(), url, polls=2, poll_interval=4.25, require_redirect=True
    )
    assert result["status_code"] == 9000
    assert result["redirect"] is True
    assert sleeps == [4.25]


def test_momo_transport_and_result_field_are_isolated() -> None:
    source = (PAYMENT_CHANNELS["momo"].adapter_module, PAYMENT_CHANNELS["momo"].result_field)
    assert source == ("payment_link_extractor.momo_channel", "momo_url")
    assert billing_for_country("VN").country == "VN"


def test_momo_transport_normalizes_host_port_user_password() -> None:
    session = SimpleNamespace(proxies={})
    _set_proxy(session, "proxy.example:3000:user:p@ss")
    assert session.proxies["https"] == "http://user:p%40ss@proxy.example:3000"


def test_momo_transport_uses_socks5h_for_1024proxy_vn_exports() -> None:
    raw = "hk.1024proxy.io:3000:user:p@ss"
    expected = "socks5h://user:p%40ss@hk.1024proxy.io:3000"
    assert normalize_momo_proxy(raw) == expected
    session = SimpleNamespace(proxies={})
    _set_proxy(session, raw)
    assert session.proxies["http"] == expected
    assert session.proxies["https"] == expected


def test_momo_zero_amount_gate_matches_gopay_behavior() -> None:
    validate_momo_amount(0)
    for value in (None, 1, -1):
        try:
            validate_momo_amount(value)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
        else:
            raise AssertionError("non-zero or missing Momo amount was accepted")


def test_vnd_uses_zero_decimal_minor_units() -> None:
    assert currency_minor_scale("VND") == 0


def test_momo_fingerprint_profiles_are_switchable_per_attempt() -> None:
    assert {item["name"] for item in MOMO_BROWSER_PROFILES} >= {"chrome136", "chrome145", "chrome146", "chrome150", "chrome152"}
    assert MomoTransportFactory("chrome145").profile["user_agent"].find("Chrome/145.") >= 0
    assert MomoTransportFactory("chrome150").profile["user_agent"].find("Chrome/150.") >= 0
    assert MomoTransportFactory("chrome146").profile["user_agent"].find("Chrome/146.") >= 0
    assert MomoTransportFactory("chrome152").profile["user_agent"].find("Chrome/150.") >= 0
    assert MomoTransportFactory().profile["name"] in {
        "chrome136",
        "chrome145",
        "chrome146",
        "chrome150",
    }


def test_momo_checkout_carries_trial_campaign_in_initial_request() -> None:
    calls = []

    class Session:
        headers = {}

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"checkout_session_id": "oaics_fixture"},
            )

    checkout = create_checkout(Session())
    body = calls[0][2]["json"]
    assert body["promo_campaign"] == {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False,
    }
    assert calls[0][2]["headers"]["Referer"].endswith("?promo_campaign=plus-1-month-free")
    assert checkout["cs_id"] == "oaics_fixture"


def test_momo_retry_budget_reuses_one_at_with_fingerprint_rotation() -> None:
    config = ExtractionConfig("token", "http://proxy", "http://proxy", country="VN", payment_method="momo", retry_count=2, proxy_pool=("http://proxy",))
    assert TaskManager._total_attempts(config) == 3


def test_momo_chatgpt_headers_follow_har_contract(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_MOMO_SENTINEL_BROWSER", "off")
    config = ExtractionConfig("opaque.token", "http://proxy", "", country="VN", payment_method="momo")
    session = MomoTransportFactory("chrome150").chatgpt(config, "http://proxy")
    try:
        keys = {str(key).lower() for key in session.headers}
        assert {"oai-device-id", "oai-session-id", "oai-client-build-number", "oai-client-version"} <= keys
        assert "chatgpt-account-id" not in keys
        headers = momo_request_headers(
            session,
            "POST",
            "https://chatgpt.com/backend-api/payments/checkout",
            {"Referer": "https://chatgpt.com/"},
            flow="chatgpt_checkout",
        )
        telemetry = json.loads(headers["oai-telemetry"])
        assert telemetry[0] == 1
        assert telemetry[2:7] == [8, 96, 48, 2, 0]
        assert headers["Referer"] == "https://chatgpt.com/"
        assert session.headers["Priority"] == "u=1, i"
    finally:
        from payment_link_extractor.momo_transport import close

        close(session)


def test_momo_pending_updates_follow_runtime_receipts_not_update_payloads() -> None:
    session = SimpleNamespace(headers={"x-oai-is-pending-updates": MOMO_EMPTY_PENDING_UPDATES})
    first = SimpleNamespace(
        headers={
            "x-oai-is-receipt": "receipt-a",
            "x-oai-is-update": "update-a",
        }
    )
    record_momo_pending_updates(session, first)
    assert json.loads(session.headers["x-oai-is-pending-updates"]) == {
        "v": 3,
        "updates": ["receipt-a"],
    }
    second = SimpleNamespace(
        headers={
            "x-oai-is-receipt": "receipt-b",
            "x-oai-is-pending-updates-ack": "ack",
        }
    )
    record_momo_pending_updates(session, second)
    assert json.loads(current_momo_pending_updates_header(session)) == {
        "v": 3,
        "updates": ["receipt-a", "receipt-b"],
    }
    assert "update-a" not in session.headers["x-oai-is-pending-updates"]


def test_momo_request_echoes_pending_receipt_on_next_chatgpt_request() -> None:
    calls = []

    class Response:
        status_code = 200
        headers = {"x-oai-is-receipt": "receipt-next"}

        def json(self):
            return {"checkout_session_id": "oaics_fixture"}

    class Session:
        def __init__(self):
            self.headers = {"x-oai-is-pending-updates": MOMO_EMPTY_PENDING_UPDATES}

        def request(self, method, url, **kwargs):
            calls.append(kwargs)
            return Response()

    from payment_link_extractor.momo_checkout import request as momo_request

    session = Session()
    momo_request(
        session,
        "GET",
        "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27",
        "fixture",
    )
    momo_request(
        session,
        "GET",
        "https://chatgpt.com/backend-api/promo_campaign/check_coupon",
        "fixture",
    )
    assert json.loads(calls[1]["headers"]["x-oai-is-pending-updates"]) == {
        "v": 3,
        "updates": ["receipt-next"],
    }


def test_momo_payment_phase_uses_empty_pending_envelope_by_default(monkeypatch) -> None:
    monkeypatch.delenv("OPLL_MOMO_ECHO_PAYMENT_PENDING_UPDATES", raising=False)
    session = SimpleNamespace(
        headers={"x-oai-is-pending-updates": '{"v":3,"updates":["receipt-old"]}'},
        momo_pending_updates=["receipt-old"],
    )
    headers = momo_request_headers(
        session,
        "POST",
        "https://chatgpt.com/backend-api/payments/checkout/confirm",
    )
    assert headers["x-oai-is-pending-updates"] == MOMO_EMPTY_PENDING_UPDATES
    assert session.momo_last_request_pending_updates == 0


def test_momo_checkout_boundary_consumes_pending_receipt_batch() -> None:
    session = SimpleNamespace(
        headers={
            "x-oai-is-pending-updates": '{"v":3,"updates":["receipt-a"]}'
        },
        momo_pending_updates=["receipt-a"],
    )
    assert clear_momo_pending_updates(session) == MOMO_EMPTY_PENDING_UPDATES
    assert session.momo_pending_updates == []


def test_momo_blocked_confirm_is_context_rejection_not_session_token_failure() -> None:
    class Response:
        status_code = 409
        headers = {"x-oai-is-receipt": "receipt-blocked"}

        def json(self):
            return {"status": "blocked", "type": "manual_approval"}

    class Session:
        headers = {
            "x-oai-is-pending-updates": MOMO_EMPTY_PENDING_UPDATES,
        }
        openai_sentinel_provider = SimpleNamespace(
            headers=lambda _flow, *, referer="": {
                "OpenAI-Sentinel-Token": "proof-fixture"
            }
        )
        refresh_momo_request_headers = staticmethod(lambda _method, _url: {
            "oai-telemetry": "[1,100,8,96,48,2,0,105]"
        })

        def request(self, *_args, **_kwargs):
            return Response()

    with pytest.raises(MomoConfirmBlockedError) as raised:
        checkout_confirm(
            Session(),
            {"cs_id": "oaics_fixture", "processor_entity": "openai_llc"},
            "ctoken_fixture",
        )
    assert raised.value.status_code == 409
    assert raised.value.failure_mode == "approval_context_rejected"
    assert raised.value.retryable is True
    assert "nextauth" not in str(raised.value).lower()
    assert "pending_updates=" in str(raised.value)
    assert "oai_telemetry=present" in str(raised.value)


def test_momo_hydration_captures_runtime_account_cookie_without_api_headers() -> None:
    class Jar:
        def __init__(self):
            self.values = {}

        def set(self, name, value, **_kwargs):
            self.values[name] = value

        def get(self, name, default=""):
            return self.values.get(name, default)

    class Response:
        status_code = 200
        text = "[{}]"
        headers = {
            "Set-Cookie": "_account=account-fixture; Max-Age=7776000; Path=/"
        }
        cookies = []

    class Session:
        def __init__(self):
            self.cookies = Jar()
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()

    session = Session()
    checkout = {"cs_id": "oaics_fixture", "processor_entity": "openai_llc"}
    result = hydrate_checkout_route(session, checkout)
    assert result["status"] == 200
    assert "checkout/openai_llc/oaics_fixture.data" in session.calls[0][1]
    assert "routes%2Fcheckout.%24entity.%24checkoutId" in session.calls[0][1]
    assert session.calls[0][2]["headers"]["Authorization"] is None
    assert session.calls[0][2]["headers"]["Content-Type"] is None
    assert session.calls[0][2]["headers"]["Origin"] is None
    assert session.cookies.get("_account") == "account-fixture"
    assert checkout["momo_checkout_hydration_format"] == "devalue_array"


def test_momo_hydration_cookie_fallback_handles_multiline_set_cookie() -> None:
    class Jar:
        def __init__(self):
            self.values = {}

        def set(self, name, value, **_kwargs):
            self.values[name] = value

        def get(self, name, default=""):
            return self.values.get(name, default)

    session = SimpleNamespace(
        cookies=Jar(),
    )
    response = SimpleNamespace(
        cookies=[],
        headers={
            "set-cookie": (
                "_account=account-fixture; Path=/\n"
                "oai-client-session-epoch=epoch-fixture; Path=/\n"
                "__Secure-next-auth.session-token.0=ignored; Path=/"
            )
        },
    )
    from payment_link_extractor.momo_transport import sync_momo_response_cookies

    assert sync_momo_response_cookies(session, response) == [
        "_account",
        "oai-client-session-epoch",
    ]
    assert session.cookies.get("_account") == "account-fixture"
    assert session.cookies.get("oai-client-session-epoch") == "epoch-fixture"


def test_momo_hydration_marks_success_status_login_redirect_as_not_ok() -> None:
    class Response:
        status_code = 202
        headers = {}
        cookies = []
        text = '<meta http-equiv="refresh" content="0;url=/auth/login">'

    class Session:
        headers = {"Authorization": "Bearer runtime-fixture"}
        openai_device_id = "device-fixture"
        openai_session_id = "session-fixture"
        openai_account_id = "account-fixture"

        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return Response()

    checkout = {"cs_id": "oaics_fixture", "processor_entity": "openai_llc"}
    session = Session()
    result = hydrate_checkout_route(session, checkout)
    assert result["status"] == 202
    assert checkout["momo_checkout_hydration"]["redirect_to_login"] is True
    assert checkout["momo_checkout_hydration"]["ok"] is False
    assert checkout["momo_checkout_hydration"]["attempts"] == 2
    assert len(session.calls) == 2


def test_momo_at_only_jar_drops_browser_nextauth_chunks() -> None:
    import requests

    jar = requests.cookies.RequestsCookieJar()
    jar.set("__Secure-next-auth.session-token.0", "chunk", domain=".chatgpt.com", path="/")
    jar.set("__Host-next-auth.csrf-token", "csrf", domain=".chatgpt.com", path="/")
    jar.set("_account", "account-fixture", domain=".chatgpt.com", path="/")
    session = SimpleNamespace(
        cookies=jar,
        headers={
            "Cookie": "__Secure-next-auth.session-token.0=chunk; _account=account-fixture"
        },
        momo_cookie_jar_mode=True,
        momo_context_cookie_names=["__Secure-next-auth.session-token.0", "_account"],
    )
    removed = purge_momo_browser_auth_cookies(session)
    assert set(removed) >= {
        "__Secure-next-auth.session-token.0",
        "__Host-next-auth.csrf-token",
    }
    assert not any("next-auth" in str(cookie.name).lower() for cookie in jar)
    assert "next-auth" not in session.headers.get("Cookie", "").lower()
    assert session.cookies.get("_account") == "account-fixture"


def test_momo_browser_cookie_allowlist_is_applied_before_http_jar_sync() -> None:
    import requests
    from payment_link_extractor.transport import BrowserSentinelProvider

    jar = requests.cookies.RequestsCookieJar()
    session = SimpleNamespace(
        cookies=jar,
        headers={},
        momo_cookie_jar_mode=True,
        momo_cookie_allowlist={"oai-did", "_account"},
    )
    provider = object.__new__(BrowserSentinelProvider)
    provider.transport_session = session
    provider.enhanced = True
    provider.device_id = "device-fixture"
    provider._run = lambda *_args, **_kwargs: {
        "data": {
            "cookies": [
                {"name": "oai-did", "value": "device-fixture", "domain": ".chatgpt.com", "path": "/"},
                {"name": "_account", "value": "account-fixture", "domain": ".chatgpt.com", "path": "/"},
                {"name": "__Secure-next-auth.session-token.0", "value": "secret-fixture", "domain": ".chatgpt.com", "path": "/"},
            ]
        }
    }
    provider._sync_cookies()
    assert session.cookies.get("oai-did") == "device-fixture"
    assert session.cookies.get("_account") == "account-fixture"
    assert session.cookies.get("__Secure-next-auth.session-token.0") is None
    assert "next-auth" not in str(getattr(provider, "_cookies", "")).lower()


def test_momo_account_routing_cookie_is_derived_from_at_account_id() -> None:
    class Jar:
        def __init__(self):
            self.values = {}

        def set(self, name, value, **_kwargs):
            self.values[name] = value

    session = SimpleNamespace(cookies=Jar())
    assert seed_momo_account_cookie(session, "account-fixture") is True
    assert session.cookies.values["_account"] == "account-fixture"
    assert session.momo_account_cookie_present is True


def test_momo_customer_balance_bootstrap_uses_at_account_and_receipt_state() -> None:
    calls = []

    class Response:
        status_code = 200
        headers = {"x-oai-is-receipt": "receipt-balance"}
        text = '{"balance":0,"currency":"VND"}'

        def json(self):
            return {"balance": 0, "currency": "VND"}

    class Session:
        openai_account_id = "account-fixture"
        headers = {"x-oai-is-pending-updates": MOMO_EMPTY_PENDING_UPDATES}

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

    checkout = {"cs_id": "oaics_fixture", "processor_entity": "openai_llc"}
    payload = refresh_momo_customer_balance(Session(), checkout)
    assert payload["currency"] == "VND"
    assert calls[0][1].endswith("/backend-api/accounts/account-fixture/customer-balance")
    assert calls[0][2]["headers"]["chatgpt-account-id"] == "account-fixture"
    assert checkout["momo_customer_balance"]["ok"] is True


def test_momo_elements_and_link_lookup_share_one_stripe_session_id() -> None:
    calls = []

    class Response:
        status_code = 200
        headers = {}

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Session:
        headers = {
            "Authorization": "Bearer at-fixture",
            "oai-device-id": "device-fixture",
            "oai-session-id": "session-fixture",
            "oai-client-build-number": "build-fixture",
            "oai-client-version": "version-fixture",
        }

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if "elements/sessions" in url:
                return Response(
                    {
                        "session_id": "elements_fixture",
                        "config_id": "config_fixture",
                        "customer": {
                            "customer_session": {"customer": "cus_fixture"}
                        },
                        "passive_captcha": {"site_key": "site-fixture"},
                    }
                )
            if "link/get-cookie" in url:
                return Response({"auth_session_client_secret": "secret-fixture"})
            return Response({"consumer_session": None})

    checkout = {
        "publishable_key": "pk_fixture",
        "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
    }
    elements_session(Session(), checkout)
    first_id = checkout["stripe_js_id"]
    result = prepare_momo_link_context(
        Session(), checkout, "user@example.test"
    )
    assert result["lookup_successes"] == 3
    assert checkout["stripe_client_session_id"] == first_id
    lookup_calls = [
        item for item in calls if "consumers/sessions/lookup" in item[1]
    ]
    assert len(lookup_calls) == 3
    for _, _, kwargs in lookup_calls:
        assert kwargs["data"]["session_id"] == first_id
        assert kwargs["data"]["key"] == "pk_fixture"
        assert kwargs["headers"]["Accept-Language"] == "en"
    assert lookup_calls[1][2]["data"]["customer_id"] == "cus_fixture"
    assert "link_global_holdback_data[assignment]" not in lookup_calls[2][2]["data"]
    assert lookup_calls[2][2]["data"]["do_not_log_consumer_funnel_event"] == "true"


def test_momo_provider_does_not_overlay_http_jar_cookie_snapshot() -> None:
    class Cookie:
        name = "_account"
        value = "account-fixture"
        domain = ".chatgpt.com"

        @staticmethod
        def has_nonstandard_attr(_name):
            return False

    class Jar:
        def __iter__(self):
            return iter([Cookie()])

    class Session:
        momo_cookie_jar_mode = True

        def __init__(self):
            self.cookies = Jar()
            self.headers = {}

    class Delegate:
        def __init__(self):
            self.seeded = []
            self._attestation = "attestation-fixture"

        def set_cookie(self, name, value, *, http_only=False):
            self.seeded.append((name, value, http_only))

        def headers(self, _flow, *, referer=""):
            return {"Cookie": "oai-did=stale", "OpenAI-Sentinel-Token": "proof"}

        def _run(self, *_args, **_kwargs):
            return {"data": {"requests": []}}

    from payment_link_extractor.momo_transport import MomoSentinelProvider

    provider = object.__new__(MomoSentinelProvider)
    provider._transport_session = Session()
    provider._delegate = Delegate()
    provider._seen_browser_receipts = set()
    result = provider.headers("checkout_session_approval", referer="https://chatgpt.com/")
    assert "Cookie" not in result
    assert provider._delegate.seeded == [("_account", "account-fixture", False)]
    assert (
        provider._transport_session.headers["oai-web-deployment-attestation"]
        == "attestation-fixture"
    )


def test_momo_at_backend_bridge_targets_backend_but_excludes_sentinel(tmp_path) -> None:
    script_path = tmp_path / "sentinel-init.js"
    script_path.write_text("/* sdk */", encoding="utf-8")

    class Delegate:
        sentinel_init_script = script_path

    from payment_link_extractor.momo_transport import MomoSentinelProvider

    provider = object.__new__(MomoSentinelProvider)
    provider._delegate = Delegate()
    provider._backend_auth_config = {
        "token": "at-fixture",
        "account": "account-fixture",
        "device": "device-fixture",
        "session": "session-fixture",
        "language": "vi-VN",
        "build": "10109010",
        "version": "version-fixture",
        "observation": "",
    }
    assert provider._install_backend_auth_bridge()
    script = script_path.read_text(encoding="utf-8")
    assert "__opllMomoAuthFetchWrapped" in script
    assert "!u.pathname.startsWith('/backend-api/sentinel/')" in script
    assert "Bearer ' + config.token" in script
    assert "config.attestation" in script
    assert "x-oai-is-pending-updates" in script
    assert "at-fixture" not in script


def test_momo_browser_receipts_wait_for_checkout_context() -> None:
    from payment_link_extractor.momo_transport import MomoSentinelProvider

    class Delegate:
        def __init__(self):
            self.calls = 0

        def _run(self, *_args, **_kwargs):
            self.calls += 1
            values = ["receipt-old"]
            if self.calls > 1:
                values.append("receipt-new")
            return {
                "data": {
                    "requests": [
                        {"responseHeaders": {"x-oai-is-receipt": value}}
                        for value in values
                    ]
                }
            }

    session = SimpleNamespace(
        headers={"x-oai-is-pending-updates": MOMO_EMPTY_PENDING_UPDATES}
    )
    provider = object.__new__(MomoSentinelProvider)
    provider._delegate = Delegate()
    provider._transport_session = session
    provider._seen_browser_receipts = set()
    provider._last_browser_receipts_sync = 0.0
    provider._browser_receipts_enabled = False

    provider._sync_browser_receipts()
    assert session.headers["x-oai-is-pending-updates"] == MOMO_EMPTY_PENDING_UPDATES
    provider.enable_browser_receipts()
    assert provider._browser_receipts_enabled is True
    provider._last_browser_receipts_sync = 0.0
    provider._sync_browser_receipts()
    assert json.loads(session.headers["x-oai-is-pending-updates"])["updates"] == [
        "receipt-new"
    ]


def test_momo_at_backend_bridge_activates_runtime_config_without_file_secret() -> None:
    class Delegate:
        def __init__(self):
            self.sentinel_init_script = None
            self.calls = []

        def _eval(self, expression, *, timeout=10):
            self.calls.append((expression, timeout))
            return True

    class Session:
        headers = {"x-oai-is-client-observation": "v1.r.p.runtime"}

    from payment_link_extractor.momo_transport import MomoSentinelProvider

    provider = object.__new__(MomoSentinelProvider)
    provider._delegate = Delegate()
    provider._transport_session = Session()
    provider._backend_auth_config = {
        "token": "at-runtime",
        "account": "acct",
        "device": "did",
        "session": "sid",
        "language": "vi-VN",
        "build": "10109010",
        "version": "ver",
        "observation": "",
    }
    provider._activate_backend_auth_bridge()
    expression = provider._delegate.calls[0][0]
    assert "window.__opllMomoAuthConfig" in expression
    assert "at-runtime" in expression
    assert provider._backend_auth_config["observation"] == "v1.r.p.runtime"
    assert provider._transport_session.momo_backend_auth_bridge_enabled is True


def test_momo_sentinel_node_fallback_is_momo_owned(monkeypatch) -> None:
    calls = []

    class NodeSentinel:
        @staticmethod
        def mint_sentinel_sync(**kwargs):
            calls.append(kwargs)
            return "node-proof", "node-observer"

    class Delegate:
        user_agent = "ua-fixture"
        proxy = "proxy-fixture"
        timezone = "Asia/Saigon"

    class Session:
        cookies = []

    import sys
    from payment_link_extractor.momo_transport import MomoSentinelProvider

    monkeypatch.setitem(sys.modules, "sentinel", NodeSentinel)
    provider = object.__new__(MomoSentinelProvider)
    provider._delegate = Delegate()
    provider._transport_session = Session()
    provider._backend_auth_config = {
        "device": "device-fixture",
        "language": "vi-VN",
        "user_agent": "ua-fixture",
    }
    provider._node_fallback_enabled = True
    provider._node_fallback_used = False
    provider._browser_error = "RuntimeError"
    result = provider._node_sentinel_headers(
        "checkout_session_approval", "https://chatgpt.com/checkout/fixture"
    )
    assert result == {
        "OpenAI-Sentinel-Token": "node-proof",
        "OpenAI-Sentinel-SO-Token": "node-observer",
    }
    assert calls[0]["flow"] == "checkout_session_approval"
    assert calls[0]["device_id"] == "device-fixture"
    assert provider._transport_session.momo_sentinel_provider_mode == "node_fallback"


def test_momo_stripe_session_uses_stripe_js_origin_and_locale() -> None:
    config = ExtractionConfig("opaque.token", "http://proxy", "", country="VN", payment_method="momo")
    session = MomoTransportFactory("chrome150").stripe(config)
    try:
        assert session.headers["Origin"] == "https://js.stripe.com"
        assert session.headers["Referer"] == "https://js.stripe.com/"
        assert "Accept-Language" in session.headers
    finally:
        from payment_link_extractor.momo_transport import close

        close(session)


def test_momo_enhanced_sentinel_navigation_puts_init_scripts_after_url() -> None:
    config = ExtractionConfig("opaque.token", "http://proxy", "", country="VN", payment_method="momo")
    session = MomoTransportFactory("chrome150").chatgpt(config, "http://proxy")
    try:
        provider = getattr(session, "openai_sentinel_provider", None)
        if provider is None:
            return
        delegate = getattr(provider, "_delegate", provider)
        args = delegate._enhanced_open_args("https://chatgpt.com/")
        assert args[:2] == ["open", "https://chatgpt.com/"]
        assert args.index("--init-script") > args.index("https://chatgpt.com/")
    finally:
        from payment_link_extractor.momo_transport import close

        close(session)


def test_momo_browser_uses_native_chrome_ua_by_default(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_MOMO_NATIVE_BROWSER_UA", "1")
    config = ExtractionConfig("opaque.token", "http://proxy", "", country="VN", payment_method="momo")
    session = MomoTransportFactory("chrome152").chatgpt(config, "http://proxy")
    try:
        provider = getattr(session, "openai_sentinel_provider", None)
        if provider is None:
            return
        delegate = getattr(provider, "_delegate", provider)
        assert getattr(delegate, "native_browser_ua", False) is True
        command = delegate._base_command()
        assert "--user-agent" not in command
    finally:
        from payment_link_extractor.momo_transport import close

        close(session)


def test_momo_request_headers_refreshes_sentinel_per_flow() -> None:
    calls = []

    class Provider:
        def headers(self, flow, *, referer=""):
            calls.append((flow, referer))
            return {"OpenAI-Sentinel-Token": "runtime-proof"}

    session = SimpleNamespace(
        openai_sentinel_provider=Provider(),
        refresh_momo_request_headers=lambda method, url: {"x-oai-is-client-observation": "v1.r.p.fixture"},
    )
    headers = momo_request_headers(
        session,
        "POST",
        "https://chatgpt.com/backend-api/payments/checkout",
        flow="chatgpt_checkout",
        referer="https://chatgpt.com/",
    )
    assert headers["OpenAI-Sentinel-Token"] == "runtime-proof"
    assert calls == [("chatgpt_checkout", "https://chatgpt.com/")]


def test_momo_backend_get_omits_json_entity_headers() -> None:
    session = SimpleNamespace(headers={})
    headers = momo_request_headers(
        session,
        "GET",
        "https://chatgpt.com/backend-api/promo_campaign/check_coupon",
        {
            "x-openai-target-path": "/backend-api/promo_campaign/check_coupon"
        },
    )
    assert headers["Accept"] == "*/*"
    assert headers["Origin"] is None
    assert headers["Content-Type"] is None
    assert headers["x-openai-target-route"] == "/backend-api/promo_campaign/check_coupon"


def test_momo_target_route_uses_live_templates() -> None:
    assert momo_target_route("/backend-api/accounts/check/v4-2023-04-27") == "/backend-api/accounts/check/{version}"
    assert momo_target_route("/backend-api/accounts/account-fixture/customer-balance") == "/backend-api/accounts/{account_id}/customer-balance"
    assert momo_target_route("/backend-api/checkout_pricing_config/configs/VN") == "/backend-api/checkout_pricing_config/configs/{country_code}"
    assert momo_target_route("/backend-api/payments/checkout/openai_llc/oaics_fixture") == "/backend-api/payments/checkout/{processor_entity}/{checkout_session_id}"
    assert momo_target_route("/backend-api/payments/checkout/confirm") == "/backend-api/payments/checkout/confirm"
    assert momo_target_route("/backend-api/checkout_pricing_config/configs/VN") == "/backend-api/checkout_pricing_config/configs/{country_code}"


def test_momo_gateway_headers_use_runtime_csrf_only(monkeypatch) -> None:
    monkeypatch.delenv("OPLL_MOMO_CSRF_TOKEN", raising=False)
    session = SimpleNamespace(momo_csrf_token="csrf-runtime")
    headers = momo_gateway_headers(
        session, "https://payment.momo.vn/v2/gateway/pay?t=opaque&s=sig"
    )
    assert headers["X-CSRF-Token"] == "csrf-runtime"
    assert headers["Origin"] == "https://payment.momo.vn"


def test_momo_gateway_csrf_can_be_captured_from_live_page() -> None:
    session = SimpleNamespace(cookies={})
    response = SimpleNamespace(
        headers={},
        text='<meta name="csrf-token" content="csrf-from-page">',
    )
    assert capture_momo_csrf_token(session, response) == "csrf-from-page"
    assert session.momo_csrf_token == "csrf-from-page"


def test_momo_gateway_csrf_supports_spring_meta_name() -> None:
    session = SimpleNamespace(cookies={})
    response = SimpleNamespace(
        headers={},
        text='<meta name="_csrf" content="csrf-spring-fixture"><meta name="_csrf_header" content="X-CSRF-TOKEN">',
    )
    assert capture_momo_csrf_token(session, response) == "csrf-spring-fixture"


def test_momo_trial_eligibility_rotates_vn_proxies_before_checkout() -> None:
    class Response:
        status_code = 200

        def __init__(self, state: str):
            self.state = state

        def json(self):
            return {
                "accounts": {
                    "default": {
                        "eligible_promo_campaigns": {
                            "plus": (
                                {"campaign_id": "plus-1-month-free"}
                                if self.state == "eligible"
                                else {}
                            )
                        }
                    }
                }
            }

    class Session:
        def __init__(self, state: str):
            self.state = state
            self.headers = {}

        def request(self, method, url, **kwargs):
            assert method == "GET"
            assert (
                "/accounts/check/v4-2023-04-27" in url
                or "/backend-anon/accounts/check/v4-2023-04-27" in url
                or "/backend-anon/me" in url
                or "/backend-anon/checkout_pricing_config/configs/VN" in url
                or "/checkout_pricing_config/configs/VN" in url
                or "/subscriptions/has_app_store_subscription_in_billing_retry" in url
                or "/settings/user" in url
                or "/payments/payment_methods" in url
            )
            return Response(self.state)

        def close(self):
            pass

    class Factory:
        def __init__(self):
            self.proxies = []

        def chatgpt(self, config, proxy):
            self.proxies.append(proxy)
            return Session("not_eligible" if len(self.proxies) == 1 else "eligible")

    config = ExtractionConfig(
        "token",
        "proxy-1",
        "proxy-1",
        country="VN",
        payment_method="momo",
        proxy_pool=("proxy-1", "proxy-2"),
    )
    events = []
    result = probe_momo_trial_eligibility(
        config, transport_factory=Factory(), stage_callback=events.append
    )
    assert result["eligible"] is True
    assert result["proxy"] == "proxy-2"
    assert events[0] == "eligibility_proxy:1"
    assert events[-1] == "eligibility_confirmed"
    assert "preflight:1" in events
    assert "promo_request:2" in events
    assert result["anon_preflight_http_statuses"]


def test_momo_anonymous_shell_preflight_suppresses_at_headers(monkeypatch) -> None:
    from payment_link_extractor.momo_eligibility import _momo_anonymous_shell_preflight

    calls = []

    class Response:
        status_code = 200
        headers = {}

        def json(self):
            return {}

    class Session:
        headers = {
            "Authorization": "Bearer at-fixture",
            "oai-device-id": "device-fixture",
            "oai-session-id": "session-fixture",
            "oai-client-build-number": "build-fixture",
            "oai-client-version": "version-fixture",
        }

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

    monkeypatch.setenv("OPLL_MOMO_ANON_PREFLIGHT", "true")
    result = _momo_anonymous_shell_preflight(Session())
    assert len(result) == 3
    assert len(calls) == 3
    for _, url, kwargs in calls:
        assert "/backend-anon/" in url
        headers = kwargs["headers"]
        assert headers["Authorization"] is None
        assert "oai-device-id" not in headers
        assert headers["x-oai-is-pending-updates"] == MOMO_EMPTY_PENDING_UPDATES
        assert headers["x-openai-target-route"].startswith("/backend-anon/")


def test_momo_trial_eligibility_can_retain_the_authenticated_session() -> None:
    class Response:
        status_code = 200

        def __init__(self, coupon: bool = False):
            self.coupon = coupon

        def json(self):
            if self.coupon:
                return {"state": "eligible"}
            return {
                "accounts": {
                    "default": {
                        "eligible_promo_campaigns": {
                            "plus": {"id": "plus-1-month-free"}
                        }
                    }
                }
            }

    class Session:
        def __init__(self):
            self.closed = False

        def request(self, method, url, **kwargs):
            return Response("check_coupon" in url)

        def close(self):
            self.closed = True

    class Factory:
        def __init__(self):
            self.session = Session()

        def chatgpt(self, config, proxy):
            return self.session

    factory = Factory()
    config = ExtractionConfig(
        "token",
        "proxy",
        "proxy",
        country="VN",
        payment_method="momo",
        proxy_pool=("proxy",),
    )
    result = probe_momo_trial_eligibility(
        config, transport_factory=factory, retain_session=True
    )
    assert result["_chatgpt_session"] is factory.session
    assert factory.session.closed is False
    factory.session.close()


def test_momo_tax_refresh_matches_progressive_har_address_shape() -> None:
    calls = []

    class Session:
        def request(self, method, url, **kwargs):
            calls.append(kwargs["json"])
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}}},
            )

    checkout = {"cs_id": "oaics_fixture", "processor_entity": "openai_llc"}
    billing = billing_for_country("VN").to_dict()
    for iteration in range(1, 4):
        taxes(Session(), checkout, billing, tax_iteration=iteration)
    assert calls[0]["billing_address"]["state"] == ""
    assert calls[0]["billing_address"]["postal_code"] == ""
    assert calls[1]["billing_address"]["state"] == billing["state"]
    assert calls[1]["billing_address"]["postal_code"] == ""
    assert calls[2]["billing_address"]["postal_code"] == billing["postal_code"]
    assert "line2" not in calls[0]["billing_address"]
    assert "tax_id" not in calls[0]


def test_momo_stripe_ids_are_shared_with_chatgpt_only() -> None:
    class Cookies:
        def __init__(self):
            self.values = {}

        def get(self, name):
            return self.values.get(name, "")

        def set(self, name, value, **kwargs):
            self.values[name] = value

    chatgpt = SimpleNamespace(cookies=Cookies(), headers={})
    stripe = SimpleNamespace(cookies=Cookies(), headers={})
    checkout = {}
    result = synchronize_momo_stripe_browser_ids(chatgpt, stripe, checkout)
    assert len(result["__stripe_mid"]) == 42
    assert result["__stripe_mid"] == chatgpt.cookies.get("__stripe_mid")
    assert stripe.cookies.get("__stripe_mid") == ""
    assert checkout["stripe_muid"] == result["__stripe_mid"]


def test_momo_checkout_route_fallback_and_confirm_headers() -> None:
    calls = []

    class Session:
        headers = {}

        def request(self, method, url, **kwargs):
            calls.append((method, url, kwargs))
            if url.endswith("/payments/checkout"):
                return SimpleNamespace(
                    status_code=200,
                    json=lambda: {"checkout_session_id": "oaics_fixture"},
                )
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"status": "success", "client_secret": "pi_fixture_secret_x"},
            )

    session = Session()
    checkout = create_checkout(session)
    assert checkout["processor_entity"] == "openai_llc"
    checkout_confirm(session, checkout, "ctoken_fixture")
    confirm_headers = calls[-1][2]["headers"]
    assert confirm_headers["x-openai-target-path"] == "/backend-api/payments/checkout/confirm"
    assert "/checkout/openai_llc/oaics_fixture" in confirm_headers["Referer"]


def test_momo_confirm_accepts_nested_setup_intent_secret() -> None:
    class Session:
        headers = {}

        def request(self, method, url, **kwargs):
            return SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "status": "open",
                    "setup_intent": {"client_secret": "seti_fixture_secret"},
                },
            )

    checkout = {"cs_id": "oaics_fixture", "processor_entity": "openai_llc"}
    result = checkout_confirm(Session(), checkout, "ctoken_fixture")
    assert result["client_secret"] == "seti_fixture_secret"


def test_momo_custom_intent_confirm_uses_base_version_and_two_attribution_fields() -> None:
    captured = {}

    class Session:
        def request(self, method, url, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(status_code=200, json=lambda: {"status": "succeeded"})

    checkout = {
        "cs_id": "oaics_fixture",
        "publishable_key": "pk_fixture",
        "customer": "cus_fixture",
    }
    confirmed = {
        "client_secret": "seti_fixture_secret",
        "confirm_return_url": "https://chatgpt.com/checkout/verify",
    }
    intent_confirm(Session(), checkout, "ctoken_fixture", confirmed)
    data = captured["data"]
    assert data["_stripe_version"] == "2025-03-31.basil"
    assert set(data) == {
        "return_url",
        "confirmation_token",
        "key",
        "_stripe_version",
        "client_secret",
        "client_attribution_metadata[client_session_id]",
        "client_attribution_metadata[merchant_integration_source]",
    }
    assert list(data) == [
        "return_url",
        "confirmation_token",
        "key",
        "_stripe_version",
        "client_attribution_metadata[client_session_id]",
        "client_attribution_metadata[merchant_integration_source]",
        "client_secret",
    ]


def test_momo_confirmation_token_reuses_synced_ids_and_har_time(monkeypatch) -> None:
    captured = {}

    class Session:
        def request(self, method, url, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(
                status_code=200,
                json=lambda: {"id": "ctoken_fixture"},
            )

    checkout = {
        "publishable_key": "pk_fixture",
        "elements_session": {"session_id": "elements_fixture", "config_id": "config_fixture"},
        "stripe_muid": "a" * 42,
        "stripe_sid": "b" * 42,
    }
    billing = billing_for_country("VN").to_dict()
    monkeypatch.delenv("OPLL_MOMO_TIME_ON_PAGE_MS", raising=False)
    from payment_link_extractor.momo_stripe import confirmation_token

    assert confirmation_token(Session(), checkout, billing) == "ctoken_fixture"
    body = captured["data"]
    assert body["payment_method_data[muid]"] == "a" * 42
    assert body["payment_method_data[sid]"] == "b" * 42
    assert body["payment_method_data[time_on_page]"] == "20000"
    assert list(body)[0] == "payment_method_data[type]"
    assert list(body)[-3:] == [
        "set_as_default_payment_method",
        "key",
        "_stripe_version",
    ]


def test_momo_client_session_id_matches_elements_and_attribution() -> None:
    from payment_link_extractor.momo_stripe import _attribution_fields

    checkout = {"stripe_js_id": "stripe-session-fixture"}
    fields = _attribution_fields(checkout, source="elements")
    assert fields["client_session_id"] == "stripe-session-fixture"
    assert checkout["stripe_client_session_id"] == "stripe-session-fixture"
    assert checkout["stripe_js_id"] == "stripe-session-fixture"


def test_momo_redirect_follows_stripe_authorize_hop() -> None:
    target = "https://payment.momo.vn/v2/gateway/pay?t=opaque&s=signature"

    class Session:
        def request(self, method, url, **kwargs):
            assert method == "GET"
            assert url.startswith("https://pm-redirects.stripe.com/")
            return SimpleNamespace(status_code=200, url=target, headers={})

    assert resolve_momo_redirect(Session(), "https://pm-redirects.stripe.com/authorize/example") == target


def test_momo_core_reuses_eligibility_session_and_har_order(monkeypatch) -> None:
    import payment_link_extractor.momo_core as core

    events = []

    class Provider:
        def prepare_flow(self, *, flow, referer):
            events.append(("prepare_flow", flow, referer))

    class Session:
        def __init__(self):
            self.headers = {}
            self.cookies = {}
            self.openai_sentinel_provider = Provider()

    chat = Session()
    stripe = Session()

    class Factory:
        profile = {"name": "chrome150"}

        def __init__(self):
            self.chat_calls = 0

        def chatgpt(self, config, proxy):
            self.chat_calls += 1
            return chat

        def stripe(self, config):
            return stripe

        def momo(self, config):
            return Session()

    factory = Factory()
    monkeypatch.setattr(
        core,
        "probe_momo_trial_eligibility",
        lambda *args, **kwargs: {
            "proxy": "proxy",
            "_chatgpt_session": chat,
            "eligible": True,
        },
    )

    def fake_checkout(*args, **kwargs):
        events.append("checkout")
        return {
            "cs_id": "oaics_fixture",
            "processor_entity": "openai_llc",
            "publishable_key": "pk_fixture",
            "checkout_state": {"total": {"total": {"minorUnitsAmount": 0}}},
        }

    def fake_elements(*args, **kwargs):
        events.append("elements")
        return {"session_id": "elements_fixture", "config_id": "config_fixture"}

    def fake_taxes(*args, **kwargs):
        events.append(("taxes", kwargs.get("tax_iteration")))
        return {}

    monkeypatch.setattr(core, "create_checkout", fake_checkout)
    monkeypatch.setattr(core, "elements_session", fake_elements)
    monkeypatch.setattr(core, "taxes", fake_taxes)
    monkeypatch.setattr(core, "synchronize_momo_stripe_browser_ids", lambda *a: None)
    monkeypatch.setattr(core, "confirmation_token", lambda *a, **k: events.append("token") or "ctoken_fixture")
    monkeypatch.setattr(core, "checkout_confirm", lambda *a, **k: events.append("checkout_confirm") or {"status": "success", "client_secret": "seti_fixture_secret"})
    monkeypatch.setattr(core, "intent_confirm", lambda *a, **k: events.append("intent_confirm") or {"url": "https://payment.momo.vn/v2/gateway/pay?t=opaque&s=sig"})
    monkeypatch.setattr(core, "query_gateway", lambda *a, **k: events.append("gateway") or {"status_code": 9000, "redirect": True, "_poll_count": 1})
    monkeypatch.setattr(core, "close", lambda *_: None)

    config = ExtractionConfig(
        "token",
        "proxy",
        "proxy",
        country="VN",
        payment_method="momo",
        momo_trial_eligibility_check=True,
        momo_zero_trial_validation=True,
    )
    result = core.extract_momo_payment_link(config, transport_factory=factory)
    assert result.provider_value.startswith("https://payment.momo.vn/")
    assert factory.chat_calls == 0  # retained session came from eligibility
    assert events.index("checkout") < events.index("elements")
    assert events.index("elements") < events.index(("taxes", 1))
    assert events.index(("taxes", 3)) < events.index("token")
    assert events[1][0] == "prepare_flow"
