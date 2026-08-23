from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from payment_link_extractor.application import _normalize_config
from payment_link_extractor.config import billing_for_country, country_for_payment_method
from payment_link_extractor.flows.oaics import extract_oaics_provider
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.web.app import create_app
from payment_link_extractor.web.routes import _config_from_payload


ROOT = Path(__file__).resolve().parents[1]


def test_gcash_forces_ph_country_in_route_and_worker_config() -> None:
    payload = {
        "access_token": "token",
        "checkout_proxy": "http://proxy.example:8080",
        "update_proxy": "http://proxy.example:8081",
        "country": "US",
        "payment_method": "gcash",
        "apply_checkout_update": False,
    }
    route_config = _config_from_payload(payload)
    assert route_config.country == "PH"
    assert country_for_payment_method("gcash", "US") == "PH"

    normalized = _normalize_config(route_config)
    assert normalized.country == "PH"
    assert billing_for_country(normalized.country).country == "PH"


def test_defaults_expose_paypal_and_gcash_payment_choices() -> None:
    app = create_app({"TESTING": True})
    response = app.test_client().get("/api/defaults", headers={"X-Workbench-Password": "test-password"})
    assert response.status_code == 200
    data = response.get_json()
    assert [(item["value"], item["label"]) for item in data["payment_methods"]] == [
        ("paypal", "PayPal"),
        ("gcash", "GCash"),
    ]
    assert data["payment_method_countries"] == {"gcash": "PH"}


class _Response:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload


class _ChatGPT:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url, kwargs))
        if url.endswith("/payments/checkout/taxes"):
            return _Response(200, {"checkout_session": {"custom_payment_methods": [{"id": "cpmt_gcash"}]}})
        if "/payments/checkout/openai_ie/oaics_fixture" in url:
            return _Response(200, {"custom_payment_methods": [{"id": "cpmt_gcash", "name": "GCash"}]})
        if url.endswith("/payments/checkout/confirm"):
            return _Response(200, {"status": "success", "confirm_return_url": "https://chatgpt.com/checkout/verify"})
        if url.endswith("/payments/checkout/custom_payment_method/start"):
            return _Response(200, {"status": "requires_action", "next_action": {"url": "https://gcash.com/pay/fixture", "paymentMethodType": "gcash"}})
        raise AssertionError(f"unexpected request: {method} {url}")


def test_oaics_gcash_uses_custom_method_confirm_and_start() -> None:
    chatgpt = _ChatGPT()
    checkout = {
        "cs_id": "oaics_fixture",
        "session_kind": "openai_custom_checkout",
        "billing_country": "PH",
        "currency": "PHP",
        "processor_entity": "openai_ie",
        "custom_payment_methods": [{"id": "cpmt_gcash", "name": "GCash"}],
    }
    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="PH",
        payment_method="gcash",
        apply_checkout_update=False,
    )
    result = extract_oaics_provider(
        config,
        chatgpt,
        SimpleNamespace(),
        checkout,
        billing_for_country("PH").to_dict(),
        None,
    )
    assert result["gcash_url"] == "https://gcash.com/pay/fixture"
    assert result["payment_method_id"] == "cpmt_gcash"
    assert [call[1].rsplit("/backend-api", 1)[-1] for call in chatgpt.calls] == [
        "/payments/checkout/taxes",
        "/payments/checkout/openai_ie/oaics_fixture",
        "/payments/checkout/confirm",
        "/payments/checkout/custom_payment_method/start",
    ]


def test_extractor_ui_contains_gcash_fixed_country_behavior() -> None:
    html = (ROOT / "payment_link_extractor/web/templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "payment_link_extractor/web/static/app.js").read_text(encoding="utf-8")
    assert '<option value="gcash">GCash（菲律宾）</option>' in html
    assert 'id="country-field"' in html
    assert 'id="gcash-country-note"' in html
    assert 'country.value = "PH"' in javascript
    assert 'paymentMethod === "gcash" ? "PH"' in javascript
