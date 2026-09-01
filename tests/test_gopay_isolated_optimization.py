from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from payment_link_extractor import gopay_checkout
from payment_link_extractor import gopay_core
from payment_link_extractor import gopay_cs_live
from payment_link_extractor import gopay_stripe_common
from payment_link_extractor import gopay_transport
from payment_link_extractor.gopay_validation import validate_checkout_batch
from payment_link_extractor.models import ExtractionConfig


ROOT = Path(__file__).resolve().parents[1]


def test_gopay_sentinel_sdk_matches_both_complete_hars() -> None:
    sdk_path = ROOT / "payment_link_extractor/sentinel_assets/sentinel_sdk.js"
    data = sdk_path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == (
        "49d0284bf3eea8a59ebcad0e6b5dd8a53edd4c72606f15bbf51ebe5610a88efd"
    )
    source = data.decode("utf-8")
    assert "timing=function" in source
    assert gopay_transport.SENTINEL_SDK_VERSION == "20260810913b"
    injected = gopay_transport.BrowserSentinelProvider._build_sentinel_init_script()
    assert "__opllSentinelReferer" in injected
    assert "referrerPolicy: 'strict-origin-when-cross-origin'" in injected


def test_gopay_uses_isolated_copy_and_paypal_core_is_unchanged() -> None:
    adapter = (ROOT / "payment_link_extractor/gopay_channel.py").read_text(encoding="utf-8")
    assert "gopay_core" in adapter
    assert "paypal_channel" not in adapter
    assert (ROOT / "payment_link_extractor/gopay_core.py").is_file()
    assert (ROOT / "payment_link_extractor/gopay_transport.py").is_file()
    # Git blob hash of the PayPal core at the shared-core baseline 7ec66b3.
    import subprocess

    result = subprocess.run(
        ["git", "hash-object", "payment_link_extractor/paypal_channel.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    assert result.stdout.strip() == "8b2191fceceb103014682b822658051c8dfab4e6"


def test_gopay_transport_matches_har_defaults_without_touching_paypal_transport(monkeypatch) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}

    monkeypatch.setattr(gopay_transport, "new_session", Session)
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="ID",
        payment_method="gopay",
        apply_checkout_update=False,
    )
    session = gopay_transport.GoPayTransportFactory().chatgpt(config, config.checkout_proxy)
    assert isinstance(
        session.openai_sentinel_provider,
        gopay_transport.PlaywrightSentinelProvider,
    )
    assert session.headers["oai-language"] == "id-ID"
    assert session.headers["oai-client-build-number"] == "10012890"
    assert session.headers["oai-client-version"] == "prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4"
    session.openai_checkout_telemetry = "[1,627.5,21,23,28,2,0,631]"
    telemetry = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout"
    )["oai-telemetry"]
    values = json.loads(telemetry)
    assert len(values) == 8
    assert values[0] == 1 and values[5:] == [2, 0, values[7]]
    assert 600 <= values[1] <= 760
    assert 3 <= values[7] - values[1] <= 5
    assert values == [1, 627.5, 21, 23, 28, 2, 0, 631]
    session.openai_approve_telemetry = "[1,725.3,113,28,59,2,0,729]"
    approve_headers = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout/approve"
    )
    approve_values = json.loads(approve_headers["oai-telemetry"])
    assert len(approve_values) == 8
    assert approve_values[0] == 1 and approve_values[5:] == [2, 0, approve_values[7]]
    assert 600 <= approve_values[1] <= 760
    assert 3 <= approve_values[7] - approve_values[1] <= 5
    assert approve_values == [1, 725.3, 113, 28, 59, 2, 0, 729]
    assert approve_headers["x-oai-is-pending-updates"] == '{"v":3,"updates":[]}'


def test_gopay_device_id_is_stable_per_access_token(monkeypatch) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}

    monkeypatch.setattr(gopay_transport, "new_session", Session)

    def build(token: str) -> str:
        config = ExtractionConfig(
            access_token=token,
            checkout_proxy="http://proxy.example:8080",
            update_proxy="http://proxy.example:8080",
            country="ID",
            payment_method="gopay",
        )
        return gopay_transport.GoPayTransportFactory().chatgpt(
            config, config.checkout_proxy
        ).headers["oai-device-id"]

    assert build("fixture-token-a") == build("fixture-token-a")
    assert build("fixture-token-a") != build("fixture-token-b")


def test_gopay_browser_profile_rotation_and_tls_ua_validation(monkeypatch) -> None:
    monkeypatch.delenv("OPLL_GOPAY_BROWSER_PROFILE", raising=False)
    profiles = {
        gopay_transport.select_gopay_browser_profile()["name"] for _ in range(80)
    }
    assert profiles.issubset({item["name"] for item in gopay_transport.GOPAY_BROWSER_PROFILES})
    assert len(profiles) >= 2
    assert gopay_transport.validate_tls_ua_consistency(
        "chrome131",
        "Mozilla/5.0 Chrome/131.0.0.0 Safari/537.36",
    )
    with pytest.raises(Exception, match="TLS/UA version mismatch"):
        gopay_transport.validate_tls_ua_consistency(
            "chrome131",
            "Mozilla/5.0 Chrome/151.0.0.0 Safari/537.36",
        )


def test_gopay_fingerprint_validation_rejects_malformed_values() -> None:
    context = gopay_stripe_common.stripe_context({}, {"currency": "IDR"})
    assert gopay_stripe_common.validate_gopay_fingerprint_params(context)
    context["guid"] = "invalid"
    with pytest.raises(ValueError, match="fingerprint guid"):
        gopay_stripe_common.validate_gopay_fingerprint_params(context)


