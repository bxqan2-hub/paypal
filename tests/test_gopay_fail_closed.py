from __future__ import annotations

from types import SimpleNamespace

import pytest

from payment_link_extractor import gopay_cs_live, gopay_stripe_common
from payment_link_extractor.errors import ProtocolError


def _checkout() -> dict[str, str]:
    return {
        "cs_id": "cs_fixture",
        "billing_country": "ID",
        "processor_entity": "openai_llc",
        "publishable_key": "pk_live_fixture",
    }


def _billing() -> dict[str, str]:
    return {
        "name": "Fixture",
        "email": "fixture@example.invalid",
        "country": "ID",
        "line1": "Street",
        "city": "Jakarta",
        "state": "Jawa",
        "postal_code": "10000",
    }


def _context() -> dict[str, str]:
    return {
        "stripe_js_id": "js_fixture",
        "elements_session_id": "elements_fixture",
        "locale": "id",
        "browser_timezone": "Asia/Jakarta",
        "currency": "idr",
        "checkout_amount": "0",
    }


class _Response:
    def __init__(self, status_code: int, text: str = "{}"):
        self.status_code = status_code
        self.text = text
        self.headers: dict[str, str] = {}

    def json(self):
        return {}


def test_gopay_elements_http_failure_is_explicit_and_retryable(monkeypatch) -> None:
    monkeypatch.setattr(
        gopay_cs_live,
        "stage_http_request",
        lambda *_args, **_kwargs: _Response(503, "upstream unavailable"),
    )
    with pytest.raises(ProtocolError) as caught:
        gopay_cs_live.cs_elements_session(
            object(),
            _checkout(),
            {"payment_method_types": ["gopay"], "total_summary": {"due": 0}},
            _context(),
            None,
        )
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert caught.value.failure_mode == "elements_session_http"


