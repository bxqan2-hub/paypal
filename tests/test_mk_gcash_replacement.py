from __future__ import annotations

import hashlib
import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from payment_link_extractor.application import extract_payment_link
from payment_link_extractor.errors import ExtractionCancelled, ProtocolError
from payment_link_extractor.mk_gcash import extract_mk_gcash_payment_link
from payment_link_extractor.models import ExtractionConfig


ROOT = Path(__file__).resolve().parents[1]


def jwt(payload: dict) -> str:
    encode = lambda value: base64.urlsafe_b64encode(
        json.dumps(value, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return f"{encode({'alg': 'none'})}.{encode(payload)}.signature"


def gcash_config() -> ExtractionConfig:
    return ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="proxy.example:8080:user:pass",
        update_proxy="",
        country="GB",
        payment_method="gcash",
    )


def test_vendored_mk_gcash_core_matches_upstream_manifest():
    manifest = json.loads((ROOT / "mk_gcash_core_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit"] == "2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5"
    for relative, expected in manifest["sha256"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected


def test_application_dispatches_gcash_before_legacy_transport(monkeypatch):
    expected = object()
    called = {}

    def direct(config, **kwargs):
        called["config"] = config
        return expected

    monkeypatch.setattr("payment_link_extractor.mk_gcash.extract_mk_gcash_payment_link", direct)

    class ForbiddenFactory:
        def chatgpt(self, *_args, **_kwargs):
            raise AssertionError("legacy GCash transport was constructed")

    assert extract_payment_link(gcash_config(), transport_factory=ForbiddenFactory()) is expected
    assert called["config"].payment_method == "gcash"
    assert called["config"].country == "PH"


def test_adapter_maps_mk_core_success(monkeypatch):
    stages = []

    class FakeChain:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._session = SimpleNamespace(curl_options={})
            self.cid = "oaics_fixture"
            self.cpmt = "cpmt_fixture"
            self.adyen_url = "https://checkoutshopper-live.adyen.com/fixture"
            self.checkout_amount = 0

        def run(self):
            for step in (
                "proxy_test", "create_checkout", "configure_taxes",
                "confirm_payment", "start_payment", "follow_redirect",
            ):
                self.kwargs["on_update"]({"current_step": step})
            return {
                "status": "success",
                "gcash_url": "https://m.gcash.com/gcash-login-web/index.html?netAuthId=fixture",
                "payment_route": "adyen_redirect",
                "qr_text": "qr-fixture",
                "qr_short": "https://short.fixture",
                "net_auth_id": "fixture",
                "qr_expires_at": 123,
                "monitor_id": "monitor-fixture",
                "callback_status": "waiting_scan",
            }

    monkeypatch.setattr("payment_link_extractor.mk_gcash.GCashChain", FakeChain)
    result = extract_mk_gcash_payment_link(gcash_config(), stage_callback=stages.append)
    assert result.checkout_session_id == "oaics_fixture"
    assert result.payment_method_id == "cpmt_fixture"
    assert result.billing_country == "PH"
    assert result.currency == "PHP"
    assert result.provider_value.startswith("https://m.gcash.com/")
    assert result.extra["mk_gcash_source_commit"] == "2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5"
    assert result.extra["connect_timeout_ms"] == 8000
    assert set(result.extra["stage_offsets_ms"]) == {
        "proxy_test", "create_checkout", "configure_taxes",
        "confirm_payment", "start_payment", "follow_redirect",
    }
    assert stages == [
        "eligibility_check", "checkout", "taxes", "payment_confirmation",
        "redirect_resolution", "completed",
    ]


@pytest.mark.parametrize(
    ("result", "cancelled", "exception"),
    [
        ({"status": "failed", "error_message": "upstream failed"}, False, ProtocolError),
        ({"status": "failed", "error_message": "TASK_CANCELLED: stopped"}, True, ExtractionCancelled),
    ],
)
def test_adapter_propagates_failures(monkeypatch, result, cancelled, exception):
    class FakeChain:
        cid = cpmt = adyen_url = None
        checkout_amount = 0

        def __init__(self, **_kwargs):
            self._session = SimpleNamespace(curl_options={})

        def run(self):
            return result

    monkeypatch.setattr("payment_link_extractor.mk_gcash.GCashChain", FakeChain)
    event = SimpleNamespace(is_set=lambda: cancelled)
    with pytest.raises(exception):
        extract_mk_gcash_payment_link(gcash_config(), cancel_event=event)


def test_adapter_uses_upstream_account_billing_instead_of_site_fixture(monkeypatch):
    captured = {}

    class FakeChain:
        cid = "oaics_fixture"
        cpmt = "cpmt_fixture"
        adyen_url = "https://checkoutshopper-live.adyen.com/fixture"
        checkout_amount = 0

        def __init__(self, **kwargs):
            captured.update(kwargs)
            self._session = SimpleNamespace(curl_options={})

        def run(self):
            return {
                "status": "success",
                "gcash_url": "https://m.gcash.com/gcash-login-web/index.html?netAuthId=fixture",
            }

    monkeypatch.setattr("payment_link_extractor.mk_gcash.GCashChain", FakeChain)
    config = ExtractionConfig(
        access_token=jwt({
            "https://api.openai.com/profile": {
                "email": "jwt@example.com",
                "name": "JWT Name",
            },
            "https://api.openai.com/auth": {"chatgpt_account_id": "acct-jwt"},
        }),
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        payment_method="gcash",
        account_email="explicit@example.com",
        account_name="Explicit Name",
    )
    result = extract_mk_gcash_payment_link(config)

    assert captured["account_id"] == "acct-jwt"
    assert captured["billing_email"] == "explicit@example.com"
    assert captured["billing_name"] == "Explicit Name"
    assert captured["client_account_id"].startswith("acct_")
    assert result.billing.to_dict() == {
        "name": "Explicit Name",
        "email": "explicit@example.com",
        "phone": "",
        "country": "PH",
        "line1": "",
        "city": "",
        "state": "",
        "postal_code": "",
    }


def test_adapter_rejects_expired_token_before_starting_core(monkeypatch):
    monkeypatch.setattr(
        "payment_link_extractor.mk_gcash.GCashChain",
        lambda **_kwargs: pytest.fail("expired account reached core"),
    )
    config = ExtractionConfig(
        access_token=jwt({"exp": 1}),
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        payment_method="gcash",
    )
    with pytest.raises(ProtocolError, match="AT 已过期"):
        extract_mk_gcash_payment_link(config)