def test_gopay_playwright_provider_keeps_one_runtime_for_init_and_token(monkeypatch) -> None:
    calls: list[tuple[str, object]] = []

    class Daemon:
        def open_session(self, **kwargs):
            calls.append(("open", kwargs["device_id"]))
            return {
                "runtime_id": "runtime-fixture",
                "attestation": "a" * 291,
                "cookie_header": "oai-did=device-fixture",
                "latest_receipt": "receipt-fixture",
                "profile_path": "C:/runtime/profile",
            }

        def prepare_flow(self, runtime_id, flow, page_url):
            calls.append(("init", (runtime_id, flow, page_url)))
            return {
                "attestation": "a" * 291,
                "cookie_header": "oai-did=device-fixture; __stripe_mid=muid-fixture",
                "request_events": [
                    {"path": "/backend-api/sentinel/req", "body_length": 700},
                    {"path": "/backend-api/sentinel/ping", "body_length": 0},
                ],
            }

        def token(self, runtime_id, flow, page_url):
            calls.append(("token", (runtime_id, flow, page_url)))
            return {
                "token": "proof-fixture",
                "timing": [1, 620.5, 7, 30, 26, 2, 0, 624],
                "attestation": "a" * 291,
                "cookie_header": "oai-did=device-fixture; __stripe_mid=muid-fixture",
                "request_events": [
                    {"path": "/backend-api/sentinel/ping", "body_length": 0}
                ],
            }

        def set_cookie(self, runtime_id, name, value, http_only):
            calls.append(("cookie", (runtime_id, name, value, http_only)))
            return {
                "cookie_header": (
                    "oai-did=device-fixture; __stripe_mid=muid-fixture; "
                    f"{name}={value}"
                )
            }

        def close_session(self, runtime_id):
            calls.append(("close", runtime_id))

    daemon = Daemon()
    monkeypatch.setattr(
        gopay_transport.PlaywrightSentinelProvider,
        "_daemon",
        staticmethod(lambda: daemon),
    )
    session = SimpleNamespace(
        headers={},
        openai_sentinel_observer_enabled=False,
    )
    provider = gopay_transport.PlaywrightSentinelProvider(
        access_token="fixture-token",
        device_id="device-fixture",
        session_id="session-fixture",
        user_agent="fixture-agent",
        proxy="http://proxy.example:8080",
        transport_session=session,
        language="id-ID",
        timezone="Asia/Jakarta",
    )
    referer = "https://chatgpt.com/checkout/openai_llc/cs_fixture"
    provider.prepare_flow(flow="checkout_session_approval", referer=referer)
    headers = provider.headers("checkout_session_approval", referer=referer)
    provider.set_cookie("__stripe_sid", "sid-fixture")
    provider.close()

    assert headers["OpenAI-Sentinel-Token"] == "proof-fixture"
    assert len(headers["oai-web-deployment-attestation"]) == 291
    assert session.openai_sentinel_prepare_events == [
        {"path": "/backend-api/sentinel/req", "body_length": 700},
        {"path": "/backend-api/sentinel/ping", "body_length": 0},
    ]
    assert session.openai_sentinel_token_events == [
        {"path": "/backend-api/sentinel/ping", "body_length": 0}
    ]
    assert json.loads(session.openai_approve_telemetry) == [
        1,
        620.5,
        7,
        30,
        26,
        2,
        0,
        624,
    ]
    assert [name for name, _ in calls] == ["open", "init", "token", "cookie", "close"]


def test_gopay_transport_propagates_har_pending_update_receipt() -> None:
    class Session:
        def __init__(self) -> None:
            self.headers = {"x-oai-is-pending-updates": '{"v":3,"updates":[]}'}

        def request(self, *_args, **_kwargs):
            class Response:
                status_code = 200
                text = "{}"
                headers = {
                    "x-oai-is-receipt": "ois1.fixture",
                    "x-oai-is-update": "must-not-be-echoed",
                }

            return Response()

    session = Session()
    response = gopay_transport.stage_http_request(
        session,
        "fixture",
        "GET",
        "https://chatgpt.com/backend-api/fixture",
        None,
    )
    assert response.status_code == 200
    assert json.loads(session.headers["x-oai-is-pending-updates"]) == {
        "v": 3,
        "updates": ["ois1.fixture"],
    }


def test_gopay_transport_clears_pending_receipts_on_ack() -> None:
    class Session:
        def __init__(self) -> None:
            self.headers = {
                "x-oai-is-pending-updates": '{"v":3,"updates":["old-receipt"]}'
            }

        def request(self, *_args, **_kwargs):
            class Response:
                status_code = 200
                text = "{}"
                headers = {
                    "x-oai-is-receipt": "new-receipt",
                    "x-oai-is-pending-updates-ack": "acknowledged",
                }

            return Response()

    session = Session()
    gopay_transport.stage_http_request(
        session, "fixture", "POST", "https://chatgpt.com/backend-api/fixture"
    )
    assert session.headers["x-oai-is-pending-updates"] == '{"v":3,"updates":[]}'


def test_gopay_browser_provider_syncs_pending_update_header(monkeypatch) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

    provider = object.__new__(gopay_transport.BrowserSentinelProvider)
    provider.transport_session = Session()
    monkeypatch.setattr(
        provider,
        "_run",
        lambda _args, timeout=20: {
            "success": True,
            "data": {
                    "requests": [
                        {"responseHeaders": {"x-oai-is-receipt": "ois1.browser-fixture"}}
                    ]
            },
        },
    )
    provider._sync_pending_update_from_browser()
    assert json.loads(provider.transport_session.headers["x-oai-is-pending-updates"]) == {
        "v": 3,
        "updates": ["ois1.browser-fixture"],
    }


def test_gopay_browser_provider_recovers_attestation_from_request_monitor(monkeypatch) -> None:
    provider = object.__new__(gopay_transport.BrowserSentinelProvider)
    provider._attestation = ""
    monkeypatch.setattr(provider, "_eval", lambda _expression: {})
    monkeypatch.setattr(
        provider,
        "_run",
        lambda _args, timeout=20: {
            "data": {
                "requests": [
                    {
                        "requestHeaders": {
                            "OAI-Web-Deployment-Attestation": "a" * 291
                        }
                    }
                ]
            }
        },
    )
    provider._capture_bootstrap()
    assert provider._attestation == "a" * 291