def test_gopay_elements_exception_is_explicit_and_retryable(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        raise RuntimeError("transport down")

    monkeypatch.setattr(gopay_cs_live, "stage_http_request", fail)
    with pytest.raises(ProtocolError) as caught:
        gopay_cs_live.cs_elements_session(
            object(),
            _checkout(),
            {"payment_method_types": ["gopay"], "total_summary": {"due": 0}},
            _context(),
            None,
        )
    assert caught.value.status_code == 502
    assert caught.value.retryable is True
    assert caught.value.failure_mode == "elements_session_transport"


def test_gopay_tax_region_http_failure_is_explicit_and_retryable(monkeypatch) -> None:
    monkeypatch.setattr(
        gopay_cs_live,
        "stage_http_request",
        lambda *_args, **_kwargs: _Response(500, "tax failure"),
    )
    with pytest.raises(ProtocolError) as caught:
        gopay_cs_live._cs_update_tax_region_fields(
            object(),
            _checkout(),
            _context(),
            _billing(),
            None,
            ("country",),
        )
    assert caught.value.status_code == 500
    assert caught.value.retryable is True
    assert caught.value.failure_mode == "tax_region_http"


def test_gopay_tax_region_unauthorized_failure_is_not_retryable(monkeypatch) -> None:
    monkeypatch.setattr(
        gopay_cs_live,
        "stage_http_request",
        lambda *_args, **_kwargs: _Response(401, "expired"),
    )
    with pytest.raises(ProtocolError) as caught:
        gopay_cs_live._cs_update_tax_region_fields(
            object(),
            _checkout(),
            _context(),
            _billing(),
            None,
            ("country",),
        )
    assert caught.value.status_code == 401
    assert caught.value.retryable is False


def test_gopay_snapshot_http_failure_is_explicit_and_retryable(monkeypatch) -> None:
    monkeypatch.setattr(
        gopay_cs_live,
        "stage_http_request",
        lambda *_args, **_kwargs: _Response(503, "snapshot unavailable"),
    )
    with pytest.raises(ProtocolError) as caught:
        gopay_cs_live.cs_snapshot_billing(
            object(),
            _checkout(),
            _billing(),
            None,
        )
    assert caught.value.status_code == 503
    assert caught.value.retryable is True
    assert caught.value.failure_mode == "snapshot_http"


def test_gopay_redirect_transport_exhaustion_is_explicit(monkeypatch) -> None:
    calls: list[int] = []

    def fail(*_args, **_kwargs):
        calls.append(1)
        raise RuntimeError("proxy down")

    monkeypatch.setattr(gopay_stripe_common, "stage_http_request", fail)
    monkeypatch.setattr(gopay_stripe_common.time, "sleep", lambda *_args: None)
    with pytest.raises(ProtocolError) as caught:
        gopay_stripe_common.resolve_external_redirect(
            object(),
            "https://pm-redirects.stripe.com/authorize/fixture",
            preferred_hosts=("app.midtrans.com",),
        )
    assert len(calls) == 3
    assert caught.value.status_code == 502
    assert caught.value.retryable is True
    assert caught.value.failure_mode == "provider_redirect_transport"


def test_gopay_redirect_intermediate_response_is_not_success(monkeypatch) -> None:
    monkeypatch.setattr(
        gopay_stripe_common,
        "stage_http_request",
        lambda *_args, **_kwargs: _Response(200),
    )
    with pytest.raises(ProtocolError) as caught:
        gopay_stripe_common.resolve_external_redirect(
            object(),
            "https://pm-redirects.stripe.com/authorize/fixture",
            preferred_hosts=("app.midtrans.com",),
        )
    assert caught.value.status_code == 502
    assert caught.value.failure_mode == "provider_redirect_intermediate"


def test_gopay_redirect_hop_limit_is_not_success(monkeypatch) -> None:
    monkeypatch.setattr(
        gopay_stripe_common,
        "stage_http_request",
        lambda *_args, **_kwargs: SimpleNamespace(
            status_code=302,
            headers={"Location": "/next"},
            text="",
        ),
    )
    with pytest.raises(ProtocolError) as caught:
        gopay_stripe_common.resolve_external_redirect(
            object(),
            "https://pm-redirects.stripe.com/authorize/fixture",
            preferred_hosts=("app.midtrans.com",),
            max_hops=1,
        )
    assert caught.value.status_code == 502
    assert caught.value.failure_mode == "provider_redirect_hops_exhausted"


def test_gopay_redirect_malformed_url_is_explicit() -> None:
    with pytest.raises(ProtocolError) as caught:
        gopay_stripe_common.resolve_external_redirect(
            object(),
            "https://[invalid",
            preferred_hosts=("app.midtrans.com",),
        )
    assert caught.value.status_code == 502
    assert caught.value.failure_mode == "provider_redirect_invalid"


def test_gopay_cs_live_missing_redirect_is_explicit(monkeypatch) -> None:
    init_payload = {
        "id": "ppage_fixture",
        "stripe_hosted_url": "https://checkout.stripe.com/c/pay/cs_fixture",
        "total_summary": {"due": 0},
        "payment_method_types": ["gopay"],
    }
    checkout = _checkout()
    billing = _billing()
    monkeypatch.setattr(
        gopay_cs_live,
        "stripe_init",
        lambda *_args, **_kwargs: (init_payload, "stripe-js-fixture"),
    )
    monkeypatch.setattr(gopay_cs_live, "synchronize_stripe_browser_ids", lambda *_args: None)
    monkeypatch.setattr(gopay_cs_live, "cs_elements_session", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(gopay_cs_live, "prefetch_checkout_approval_proof", lambda *_args: None)
    monkeypatch.setattr(gopay_cs_live, "stripe_consumer_session_lookup", lambda *_args: {})
    monkeypatch.setattr(
        gopay_cs_live,
        "_cs_update_tax_region_fields",
        lambda *_args, **_kwargs: ({}, {}),
    )
    monkeypatch.setattr(gopay_cs_live, "cs_snapshot_billing", lambda *_args: None)
    monkeypatch.setattr(gopay_cs_live, "cs_checkout_taxes", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(gopay_cs_live, "cs_checkout_page_refresh", lambda *_args: {})
    monkeypatch.setattr(gopay_cs_live, "stripe_confirm_cs_live", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(gopay_cs_live, "provider_redirect_after_confirm", lambda *_args: "")
    monkeypatch.setattr(
        gopay_cs_live,
        "resolve_external_redirect",
        lambda *_args, **_kwargs: pytest.fail("redirect resolver must not accept an empty input"),
    )
    config = SimpleNamespace(
        payment_method="gopay",
        country="ID",
        stripe_hcaptcha_token="",
    )
    with pytest.raises(ProtocolError, match="no provider redirect"):
        gopay_cs_live.extract_cs_live_provider(
            config,
            object(),
            object(),
            checkout,
            billing,
            None,
        )
