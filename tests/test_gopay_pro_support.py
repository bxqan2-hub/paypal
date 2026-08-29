from __future__ import annotations

from types import SimpleNamespace

import payment_link_extractor.gopay_pro as gopay_pro
from payment_link_extractor.application import _normalize_config, extract_payment_link
from payment_link_extractor.channels import PAYMENT_CHANNELS
from payment_link_extractor.models import BillingProfile, ExtractionConfig, PaymentLinkResult
from payment_link_extractor.providers import provider_redirect_config


def config() -> ExtractionConfig:
    return ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="US",
        payment_method="gopay_pro",
        apply_checkout_update=True,
    )


def _result() -> PaymentLinkResult:
    return PaymentLinkResult(
        checkout_session_id="cs_gopay_pro",
        session_kind="stripe_checkout",
        payment_method="gopay_pro",
        billing_country="ID",
        currency="IDR",
        amount_due=0,
        amount_due_minor=0,
        billing=BillingProfile("GoPay Pro User", "user@example.com", "", "ID", "", "", "", ""),
        provider_url="https://pm-redirects.stripe.com/authorize/gopay-pro",
        provider_field="gopay_pro_url",
        provider_value="https://pm-redirects.stripe.com/authorize/gopay-pro",
    )


def test_gopay_pro_is_independent_and_fixed_to_id_idr() -> None:
    channel = PAYMENT_CHANNELS["gopay_pro"]
    assert channel.adapter_module == "payment_link_extractor.gopay_pro"
    assert channel.adapter_callable == "extract_gopay_pro_payment_link"
    assert channel.result_field == "gopay_pro_url"
    assert (channel.country, channel.currency) == ("ID", "IDR")
    assert channel.uses_legacy_transport is True
    assert channel.uses_checkout_update is True
    assert _normalize_config(config()).country == "ID"


def test_gopay_pro_provider_hosts_are_separate() -> None:
    redirect = provider_redirect_config("gopay_pro")
    assert redirect["result_field"] == "gopay_pro_url"
    assert "pm-redirects.stripe.com" in redirect["preferred_hosts"]


def test_gopay_pro_delegates_to_paypal_base_and_adds_gcash_optimization(monkeypatch) -> None:
    base_calls = {}

    def fake_base(config, **kwargs):
        base_calls["config"] = config
        base_calls["kwargs"] = kwargs
        return _result()

    monkeypatch.setattr(gopay_pro, "extract_legacy_payment_link", fake_base)
    result = gopay_pro.extract_gopay_pro_payment_link(config())
    assert base_calls["config"].payment_method == "gopay_pro"
    assert result.extra["gopay_pro_core"] == "paypal_legacy_checkout_stripe"
    assert result.extra["gopay_pro_optimization"] == "gcash_browser_sentinel_sdk"
    assert result.extra["payment_route"] == "gopay_pro_paypal_base"


def test_application_dispatches_gopay_pro_to_its_adapter(monkeypatch) -> None:
    expected = object()
    monkeypatch.setattr(gopay_pro, "extract_gopay_pro_payment_link", lambda config, **kwargs: expected)
    assert extract_payment_link(config()) is expected
