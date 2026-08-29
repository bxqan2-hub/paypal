from __future__ import annotations

from pathlib import Path

from payment_link_extractor.channels import PAYMENT_CHANNELS, public_payment_channels
from payment_link_extractor.cli import SUPPORTED_PAYMENT_METHODS


ROOT = Path(__file__).resolve().parents[1]


def test_all_channels_have_unique_adapters_and_result_fields() -> None:
    channels = list(PAYMENT_CHANNELS.values())
    assert tuple(channel.name for channel in channels) == ("paypal", "gopay", "gopay_pro", "gcash")
    assert len({channel.adapter_module for channel in channels}) == len(channels)
    assert len({channel.result_field for channel in channels}) == len(channels)
    assert SUPPORTED_PAYMENT_METHODS == tuple(PAYMENT_CHANNELS)


def test_channel_country_currency_and_transport_contracts_are_isolated() -> None:
    paypal = PAYMENT_CHANNELS["paypal"]
    gopay = PAYMENT_CHANNELS["gopay"]
    gopay_pro = PAYMENT_CHANNELS["gopay_pro"]
    gcash = PAYMENT_CHANNELS["gcash"]
    assert (paypal.country, paypal.currency, paypal.uses_legacy_transport) == ("", "", True)
    assert (gopay.country, gopay.currency, gopay.uses_legacy_transport) == ("ID", "IDR", True)
    assert (gopay_pro.country, gopay_pro.currency, gopay_pro.uses_legacy_transport) == ("ID", "IDR", False)
    assert (gcash.country, gcash.currency, gcash.uses_legacy_transport) == ("PH", "PHP", False)
    assert paypal.uses_checkout_update is True
    assert gopay.uses_checkout_update is True
    assert gopay_pro.uses_checkout_update is False
    assert gcash.uses_checkout_update is False


def test_ui_options_match_the_channel_registry() -> None:
    html = (ROOT / "payment_link_extractor/web/templates/index.html").read_text(encoding="utf-8")
    for item in public_payment_channels():
        assert f'value="{item["value"]}"' in html
