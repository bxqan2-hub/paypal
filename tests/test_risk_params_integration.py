from __future__ import annotations

import re
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from payment_link_extractor import risk_params, stripe_common
from payment_link_extractor.flows import cs_live, oaics
from payment_link_extractor.models import ExtractionConfig


class _JsonResponse(SimpleNamespace):
    def json(self) -> dict[str, Any]:
        return self.payload


def _response(payload: dict[str, Any]) -> _JsonResponse:
    return _JsonResponse(status_code=200, text="", payload=payload)


@pytest.mark.parametrize(
    ("factory", "prefix"),
    (
        (risk_params.stripe_guid, "guid_"),
        (risk_params.stripe_muid, "muid_"),
        (risk_params.stripe_sid, "sid_"),
    ),
)
def test_machine_risk_ids_have_expected_prefix_alphabet_and_length(factory, prefix: str) -> None:
    values = {factory() for _ in range(40)}

    assert len(values) == 40
    for value in values:
        assert re.fullmatch(rf"{prefix}[A-Za-z0-9_-]{{29,33}}", value)


def test_stripe_js_id_is_a_uuid() -> None:
    value = risk_params.stripe_js_id()

    assert str(uuid.UUID(value)) == value


def test_time_on_page_is_midpoint_weighted_and_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[int, int, float]] = []

    def fake_triangular(lo: int, hi: int, mode: float) -> float:
        observed.append((lo, hi, mode))
        return hi + 100

    monkeypatch.setattr(risk_params.random, "triangular", fake_triangular)

    assert risk_params.time_on_page_ms(25_000, 55_000) == 55_000
    assert observed == [(25_000, 55_000, 40_000.0)]
    assert risk_params.time_on_page_ms(12, 12) == 12


def test_stripe_context_generates_one_consistent_risk_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stripe_common, "new_stripe_js_id", lambda: "js-fixture")
    monkeypatch.setattr(stripe_common, "stripe_guid", lambda: "guid_fixture")
    monkeypatch.setattr(stripe_common, "stripe_muid", lambda: "muid_fixture")
    monkeypatch.setattr(stripe_common, "stripe_sid", lambda: "sid_fixture")

    ctx = stripe_common.stripe_context({}, {"currency": "GBP"})

    assert {key: ctx[key] for key in ("stripe_js_id", "guid", "muid", "sid")} == {
        "stripe_js_id": "js-fixture",
        "guid": "guid_fixture",
        "muid": "muid_fixture",
        "sid": "sid_fixture",
    }


def test_cs_init_uses_risk_helper_for_stripe_js_id(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(cs_live, "new_stripe_js_id", lambda: "js-from-helper")

    def fake_request(*args, **kwargs):
        captured.update(kwargs["data"])
        return _response({"id": "cs_fixture"})

    monkeypatch.setattr(cs_live, "stage_http_request", fake_request)
    config = ExtractionConfig(access_token="fixture", checkout_proxy="", update_proxy="")

    _, stripe_js_id = cs_live.stripe_init(config, {"cs_id": "cs_fixture"}, None, object())

    assert stripe_js_id == "js-from-helper"
    assert captured["elements_session_client[stripe_js_id]"] == "js-from-helper"


def test_oaics_confirmation_reuses_context_risk_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(oaics, "time_on_page_ms", lambda lo, hi: 65_432)

    def fake_request(*args, **kwargs):
        captured.update(kwargs["data"])
        return _response({"id": "ctoken_fixture"})

    monkeypatch.setattr(oaics, "stage_http_request", fake_request)
    config = ExtractionConfig(access_token="fixture", checkout_proxy="", update_proxy="")
    ctx = {
        "stripe_js_id": "js_fixture",
        "guid": "guid_fixture",
        "muid": "muid_fixture",
        "sid": "sid_fixture",
        "elements_session_id": "elements_fixture",
        "elements_session_config_id": "config_fixture",
    }
    billing = {
        "name": "Test User",
        "phone": "+440000000000",
        "line1": "1 Test Street",
        "city": "London",
        "postal_code": "SW1A 1AA",
        "state": "",
    }

    token = oaics.openai_confirmation_token(
        object(), config, {"publishable_key": "pk_test"}, billing, ctx, "paypal", None
    )

    assert token == "ctoken_fixture"
    assert captured["payment_method_data[time_on_page]"] == "65432"
    assert captured["payment_method_data[guid]"] == "guid_fixture"
    assert captured["payment_method_data[muid]"] == "muid_fixture"
    assert captured["payment_method_data[sid]"] == "sid_fixture"


def test_cs_confirm_reuses_context_risk_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}
    monkeypatch.setattr(cs_live, "time_on_page_ms", lambda lo, hi: 91_234)

    def fake_request(*args, **kwargs):
        captured.update(kwargs["data"])
        return _response({"status": "requires_action"})

    monkeypatch.setattr(cs_live, "stage_http_request", fake_request)
    ctx = {
        "stripe_js_id": "js_fixture",
        "guid": "guid_fixture",
        "muid": "muid_fixture",
        "sid": "sid_fixture",
        "elements_session_id": "elements_fixture",
        "elements_session_config_id": "config_fixture",
        "config_id": "config_fixture",
        "checkout_amount": "2000",
    }
    billing = {
        "name": "Test User",
        "email": "test@example.com",
        "line1": "1 Test Street",
        "city": "London",
        "postal_code": "SW1A 1AA",
        "country": "GB",
        "state": "",
    }

    result = cs_live.stripe_confirm_cs_live(
        object(),
        {"cs_id": "cs_fixture", "publishable_key": "pk_test"},
        {},
        ctx,
        "https://checkout.stripe.com/c/pay/cs_fixture",
        "paypal",
        billing,
        None,
    )

    assert result == {"status": "requires_action"}
    assert captured["guid"] == "guid_fixture"
    assert captured["muid"] == "muid_fixture"
    assert captured["sid"] == "sid_fixture"
    assert captured["payment_method_data[time_on_page]"] == "91234"