def test_gopay_browser_token_uses_sdk_timing_without_manual_ping(monkeypatch) -> None:
    events: list[str] = []
    provider = object.__new__(gopay_transport.BrowserSentinelProvider)
    provider._lock = __import__("threading").RLock()
    provider._failed = False
    provider._started = True
    provider._attestation = ""
    provider._cookies = ""
    provider.device_id = "device-fixture"
    provider.transport_session = type(
        "Session",
        (),
        {"headers": {}, "openai_sentinel_observer_enabled": False},
    )()

    def fake_eval(expression, timeout=75):
        if "SentinelSDK.token" in expression:
            events.append("token")
            return {
                "token": "proof-fixture",
                "timing": "[1,620.5,7,30,26,2,0,624]",
            }
        raise AssertionError(expression)

    monkeypatch.setattr(provider, "_eval", fake_eval)
    monkeypatch.setattr(provider, "_sync_cookies", lambda: events.append("cookies"))

    headers = provider.headers("checkout_session_approval", referer="https://chatgpt.com/checkout/fixture")
    assert headers["OpenAI-Sentinel-Token"] == "proof-fixture"
    assert events == ["token", "cookies"]
    assert json.loads(provider.transport_session.openai_approve_telemetry) == [
        1,
        620.5,
        7,
        30,
        26,
        2,
        0,
        624,
    ]


def test_gopay_browser_prepare_flow_uses_sdk_init_without_exporting_token(monkeypatch) -> None:
    provider = object.__new__(gopay_transport.BrowserSentinelProvider)
    provider._lock = __import__("threading").RLock()
    provider._failed = False
    provider._started = True
    expressions: list[str] = []
    cookie_sync: list[str] = []
    monkeypatch.setattr(
        provider,
        "_eval",
        lambda expression, timeout=90: expressions.append(expression) or True,
    )
    monkeypatch.setattr(provider, "_sync_cookies", lambda: cookie_sync.append("cookies"))
    provider.prepare_flow(
        flow="checkout_session_approval",
        referer="https://chatgpt.com/checkout/openai_llc/cs_fixture",
    )
    assert len(expressions) == 1
    assert 'window.__opllSentinelReferer="https://chatgpt.com/checkout/openai_llc/cs_fixture"' in expressions[0]
    assert 'SentinelSDK.init("checkout_session_approval")' in expressions[0]
    assert "SentinelSDK.token" not in expressions[0]
    assert cookie_sync == ["cookies"]


def test_gopay_stripe_metrics_reuse_chatgpt_browser_cookie_identity() -> None:
    class Session:
        headers = {
            "Cookie": "oai-did=device-fixture; __stripe_mid=muid-cookie-fixture; __stripe_sid=sid-cookie-fixture"
        }

    ctx = {"muid": "random-muid", "sid": "random-sid"}
    gopay_transport.synchronize_stripe_browser_ids(Session(), ctx)
    assert ctx["muid"] == "muid-cookie-fixture"
    assert ctx["sid"] == "sid-cookie-fixture"


def test_gopay_stripe_metrics_seed_missing_browser_mid_cookie() -> None:
    installed: list[tuple[str, str, bool]] = []

    class Provider:
        def set_cookie(self, name, value, *, http_only=False):
            installed.append((name, value, http_only))

    class Session:
        def __init__(self):
            self.headers = {"Cookie": "oai-did=device-fixture"}
            self.openai_sentinel_provider = Provider()

    session = Session()
    ctx = {"muid": "muid-generated-fixture", "sid": "sid-generated-fixture"}
    gopay_transport.synchronize_stripe_browser_ids(session, ctx)
    assert installed == [("__stripe_mid", "muid-generated-fixture", False)]
    assert "__stripe_mid=muid-generated-fixture" in session.headers["Cookie"]
    assert ctx["sid"] == "NA"


def test_gopay_promo_probe_does_not_create_browser_checkout_state(monkeypatch) -> None:
    calls: list[str] = []
    captured: dict[str, object] = {}

    class Session:
        def __init__(self) -> None:
            self.openai_sentinel_provider = type(
                "Provider", (), {"prepare": lambda _self: calls.append("prepare")}
            )()
            self.proxies: dict[str, str] = {}

    class Response:
        status_code = 200
        text = '{"state":"eligible"}'
        headers: dict[str, str] = {}

        def json(self):
            return {"state": "eligible"}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_checkout, "stage_http_request", fake_request)
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    result = gopay_checkout.check_coupon_eligibility(config, Session(), None)
    assert result["state"] == "eligible"
    assert result["eligible"] is True
    assert result["coupon"] == "plus-1-month-free"
    assert result["http_status"] == 200
    assert captured["headers"]["Accept"] == "application/json"
    assert captured["headers"]["Referer"] == "https://chatgpt.com/"
    assert captured["headers"]["x-openai-target-path"] == "/backend-api/promo_campaign/check_coupon"
    assert calls == []


def test_gopay_checkout_starts_from_promo_context(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Provider:
        def headers(self, _flow, **_kwargs):
            return {
                "OpenAI-Sentinel-Token": "proof-fixture",
                "OpenAI-Sentinel-SO-Token": "observer-fixture",
                "oai-web-deployment-attestation": "a" * 291,
            }

    class Session:
        openai_sentinel_provider = Provider()

    class Response:
        status_code = 200
        text = '{"checkout_session_id":"cs_fixture"}'
        headers: dict[str, str] = {}

        def json(self):
            return {"checkout_session_id": "cs_fixture"}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_checkout, "stage_http_request", fake_request)
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    commits: list[str] = []
    checkout = gopay_checkout.create_checkout(
        config,
        Session(),
        None,
        commit_callback=lambda: commits.append("checkout_committed"),
    )
    assert checkout["session_kind"] == "stripe_checkout"
    body = captured["json"]
    assert body["promo_campaign"] == {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": True,
    }
    assert body["check_card_proxy"] is True
    assert captured["headers"]["Referer"].endswith(
        "/?promo_campaign=plus-1-month-free"
    )
    assert "OpenAI-Sentinel-SO-Token" not in captured["headers"]
    assert len(captured["headers"]["oai-web-deployment-attestation"]) == 291
    assert commits == ["checkout_committed"]


