from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from payment_link_extractor.application import _normalize_config
from payment_link_extractor.auth import extract_session_token
from payment_link_extractor.channels import PAYMENT_CHANNELS
from payment_link_extractor.config import billing_for_country, currency_minor_scale
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.momo_core import _gateway_session_id, query_gateway, validate_momo_amount
from payment_link_extractor.momo_eligibility import probe_momo_trial_eligibility
from payment_link_extractor.momo_stripe import checkout_confirm, intent_confirm, resolve_momo_redirect, synchronize_momo_stripe_browser_ids, validate_momo_url
from payment_link_extractor.momo_checkout import create_checkout, taxes
from payment_link_extractor.momo_transport import MOMO_BROWSER_PROFILES, MomoTransportFactory, _set_proxy, capture_momo_csrf_token, momo_gateway_headers, momo_gateway_page_headers, momo_request_headers, normalize_momo_proxy
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
    assert {item["name"] for item in MOMO_BROWSER_PROFILES} >= {"chrome136", "chrome145", "chrome146", "chrome150"}
    assert MomoTransportFactory("chrome145").profile["user_agent"].find("Chrome/145.") >= 0
    assert MomoTransportFactory("chrome150").profile["user_agent"].find("Chrome/150.") >= 0
    assert MomoTransportFactory("chrome146").profile["user_agent"].find("Chrome/146.") >= 0
    assert MomoTransportFactory("chrome152").profile["user_agent"].find("Chrome/150.") >= 0


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
    finally:
        from payment_link_extractor.momo_transport import close

        close(session)


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
            assert "/accounts/check/v4-2023-04-27" in url
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
    assert events == ["eligibility_proxy:1", "eligibility_proxy:2", "eligibility_confirmed"]


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


def test_momo_stripe_ids_are_shared_with_chatgpt_and_stripe_sessions() -> None:
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
    assert result["__stripe_mid"] == stripe.cookies.get("__stripe_mid")
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
