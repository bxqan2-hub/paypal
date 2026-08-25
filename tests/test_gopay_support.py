from __future__ import annotations

from types import SimpleNamespace

from payment_link_extractor.application import (
    _normalize_config,
    _should_apply_checkout_update,
    extract_payment_link,
)
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.web.app import create_app
from payment_link_extractor.web.routes import _config_from_payload
import payment_link_extractor.mk_gopay as mk


def gopay_config() -> ExtractionConfig:
    return ExtractionConfig(
        access_token="fixture-token",
        session_token="session-fixture",
        checkout_proxy="http://id-user:id-pass@proxy.example:8080",
        update_proxy="",
        country="US",
        payment_method="gopay",
        apply_checkout_update=True,
    )


def test_gopay_forces_indonesia_and_skips_legacy_update() -> None:
    normalized = _normalize_config(gopay_config())
    assert normalized.country == "ID"
    assert normalized.session_token == "session-fixture"
    assert _should_apply_checkout_update(normalized) is False
    route_config = _config_from_payload(
        {
            "access_token": "fixture-token",
            "__Secure-next-auth.session-token": "session-fixture",
            "checkout_proxy": "http://proxy.example:8080",
            "country": "US",
            "payment_method": "gopay",
            "apply_checkout_update": True,
        }
    )
    assert route_config.country == "ID"
    assert route_config.session_token == "session-fixture"


def test_defaults_expose_gopay_choice() -> None:
    app = create_app({"TESTING": True})
    response = app.test_client().get(
        "/api/defaults", headers={"X-Workbench-Password": "test-password"}
    )
    assert response.status_code == 200
    data = response.get_json()
    methods = {item["value"]: item for item in data["payment_methods"]}
    assert methods["gopay"]["label"] == "GoPay"
    assert data["payment_method_countries"]["gopay"] == "ID"


def test_application_dispatches_gopay_before_legacy_transport(monkeypatch) -> None:
    expected = object()
    called = {}

    def direct(config, **_kwargs):
        called["config"] = config
        return expected

    monkeypatch.setattr(mk, "extract_mk_gopay_payment_link", direct)

    class ForbiddenFactory:
        def chatgpt(self, *_args, **_kwargs):
            raise AssertionError("legacy GoPay transport was constructed")

    assert extract_payment_link(gopay_config(), transport_factory=ForbiddenFactory()) is expected
    assert called["config"].country == "ID"


def test_direct_upstream_gopay_result_mapping(monkeypatch) -> None:
    fake = SimpleNamespace(
        indonesia_billing_profile=lambda email: {
            "name": "Fixture User",
            "email": email,
            "line1": "Jl Fixture",
            "city": "Jakarta",
            "state": "DKI Jakarta",
            "postal_code": "10220",
        },
        run_gopay_flow=lambda access, session, proxy, billing: (
            "https://pm-redirects.stripe.com/authorize/acct_fixture/nonce_fixture"
        ),
    )
    monkeypatch.setattr(mk, "_UPSTREAM_GOPAY", fake)
    stages = []
    result = mk.extract_mk_gopay_payment_link(gopay_config(), stage_callback=stages.append)
    assert result.payment_method == "gopay"
    assert result.provider_value.startswith("https://pm-redirects.stripe.com/authorize/")
    assert result.billing_country == "ID"
    assert result.extra["mk_gopay_source_commit"] == mk.MK_GOPAY_SOURCE_COMMIT
    assert stages == ["checkout", "redirect_resolution", "completed"]