def test_gopay_required_sentinel_proof_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="browser Sentinel provider is required"):
        gopay_transport.openai_sentinel_headers(
            SimpleNamespace(), flow="chatgpt_checkout", required=True
        )


def test_gopay_checkout_commits_with_required_playwright_token_without_attestation(monkeypatch) -> None:
    class Provider:
        def headers(self, _flow, **_kwargs):
            return {"OpenAI-Sentinel-Token": "proof-fixture"}

    session = SimpleNamespace(openai_sentinel_provider=Provider())
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    class Response:
        status_code = 200
        text = '{"checkout_session_id":"cs_fixture"}'
        headers: dict[str, str] = {}

        def json(self):
            return {"checkout_session_id": "cs_fixture"}

    monkeypatch.setattr(gopay_checkout, "stage_http_request", lambda *_args, **_kwargs: Response())
    committed: list[bool] = []
    checkout = gopay_checkout.create_checkout(
        config,
        session,
        None,
        commit_callback=lambda: committed.append(True),
    )
    assert checkout["cs_id"] == "cs_fixture"
    assert committed == [True]


def test_gopay_sentinel_init_script_uses_dedicated_assets() -> None:
    script = gopay_transport.BrowserSentinelProvider._build_sentinel_init_script()
    assert "window.SentinelSDK = SentinelSDK" in script
    assert "entry.responseStart - entry.requestStart" in script
    assert "s-cf-edge-msec" in script
    assert "s-cf-origin-ttfb-msec" in script
    assert "s-cf-quic-rtt-msec" in script
    assert "protocolCode" in script
    assert "mk_gcash_open_source" not in script


def test_gopay_checkout_methods_merge_nested_values() -> None:
    state: dict[str, object] = {
        "payment_method_types": ["card", "gopay"],
        "custom_payment_methods": [{"id": "cpmt_1", "name": "GoPay"}],
    }
    gopay_checkout.merge_checkout_payload(
        state,
        {
            "checkout_session": {
                "payment_method_types": ["gopay", "link"],
                "custom_payment_methods": [
                    {"id": "cpmt_1", "display_name": "GoPay Indonesia"},
                    {"id": "cpmt_2", "name": "Bank"},
                ],
            }
        },
    )
    assert state["payment_method_types"] == ["card", "gopay", "link"]
    assert state["custom_payment_methods"] == [
        {"id": "cpmt_1", "name": "GoPay", "display_name": "GoPay Indonesia"},
        {"id": "cpmt_2", "name": "Bank"},
    ]


def test_gopay_amount_gate_is_zero_only() -> None:
    gopay_core.validate_gopay_amount(0, promotion_applied=True)
    with pytest.raises(Exception, match="expected zero amount, got missing"):
        gopay_core.validate_gopay_amount(None, promotion_applied=True)
    with pytest.raises(Exception, match="expected zero amount, got 349000"):
        gopay_core.validate_gopay_amount(349000, promotion_applied=True)


