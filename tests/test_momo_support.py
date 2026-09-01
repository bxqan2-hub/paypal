from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from payment_link_extractor.application import _normalize_config
from payment_link_extractor.channels import PAYMENT_CHANNELS
from payment_link_extractor.config import billing_for_country, currency_minor_scale
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.momo_core import _gateway_session_id, query_gateway, validate_momo_amount
from payment_link_extractor.momo_eligibility import probe_momo_trial_eligibility
from payment_link_extractor.momo_stripe import checkout_confirm, resolve_momo_redirect, validate_momo_url
from payment_link_extractor.momo_checkout import create_checkout
from payment_link_extractor.momo_transport import MOMO_BROWSER_PROFILES, MomoTransportFactory, _set_proxy, capture_momo_csrf_token, momo_gateway_headers, momo_request_headers, normalize_momo_proxy
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


def test_momo_route_and_ui_defaults() -> None:
    config = _config_from_payload({"access_token": "token", "proxy_pool": ["http://proxy"], "payment_method": "momo", "country": "US"})
    assert (config.country, config.payment_method, config.update_proxy) == ("VN", "momo", "http://proxy")
    app = create_app({"TESTING": True})
    data = app.test_client().get("/api/defaults", headers={"X-Workbench-Password": "test-password"}).get_json()
    methods = {item["value"]: item for item in data["payment_methods"]}
    assert methods["momo"]["label"] == "MoMo"
    assert data["payment_method_countries"]["momo"] == "VN"


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
    assert {item["name"] for item in MOMO_BROWSER_PROFILES} >= {"chrome145", "chrome150"}
    assert MomoTransportFactory("chrome145").profile["user_agent"].find("Chrome/145.") >= 0
    assert MomoTransportFactory("chrome150").profile["user_agent"].find("Chrome/150.") >= 0


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
        assert headers["oai-telemetry"] == "[1,null]"
        assert headers["Referer"] == "https://chatgpt.com/"
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


def test_momo_trial_eligibility_rotates_vn_proxies_before_checkout() -> None:
    class Response:
        status_code = 200

        def __init__(self, state: str):
            self.state = state

        def json(self):
            return {"state": self.state, "coupon": "plus-1-month-free"}

    class Session:
        def __init__(self, state: str):
            self.state = state
            self.headers = {}

        def request(self, method, url, **kwargs):
            assert method == "GET"
            assert "/promo_campaign/check_coupon" in url
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
    checkout = create_checkout(session, trial_eligible=True)
    assert checkout["processor_entity"] == "openai_llc"
    assert calls[0][2]["json"]["promo_campaign"]["promo_campaign_id"] == "plus-1-month-free"
    checkout_confirm(session, checkout, "ctoken_fixture")
    confirm_headers = calls[-1][2]["headers"]
    assert confirm_headers["x-openai-target-path"] == "/backend-api/payments/checkout/confirm"
    assert "/checkout/openai_llc/oaics_fixture" in confirm_headers["Referer"]


def test_momo_redirect_follows_stripe_authorize_hop() -> None:
    target = "https://payment.momo.vn/v2/gateway/pay?t=opaque&s=signature"

    class Session:
        def request(self, method, url, **kwargs):
            assert method == "GET"
            assert url.startswith("https://pm-redirects.stripe.com/")
            return SimpleNamespace(status_code=200, url=target, headers={})

    assert resolve_momo_redirect(Session(), "https://pm-redirects.stripe.com/authorize/example") == target
