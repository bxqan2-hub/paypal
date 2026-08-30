from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from payment_link_extractor import gopay_checkout
from payment_link_extractor import gopay_core
from payment_link_extractor import gopay_cs_live
from payment_link_extractor import gopay_transport
from payment_link_extractor.gopay_validation import validate_checkout_batch
from payment_link_extractor.models import ExtractionConfig


ROOT = Path(__file__).resolve().parents[1]


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
    assert session.headers["oai-language"] == "id-ID"
    assert session.headers["oai-client-build-number"] == "10012890"
    assert session.headers["oai-client-version"] == "prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4"
    telemetry = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout"
    )["oai-telemetry"]
    values = json.loads(telemetry)
    assert len(values) == 8
    assert values[0] == 1 and values[5:] == [2, 0, values[7]]
    approve_telemetry = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout/approve"
    )["oai-telemetry"]
    approve_values = json.loads(approve_telemetry)
    assert len(approve_values) == 8
    assert approve_values[0] == 1 and approve_values[5:] == [2, 0, approve_values[7]]


def test_gopay_transport_propagates_har_pending_update_receipt() -> None:
    class Session:
        def __init__(self) -> None:
            self.headers = {"x-oai-is-pending-updates": '{"v":3,"updates":[]}'}

        def request(self, *_args, **_kwargs):
            class Response:
                status_code = 200
                text = "{}"
                headers = {"x-oai-is-update": "ois1.fixture"}

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
                    {"responseHeaders": {"x-oai-is-update": "ois1.browser-fixture"}}
                ]
            },
        },
    )
    provider._sync_pending_update_from_browser()
    assert json.loads(provider.transport_session.headers["x-oai-is-pending-updates"]) == {
        "v": 3,
        "updates": ["ois1.browser-fixture"],
    }


def test_gopay_promo_probe_does_not_create_browser_checkout_state(monkeypatch) -> None:
    calls: list[str] = []

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

    monkeypatch.setattr(gopay_checkout, "stage_http_request", lambda *_args, **_kwargs: Response())
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="ID",
        payment_method="gopay",
    )
    result = gopay_checkout.check_coupon_eligibility(config, Session(), None)
    assert result["state"] == "eligible"
    assert calls == []


def test_gopay_checkout_starts_from_promo_context(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class Provider:
        def headers(self, _flow, **_kwargs):
            return {"OpenAI-Sentinel-Token": "proof-fixture"}

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
    checkout = gopay_checkout.create_checkout(config, Session(), None)
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


def test_gopay_required_sentinel_proof_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="browser Sentinel provider is required"):
        gopay_transport.openai_sentinel_headers(
            SimpleNamespace(), flow="chatgpt_checkout", required=True
        )


def test_gopay_sentinel_init_script_uses_dedicated_assets() -> None:
    script = gopay_transport.BrowserSentinelProvider._build_sentinel_init_script()
    assert "window.SentinelSDK = SentinelSDK" in script
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

    flows: list[str] = []

    class Provider:
        def headers(self, flow, **_kwargs):
            flows.append(flow)
            return {"OpenAI-Sentinel-Token": "proof-fixture"}

    class Session:
        openai_sentinel_provider = Provider()

    monkeypatch.setattr(
        gopay_cs_live,
        "stage_http_request",
        lambda *_args, **_kwargs: Response(),
    )
    gopay_cs_live.chatgpt_approve(
        Session(),
        {"cs_id": "cs_live_fixture", "billing_country": "ID"},
        None,
    )
    assert flows == ["checkout_session_approval"]


def test_gopay_tax_region_matches_har_progressive_sequence(monkeypatch) -> None:
    class Response:
        status_code = 200
        text = "{}"
        headers: dict[str, str] = {}

        def json(self):
            return {"total_summary": {"total": 0}}

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
        lambda *_args: {
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
    monkeypatch.setattr(gopay_core, "update_checkout", lambda *_args: {})
    monkeypatch.setattr(
        gopay_core,
        "create_checkout",
        lambda *_args: {
            "cs_id": "cs_fixture",
            "session_kind": "stripe_checkout",
            "billing_country": "ID",
            "currency": "IDR",
            "checkout_state": {"currency": "IDR", "total": {"total": {"minorUnitsAmount": 0}}},
        },
    )
    monkeypatch.setattr(gopay_core, "require_country_currency", lambda *_args: None)
    monkeypatch.setattr(
        gopay_core,
        "extract_cs_live_provider",
        lambda *_args, **_kwargs: {"provider_url": "https://app.midtrans.com/snap/v4/redirection/fixture", "gopay_url": "https://app.midtrans.com/snap/v4/redirection/fixture"},
    )
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