def test_gopay_provider_flows_defer_zero_gate_to_core() -> None:
    """The switch must control the only zero gate after provider generation."""
    for relative in (
        "payment_link_extractor/gopay_cs_live.py",
        "payment_link_extractor/gopay_oaics.py",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "validate_gopay_amount" not in source
        assert "expected zero amount" not in source


def test_gopay_stripe_confirm_includes_browser_checksum_fields(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "{}"
        headers: dict[str, str] = {}

        def json(self):
            return {}

    captured: dict[str, object] = {}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    checkout = {
        "cs_id": "cs_live_fixture",
        "publishable_key": "pk_live_fixture",
    }
    init_payload = {
        "id": "ppage_fixture",
        "init_checksum": "init-fixture",
        "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_fixture",
    }
    ctx = gopay_cs_live.stripe_context(init_payload, checkout, "stripe-js-fixture")
    billing = {
        "name": "Budi Santoso",
        "email": "budi@example.com",
        "phone": "+622112345678",
        "country": "ID",
        "line1": "Jl. Sudirman",
        "city": "Jakarta",
        "state": "DKI Jakarta",
        "postal_code": "10220",
    }

    gopay_cs_live.stripe_confirm_cs_live(
        object(),
        checkout,
        init_payload,
        ctx,
        init_payload["stripe_hosted_url"],
        "gopay",
        billing,
        None,
    )

    body = captured["data"]
    assert isinstance(body, dict)
    assert body["js_checksum"] == gopay_cs_live.stripe_js_checksum("ppage_fixture")
    assert body["rv_timestamp"] == gopay_cs_live.stripe_rv_timestamp()
    assert all(len(str(body[key])) == 42 for key in ("guid", "muid", "sid"))
    # HAR confirm requests contain 60 form fields, including both browser
    # integrity values that were missing from the previous implementation.
    assert len(body) == 60


def test_gopay_stripe_confirm_forwards_optional_passive_captcha(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "{}"
        headers: dict[str, str] = {}

        def json(self):
            return {}

    captured: dict[str, object] = {}
    monkeypatch.setattr(
        gopay_cs_live,
        "stage_http_request",
        lambda *_args, **kwargs: (captured.update(kwargs) or Response()),
    )
    checkout = {"cs_id": "cs_live_fixture", "publishable_key": "pk_live_fixture"}
    init_payload = {
        "id": "ppage_fixture",
        "init_checksum": "init-fixture",
        "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_live_fixture",
    }
    ctx = gopay_cs_live.stripe_context(init_payload, checkout, "stripe-js-fixture")
    billing = {
        "name": "Budi Santoso",
        "email": "budi@example.com",
        "country": "ID",
        "line1": "Jl. Sudirman",
        "city": "Jakarta",
        "state": "DKI Jakarta",
        "postal_code": "10220",
    }
    gopay_cs_live.stripe_confirm_cs_live(
        object(),
        checkout,
        init_payload,
        ctx,
        init_payload["stripe_hosted_url"],
        "gopay",
        billing,
        None,
        passive_captcha_token="P1_fixture",
    )
    assert captured["data"]["passive_captcha_token"] == "P1_fixture"
    assert captured["data"]["passive_captcha_ekey"] == ""
    assert len(captured["data"]) == 62


def test_gopay_approve_requests_checkout_session_approval_proof(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = '{"result":"approved"}'
        headers: dict[str, str] = {}

        def json(self):
            return {"result": "approved"}

    events: list[str] = []
    requests: list[dict[str, object]] = []

    class Provider:
        def __init__(self, owner):
            self.owner = owner

        def headers(self, flow, **_kwargs):
            events.append(f"headers:{flow}")
            self.owner.headers["Cookie"] = "final-ping-cookie"
            return {
                "OpenAI-Sentinel-Token": "proof-fixture",
                "OpenAI-Sentinel-SO-Token": "observer-fixture",
                "Cookie": "stale-prefetch-cookie",
                "oai-web-deployment-attestation": "a" * 291,
            }

        def prepare_flow(self, *, flow, referer):
            events.append(f"prepare:{flow}")

    class Session:
        def __init__(self):
            self.headers = {"Cookie": "current-approve-cookie"}
            self.openai_sentinel_provider = Provider(self)

    def fake_request(*_args, **kwargs):
        requests.append({**kwargs, "session_cookie": _args[0].headers["Cookie"]})
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    checkout = {"cs_id": "cs_live_fixture", "billing_country": "ID"}
    session = Session()
    gopay_cs_live.prefetch_checkout_approval_proof(session, checkout, None)
    gopay_cs_live.chatgpt_approve(
        session,
        checkout,
        None,
    )
    assert events == [
        "prepare:checkout_session_approval",
        "headers:checkout_session_approval",
    ]
    assert len(requests) == 1
    headers = requests[0]["headers"]
    assert headers["OpenAI-Sentinel-Token"] == "proof-fixture"
    assert "OpenAI-Sentinel-SO-Token" not in headers
    assert headers["x-oai-is-pending-updates"] == '{"v":3,"updates":[]}'
    assert "Cookie" not in headers
    assert requests[0]["session_cookie"] == "final-ping-cookie"
    assert "_gopay_checkout_approval_headers" not in checkout


def test_gopay_taxes_does_not_mint_an_extra_sentinel_proof(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        text = '{"checkout_session":{}}'
        headers: dict[str, str] = {}

        def json(self):
            return {"checkout_session": {}}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    checkout = {
        "cs_id": "cs_live_fixture",
        "billing_country": "ID",
        "processor_entity": "openai_llc",
        "currency": "IDR",
    }
    billing = {
        "name": "Budi Santoso",
        "email": "budi@example.com",
        "country": "ID",
        "line1": "Jl. Sudirman",
        "city": "Jakarta",
        "state": "DKI Jakarta",
        "postal_code": "10220",
    }
    gopay_cs_live.cs_checkout_taxes(config, object(), checkout, billing, None)
    headers = captured["headers"]
    assert headers["x-openai-target-path"] == "/backend-api/payments/checkout/taxes"
    assert "OpenAI-Sentinel-Token" not in headers
    assert "openai-sentinel-token" not in headers


def test_gopay_consumer_lookup_matches_both_har_parameter_sets(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        text = '{"consumer_session":null}'
        headers: dict[str, str] = {}

        def json(self):
            return {"consumer_session": None}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    checkout = {
        "cs_id": "cs_live_fixture",
        "publishable_key": "pk_live_fixture",
        "checkout_state": {"email": "account@example.com"},
    }
    billing = {"email": "billing@example.com"}
    result = gopay_cs_live.stripe_consumer_session_lookup(
        object(), checkout, billing, None
    )
    assert result == {"consumer_session": None}
    assert captured["data"] == {
        "request_surface": "web_elements_controller",
        "email_address": "account@example.com",
        "email_source": "default_value",
        "session_id": "cs_live_fixture",
        "key": "pk_live_fixture",
        "do_not_log_consumer_funnel_event": "true",
    }
    assert captured["headers"] == {
        **gopay_cs_live.cs_stripe_headers(),
        "Accept-Language": "en",
    }


def test_gopay_stripe_init_negotiates_manual_approval_on_first_request(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        text = '{"id":"ppage_fixture"}'
        headers: dict[str, str] = {}

        def json(self):
            return {"id": "ppage_fixture"}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    checkout = {
        "cs_id": "cs_live_fixture",
        "publishable_key": "pk_live_fixture",
    }
    gopay_cs_live.stripe_init(config, checkout, None, object())
    data = captured["data"]
    assert data["_stripe_version"] == gopay_cs_live.STRIPE_VERSION_FULL
    assert data["elements_session_client[client_betas][0]"] == "custom_checkout_server_updates_1"
    assert data["elements_session_client[client_betas][1]"] == "custom_checkout_manual_approval_1"
    assert len(data) == 13


def test_gopay_stripe_init_retries_transport_on_same_checkout(monkeypatch) -> None:
    calls: list[str] = []

    class Response:
        status_code = 200
        text = '{"id":"ppage_fixture"}'
        headers: dict[str, str] = {}

        def json(self):
            return {"id": "ppage_fixture"}

    def fake_request(*args, **_kwargs):
        calls.append(args[3])
        if len(calls) < 3:
            from payment_link_extractor.errors import NetworkError

            raise NetworkError("Stripe payment_pages init", "fixture timeout")
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    monkeypatch.setattr(gopay_cs_live.time, "sleep", lambda _seconds: None)
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    checkout = {
        "cs_id": "cs_live_fixture",
        "publishable_key": "pk_live_fixture",
    }
    gopay_cs_live.stripe_init(config, checkout, None, object())
    assert calls == [
        "https://api.stripe.com/v1/payment_pages/cs_live_fixture/init"
    ] * 3


def test_gopay_elements_uses_primary_locale_and_browser_timezone(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        text = '{"payment_method_types":["gopay"]}'
        headers: dict[str, str] = {}

        def json(self):
            return {"payment_method_types": ["gopay"]}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    checkout = {
        "cs_id": "cs_live_fixture",
        "publishable_key": "pk_live_fixture",
        "payment_locale": "id-ID",
        "browser_timezone": "Asia/Jakarta",
        "currency": "IDR",
    }
    init_payload = {
        "id": "ppage_fixture",
        "config_id": "init-config",
        "currency": "idr",
        "total_summary": {"due": 0},
        "payment_method_types": ["card", "gopay"],
        "payment_method_configuration": {"id": "pmc_fixture"},
    }
    ctx = gopay_stripe_common.stripe_context(init_payload, checkout, "stripe-js-fixture")
    gopay_cs_live.cs_elements_session(object(), checkout, init_payload, ctx, None)
    assert ctx["locale"] == "id"
    assert captured["params"]["locale"] == "id"
    assert captured["params"]["browser_timezone"] == "Asia/Jakarta"
    assert len(captured["params"]) == 19


def test_gopay_confirm_splits_initial_and_latest_checkout_config_ids(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        text = "{}"
        headers: dict[str, str] = {}

        def json(self):
            return {}

    def fake_request(*_args, **kwargs):
        captured.update(kwargs)
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    checkout = {
        "cs_id": "cs_live_fixture",
        "publishable_key": "pk_live_fixture",
        "payment_locale": "id-ID",
        "billing_country": "ID",
        "checkout_state": {"email": "account@example.com"},
    }
    init_payload = {
        "id": "ppage_fixture",
        "config_id": "init-config",
        "init_checksum": "a" * 32,
        "currency": "idr",
        "total_summary": {"due": 0},
    }
    ctx = gopay_stripe_common.stripe_context(init_payload, checkout, "stripe-js-fixture")
    ctx.update(
        {
            "checkout_config_id": "latest-config",
            "elements_session_id": "elements-session",
            "elements_session_config_id": "elements-config",
        }
    )
    billing = {
        "name": "Budi Santoso",
        "email": "static@example.invalid",
        "country": "ID",
        "line1": "Jl. Sudirman",
        "city": "Jakarta",
        "state": "Jambi",
        "postal_code": "10220",
    }
    gopay_cs_live.stripe_confirm_cs_live(
        object(),
        checkout,
        init_payload,
        ctx,
        "https://checkout.stripe.com/c/pay/cs_live_fixture",
        "gopay",
        billing,
        None,
    )
    data = captured["data"]
    assert data["client_attribution_metadata[checkout_config_id]"] == "latest-config"
    assert data["payment_method_data[client_attribution_metadata][checkout_config_id]"] == "init-config"
    assert data["payment_method_data[billing_details][email]"] == "account@example.com"


def test_gopay_confirm_uses_har_stripe_runtime_without_precreating_payment_method() -> None:
    assert gopay_cs_live.GOPAY_STRIPE_RUNTIME_VERSION == "b0f5e7abe5"
    source = (ROOT / "payment_link_extractor/gopay_cs_live.py").read_text(encoding="utf-8")
    flow = source[source.index("def extract_cs_live_provider(") : source.index("def payment_method_types(")]
    assert "stripe_create_payment_method(" not in flow
    assert "stripe_confirm_cs_live(" in flow
    assert flow.index("synchronize_stripe_browser_ids(") < flow.index("cs_elements_session(")
    assert flow.index("prefetch_checkout_approval_proof(") < flow.index("cs_checkout_taxes(")
    assert flow.index("prefetch_checkout_approval_proof(") < flow.index("stripe_consumer_session_lookup(")
    assert flow.index("stripe_consumer_session_lookup(") < flow.index("cs_checkout_taxes(")
    assert flow.index("cs_checkout_taxes(") < flow.index("stripe_confirm_cs_live(")


def test_gopay_tax_region_matches_har_progressive_sequence(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "{}"
        headers: dict[str, str] = {}

        def json(self):
            return {
                "config_id": f"checkout-config-{len(requests)}",
                "total_summary": {"total": 0},
            }

    requests: list[dict[str, str]] = []

    def fake_request(*_args, **kwargs):
        requests.append(dict(kwargs["data"]))
        return Response()

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fake_request)
    checkout = {"cs_id": "cs_live_fixture", "publishable_key": "pk_live_fixture"}
    ctx = {
        "stripe_js_id": "stripe-js-fixture",
        "elements_session_id": "elements-fixture",
        "locale": "id",
    }
    billing = {
        "country": "ID",
        "line1": "Jl. Jenderal Sudirman No. 45, Jakarta, DKI Jakarta",
        "city": "Jakarta",
        "state": "Kalimantan Utara",
        "postal_code": "10220",
    }

    gopay_cs_live.cs_update_tax_region(
        object(), checkout, ctx, billing, None
    )

    tax_keys = [
        [key for key in data if key.startswith("tax_region[")]
        for data in requests
    ]
    assert tax_keys == [
        ["tax_region[country]"],
        ["tax_region[country]", "tax_region[line1]"],
        ["tax_region[country]", "tax_region[line1]", "tax_region[city]"],
        [
            "tax_region[country]",
            "tax_region[line1]",
            "tax_region[city]",
            "tax_region[state]",
        ],
        [
            "tax_region[country]",
            "tax_region[line1]",
            "tax_region[city]",
            "tax_region[state]",
            "tax_region[postal_code]",
        ],
    ]
    assert ctx["checkout_config_id"] == "checkout-config-5"


def test_gopay_provider_uses_complete_har_tax_and_snapshot_cadence(monkeypatch) -> None:
    events: list[str] = []

    monkeypatch.setattr(
        gopay_cs_live,
        "stripe_init",
        lambda *_args: (
            {
                "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_fixture",
                "payment_method_types": ["gopay"],
                "total_summary": {"due": 0},
                "currency": "idr",
            },
            "stripe-js-fixture",
        ),
    )
    monkeypatch.setattr(gopay_cs_live, "ensure_payment_method_offered", lambda *_args: None)
    monkeypatch.setattr(
        gopay_cs_live,
        "stripe_context",
        lambda *_args, **_kwargs: {
            "checkout_amount": 0,
            "currency": "idr",
            "stripe_js_id": "stripe-js-fixture",
            "elements_session_id": "elements-fixture",
            "elements_session_config_id": "elements-config",
            "config_id": "init-config",
            "checkout_config_id": "init-config",
            "locale": "id",
        },
    )
    monkeypatch.setattr(gopay_cs_live, "synchronize_stripe_browser_ids", lambda *_args: None)
    monkeypatch.setattr(
        gopay_cs_live,
        "cs_elements_session",
        lambda *_args, **_kwargs: {"payment_method_types": ["gopay"]},
    )
    monkeypatch.setattr(
        gopay_cs_live,
        "prefetch_checkout_approval_proof",
        lambda *_args: events.append("approval_init"),
    )
    monkeypatch.setattr(
        gopay_cs_live,
        "stripe_consumer_session_lookup",
        lambda *_args: events.append("consumer_lookup") or {},
    )

    def tax_fields(_stripe, _checkout, _ctx, billing, _log, fields, accumulated=None):
        current = dict(accumulated or {})
        current.update({field: billing[field] for field in fields})
        events.append("tax_region:" + ",".join(fields))
        return {}, current

    monkeypatch.setattr(gopay_cs_live, "_cs_update_tax_region_fields", tax_fields)
    monkeypatch.setattr(
        gopay_cs_live,
        "cs_snapshot_billing",
        lambda *_args: events.append("snapshot"),
    )
    monkeypatch.setattr(
        gopay_cs_live,
        "cs_checkout_taxes",
        lambda *_args: events.append("taxes") or {},
    )
    monkeypatch.setattr(
        gopay_cs_live,
        "cs_checkout_page_refresh",
        lambda *_args: events.append("page_get") or {},
    )
    monkeypatch.setattr(gopay_cs_live, "stripe_confirm_cs_live", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        gopay_cs_live,
        "provider_redirect_after_confirm",
        lambda *_args: "https://pm-redirects.stripe.com/authorize/fixture",
    )
    monkeypatch.setattr(
        gopay_cs_live,
        "resolve_external_redirect",
        lambda *_args, **_kwargs: "https://app.midtrans.com/snap/v4/redirection/fixture",
    )
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    checkout = {
        "cs_id": "cs_live_fixture",
        "publishable_key": "pk_live_fixture",
        "payment_locale": "id-ID",
        "billing_country": "ID",
        "currency": "IDR",
    }
    billing = {
        "name": "Budi Santoso",
        "email": "account@example.com",
        "phone": "000",
        "country": "ID",
        "line1": "Jl. Sudirman",
        "city": "Jakarta",
        "state": "Jambi",
        "postal_code": "10220",
    }
    result = gopay_cs_live.extract_cs_live_provider(
        config, object(), object(), checkout, billing, None
    )
    assert result["gopay_url"].startswith("https://app.midtrans.com/")
    assert events == [
        "approval_init",
        "consumer_lookup",
        "tax_region:country,line1,city",
        "snapshot",
        "tax_region:state",
        "snapshot",
        "taxes",
        "page_get",
        "tax_region:postal_code",
        "taxes",
        "page_get",
    ]


def test_gopay_core_zero_validation_off_skips_only_steps_one_and_six(monkeypatch) -> None:
    class Session:
        def close(self) -> None:
            return None

    class Factory:
        def __init__(self) -> None:
            self.chatgpt_calls = 0
            self.stripe_calls = 0

        def chatgpt(self, _config, _proxy):
            self.chatgpt_calls += 1
            return Session()

        def stripe(self, _config):
            self.stripe_calls += 1
            return Session()

    factory = Factory()
    calls: list[str] = []
    monkeypatch.setattr(
        gopay_core,
        "check_coupon_eligibility",
        lambda *_args: pytest.fail("eligibility step must be skipped when the switch is off"),
    )
    monkeypatch.setattr(
        gopay_core,
        "update_checkout",
        lambda *_args: calls.append("checkout_update") or {},
    )
    monkeypatch.setattr(
        gopay_core,
        "create_checkout",
        lambda *_args, **_kwargs: {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "ID",
            "currency": "IDR",
            "checkout_state": {
                "currency": "IDR",
                "total": {"total": {"minorUnitsAmount": 34900000}},
            },
        },
    )
    monkeypatch.setattr(gopay_core, "require_country_currency", lambda *_args: None)
    def fake_provider(*_args, **kwargs):
        callback = kwargs["stage_callback"]
        for stage in (
            "elements_session",
            "taxes",
            "payment_confirmation",
            "redirect_resolution",
        ):
            callback(stage)
        return {
            "provider_url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "gopay_url": "https://app.midtrans.com/snap/v4/redirection/fixture",
        }

    monkeypatch.setattr(gopay_core, "extract_cs_live_provider", fake_provider)
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
        apply_checkout_update=True,
        gopay_zero_trial_validation=False,
    )
    stages: list[str] = []
    result = gopay_core.extract_gopay_payment_link(
        config, transport_factory=factory, stage_callback=stages.append
    )
    assert result.payment_method == "gopay"
    assert result.provider_field == "gopay_url"
    assert result.provider_value.endswith("/fixture")
    assert result.amount_due_minor == 34900000
    assert result.extra["gopay_zero_trial_validation"] is False
    assert calls == ["checkout_update"]
    assert stages == [
        "checkout",
        "checkout_kind:stripe_checkout",
        "checkout_update",
        "promotion_applied",
        "stripe_init",
        "elements_session",
        "taxes",
        "payment_confirmation",
        "redirect_resolution",
        "completed",
    ]
    assert "eligibility_check" not in stages
    assert "eligibility_confirmed" not in stages
    assert "zero_amount_validation" not in stages
    assert "zero_amount_confirmed" not in stages
    assert stages[-1] == "completed"
    assert factory.chatgpt_calls == 1 and factory.stripe_calls == 1


def test_gopay_core_uses_promotion_update_before_provider(monkeypatch) -> None:
    class Session:
        def close(self) -> None:
            return None

    class Factory:
        def chatgpt(self, _config, _proxy):
            return Session()

        def stripe(self, _config):
            return Session()

    monkeypatch.setattr(gopay_core, "check_coupon_eligibility", lambda *_args: {"state": "eligible"})
    monkeypatch.setattr(gopay_core, "account_email", lambda _token: "account@example.com")
    monkeypatch.setattr(gopay_core, "update_checkout", lambda *_args: {})
    monkeypatch.setattr(
        gopay_core,
        "create_checkout",
        lambda *_args, **_kwargs: {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "ID",
            "currency": "IDR",
            "checkout_state": {"currency": "IDR", "total": {"total": {"minorUnitsAmount": 0}}},
        },
    )
    monkeypatch.setattr(gopay_core, "require_country_currency", lambda *_args: None)
    captured_billing: dict[str, str] = {}

    def provider(*args, **_kwargs):
        captured_billing.update(args[4])
        return {
            "provider_url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            "gopay_url": "https://app.midtrans.com/snap/v4/redirection/fixture",
        }

    monkeypatch.setattr(gopay_core, "extract_cs_live_provider", provider)
    stages: list[str] = []
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
        apply_checkout_update=True,
    )
    result = gopay_core.extract_gopay_payment_link(
        config, transport_factory=Factory(), stage_callback=stages.append
    )
    assert stages[:6] == [
        "eligibility_check",
        "eligibility_confirmed",
        "checkout",
        "checkout_kind:stripe_checkout",
        "checkout_update",
        "promotion_applied",
    ]
    assert stages[-3:] == [
        "zero_amount_validation",
        "zero_amount_confirmed",
        "completed",
    ]
    assert result.extra["gopay_zero_trial_validation"] is True
    assert captured_billing["email"] == "account@example.com"
    assert result.billing.email == "account@example.com"


def test_gopay_core_pins_eligibility_and_provider_to_one_attempt_proxy(monkeypatch) -> None:
    selected = "http://selected-id-proxy.example:8080"
    observed: list[tuple[str, str, str]] = []

    class Session:
        def close(self) -> None:
            return None

    class Factory:
        def chatgpt(self, config, proxy):
            observed.append(("chatgpt", config.update_proxy, proxy))
            return Session()

        def stripe(self, config):
            observed.append(("stripe", config.update_proxy, config.checkout_proxy))
            return Session()

    def observe(name, result):
        def call(config, *_args, **_kwargs):
            observed.append((name, config.checkout_proxy, config.update_proxy))
            return result

        return call

    checkout = {
        "cs_id": "cs_fixture",
        "session_kind": "stripe_checkout",
        "billing_country": "ID",
        "currency": "IDR",
        "checkout_state": {"currency": "IDR", "total": {"total": {"minorUnitsAmount": 0}}},
    }
    monkeypatch.setattr(gopay_core, "check_coupon_eligibility", observe("eligibility", {"state": "eligible"}))
    monkeypatch.setattr(gopay_core, "create_checkout", observe("checkout", checkout))
    monkeypatch.setattr(gopay_core, "update_checkout", observe("update", {}))
    monkeypatch.setattr(gopay_core, "require_country_currency", lambda *_args: None)
    monkeypatch.setattr(
        gopay_core,
        "extract_cs_live_provider",
        observe(
            "provider",
            {
                "provider_url": "https://app.midtrans.com/snap/v4/redirection/fixture",
                "gopay_url": "https://app.midtrans.com/snap/v4/redirection/fixture",
            },
        ),
    )
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy=selected,
        update_proxy="http://must-not-be-used.example:8080",
        country="ID",
        payment_method="gopay",
        proxy_pool=(selected, "http://future-retry-only.example:8080"),
    )
    result = gopay_core.extract_gopay_payment_link(config, transport_factory=Factory())
    assert result.provider_value.endswith("/fixture")
    assert observed
    assert all(left == selected and right == selected for _, left, right in observed)


def test_gopay_batch_validation_keeps_failure_modes_separate() -> None:
    report = validate_checkout_batch(
        [
            {"status_code": 200, "payload": {"checkout_session_id": "oaics_one", "payment_method_types": ["gopay"]}},
            {"status_code": 200, "payload": {"checkout_session": {"id": "cs_two", "payment_method_types": ["gopay", "card"]}}},
            {"status_code": 429, "payload": '{"detail":"Too many requests"}'},
        ]
    )
    assert report["success_count"] == 2
    assert report["session_kinds"] == {
        "openai_custom_checkout": 1,
        "stripe_checkout": 1,
    }
    assert report["failure_modes"] == {"rate_limited": 1}
    assert report["payment_methods"] == ["gopay", "card"]


def test_gopay_promo_eligibility_error_retries_except_at_401() -> None:
    eligibility = gopay_checkout.PromoEligibilityError(
        409,
        "promo eligibility rejected: state=not_eligible",
        failure_mode="promo_not_eligible",
        retryable=True,
    )
    assert eligibility.retryable is True
    assert eligibility.failure_mode == "promo_not_eligible"
    auth = gopay_checkout.PromoEligibilityError(
        401,
        "invalid token",
        failure_mode="access_token_invalid",
        retryable=False,
    )
    assert auth.retryable is False


def test_gopay_checkout_failures_are_retryable_except_401() -> None:
    assert gopay_checkout.classify_checkout_create_failure(401, "unauthorized") == (
        "access_token_invalid",
        False,
    )
    assert gopay_checkout.classify_checkout_create_failure(403, "access denied") == (
        "access_denied",
        True,
    )
    assert gopay_checkout.classify_checkout_create_failure(409, "payment method unavailable") == (
        "payment_method_unavailable",
        True,
    )


def test_gopay_proxy_pool_is_randomized_once_per_task() -> None:
    pool = tuple(f"proxy-{index}" for index in range(10))
    plan = __import__("payment_link_extractor.web.tasks", fromlist=["TaskManager"]).TaskManager._random_proxy_plan(pool, 6)
    assert len(plan) == 6
    assert len(set(plan)) == 6
    assert set(plan).issubset(set(pool))
