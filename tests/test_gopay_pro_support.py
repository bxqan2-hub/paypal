from __future__ import annotations

import sys
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import payment_link_extractor.gopay_pro as adapter
from payment_link_extractor.application import _normalize_config, extract_payment_link
from payment_link_extractor.channels import PAYMENT_CHANNELS
from payment_link_extractor.models import ExtractionConfig


ROOT = Path(__file__).resolve().parents[1]


def gopay_pro_config() -> ExtractionConfig:
    return ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="US",
        payment_method="gopay_pro",
        apply_checkout_update=False,
        account_email="gopay@example.com",
        account_name="GoPay Pro User",
    )


def test_gopay_pro_has_independent_channel_and_idr_billing() -> None:
    channel = PAYMENT_CHANNELS["gopay_pro"]
    assert channel.adapter_module == "payment_link_extractor.gopay_pro"
    assert channel.adapter_callable == "extract_gopay_pro_payment_link"
    assert channel.result_field == "gopay_pro_url"
    assert (channel.country, channel.currency) == ("ID", "IDR")
    normalized = _normalize_config(gopay_pro_config())
    assert normalized.country == "ID"


def test_gopay_pro_core_is_a_distinct_copy_with_id_route() -> None:
    core = adapter.GOPAY_PRO_PROJECT_DIR
    assert core != ROOT / "payment_link_extractor" / "mk_gcash_open_source"
    assert (core / "gopay_pro_chain.py").is_file()
    assert (core / "gopay_pro_monitor.py").is_file()
    assert (core / "gopay_pro_sentinel.py").is_file()
    text = (core / "gopay_pro_chain.py").read_text(encoding="utf-8")
    assert '"billing_country": "ID"' in text
    assert '"currency": "IDR"' in text
    assert "_is_gopay_pro_url" in text


def test_gopay_pro_core_manifest_matches_its_copied_files() -> None:
    manifest = json.loads((ROOT / "gopay_pro_project_manifest.json").read_text(encoding="utf-8"))
    assert manifest["channel"] == "gopay_pro"
    assert manifest["tracked_file_count"] == 22
    for relative, expected in manifest["sha256"].items():
        assert hashlib.sha256((adapter.GOPAY_PRO_PROJECT_DIR / relative).read_bytes()).hexdigest() == expected


def test_gopay_pro_adapter_maps_core_result_to_own_field(monkeypatch) -> None:
    fake = SimpleNamespace()
    raw = {
        "client_account_id": "acct_fixture",
        "status": "success",
        "current_step": "follow_redirect",
        "checkout_session_id": "oaics_gopay_pro",
        "payment_method_id": "cpmt_gopay",
        "checkout_amount": 0,
        "gopay_pro_url": "https://gopay.co.id/pay/fixture",
        "payment_route": "direct_gopay_pro",
    }
    fake.CHAIN_MANAGER = SimpleNamespace(get_tasks=lambda _job: [raw])
    fake.create_job = lambda _payload: {"job_id": "local_fixture", "accounts": [{"id": "acct_fixture"}]}
    fake.public_job = lambda _job: {
        "accounts": [{
            "id": "acct_fixture",
            "status": "success",
            "current_step": "follow_redirect",
            "link": raw["gopay_pro_url"],
        }]
    }
    fake.cancel_job = lambda _job: None
    monkeypatch.setattr(adapter, "_APP", fake)
    monkeypatch.setitem(sys.modules, "gopay_pro_chain", SimpleNamespace(_is_gopay_pro_url=lambda value: value.startswith("https://gopay.co.id/")))
    result = adapter.extract_gopay_pro_payment_link(gopay_pro_config())
    assert result.payment_method == "gopay_pro"
    assert result.billing_country == "ID"
    assert result.currency == "IDR"
    assert result.provider_field == "gopay_pro_url"
    assert result.provider_value == raw["gopay_pro_url"]


def test_gopay_pro_dispatch_does_not_construct_legacy_paypal_transport(monkeypatch) -> None:
    expected = object()
    monkeypatch.setattr(adapter, "extract_gopay_pro_payment_link", lambda config, **_kwargs: expected)

    class ForbiddenFactory:
        def chatgpt(self, *_args, **_kwargs):
            raise AssertionError("GoPay Pro should use its copied core")

    assert extract_payment_link(gopay_pro_config(), transport_factory=ForbiddenFactory()) is expected
