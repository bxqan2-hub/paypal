from __future__ import annotations

from payment_link_extractor.application import (
    _normalize_config,
    _should_apply_checkout_update,
)
from payment_link_extractor.channels import PAYMENT_CHANNELS
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.web.app import create_app
from payment_link_extractor.web.routes import _config_from_payload


def gopay_config() -> ExtractionConfig:
    return ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://id-user:id-pass@proxy.example:8080",
        update_proxy="http://id-user:id-pass@proxy.example:8080",
        country="US",
        payment_method="gopay",
        apply_checkout_update=True,
    )


def test_gopay_uses_dedicated_core_and_fixed_indonesia_billing() -> None:
    channel = PAYMENT_CHANNELS["gopay"]
    assert channel.adapter_module == "payment_link_extractor.gopay_channel"
    assert channel.adapter_callable == "extract_gopay_payment_link"
    assert channel.result_field == "gopay_url"
    assert channel.country == "ID"
    assert channel.currency == "IDR"
    assert channel.uses_legacy_transport is False
    assert channel.uses_checkout_update is True

    normalized = _normalize_config(gopay_config())
    assert normalized.country == "ID"
    assert normalized.payment_method == "gopay"
    assert _should_apply_checkout_update(normalized) is True


def test_gopay_route_normalization_forces_indonesia() -> None:
    route_config = _config_from_payload(
        {
            "access_token": "fixture-token",
            "checkout_proxy": "http://proxy.example:8080",
            "update_proxy": "http://proxy.example:8080",
            "country": "US",
            "payment_method": "gopay",
            "apply_checkout_update": True,
        }
    )
    assert route_config.country == "ID"
    assert route_config.payment_method == "gopay"


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


def test_gopay_adapter_does_not_import_paypal_core():
    source = __import__("pathlib").Path(__file__).resolve().parents[1] / "payment_link_extractor/gopay_channel.py"
    assert "paypal_channel" not in source.read_text(encoding="utf-8")
    assert "gopay_pro_core" in source.read_text(encoding="utf-8")
