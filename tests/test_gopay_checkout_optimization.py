from __future__ import annotations

import threading
import time
import json
from types import SimpleNamespace

import pytest

from payment_link_extractor import checkout
from payment_link_extractor.errors import CheckoutCreateError
from payment_link_extractor.gopay_pro_core.validation import validate_checkout_batch
from payment_link_extractor.gopay_pro_core.core import validate_gopay_amount
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.transport import BrowserSentinelProvider
from payment_link_extractor.transport import DefaultTransportFactory
from payment_link_extractor.web.tasks import TaskManager


def _terminal(manager: TaskManager, task_id: str) -> dict:
    deadline = time.time() + 3
    while time.time() < deadline:
        snapshot = manager.get(task_id) or {}
        if snapshot.get("status") in {"succeeded", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("task did not become terminal")


def test_sentinel_default_aliases_use_chatgpt_checkout_and_sync_observer() -> None:
    provider = BrowserSentinelProvider.__new__(BrowserSentinelProvider)
    provider._failed = False
    provider._started = True
    provider._lock = threading.RLock()
    provider._cookies = ""
    provider._attestation = ""
    provider.device_id = "browser-device"
    provider._ping = lambda _referer: None
    calls: list[str] = []

    def fake_eval(expression: str, timeout: float = 75.0):
        calls.append(expression)
        return "observer-proof" if "sessionObserverToken" in expression else "main-proof"

    provider._eval = fake_eval
    headers = provider.headers("default")
    assert headers["OpenAI-Sentinel-Token"] == "main-proof"
    assert headers["OpenAI-Sentinel-SO-Token"] == "observer-proof"
    assert headers["oai-device-id"] == "browser-device"
    assert any('token("chatgpt_checkout")' in call for call in calls)
    assert all('token("default")' not in call for call in calls)


def test_nextauth_session_cookie_is_chunked_under_chromium_limit() -> None:
    provider = BrowserSentinelProvider.__new__(BrowserSentinelProvider)
    calls: list[list[str]] = []
    provider._run = lambda args: calls.append(list(args))
    value = "x" * 4092
    provider._set_cookie("__Secure-next-auth.session-token", value)
    assert [call[2] for call in calls] == [
        "__Secure-next-auth.session-token.0",
        "__Secure-next-auth.session-token.1",
    ]
    assert len(calls[0][3]) == 3800
    assert len(calls[1][3]) == 292
    assert all("--httpOnly" in call and "--secure" in call for call in calls)


def test_required_sentinel_proof_fails_closed_without_browser_provider() -> None:
    with pytest.raises(RuntimeError, match="browser Sentinel provider is required"):
        from payment_link_extractor.transport import openai_sentinel_headers

        openai_sentinel_headers(SimpleNamespace(), flow="chatgpt_checkout", required=True)


def test_transport_defaults_match_current_gopay_har_contract(monkeypatch) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}

    monkeypatch.setattr("payment_link_extractor.transport.new_session", Session)
    config = ExtractionConfig(
        access_token="fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="ID",
        payment_method="gopay",
        apply_checkout_update=False,
    )
    session = DefaultTransportFactory().chatgpt(config, config.checkout_proxy)
    assert session.headers["oai-language"] == "id-ID"
    assert session.headers["oai-client-build-number"] == "10012890"
    assert session.headers["oai-client-version"] == "prod-7890a3be6202572c0e8e3bb4907574d660b4e4f4"
    telemetry = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout"
    )["oai-telemetry"]
    values = json.loads(telemetry)
    assert len(values) == 8
    assert values[0] == 1 and values[5:] == [2, 0, values[7]]


def test_checkout_methods_merge_and_dedupe_across_nested_payloads() -> None:
    state: dict[str, object] = {
        "payment_method_types": ["card", "gopay"],
        "custom_payment_methods": [{"id": "cpmt_1", "name": "GoPay"}],
    }
    checkout.merge_checkout_payload(
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
    assert len(state["payment_methods"]) == 5


def test_gopay_zero_amount_gate_matches_promotion_contract() -> None:
    validate_gopay_amount(0, promotion_applied=True)
    validate_gopay_amount(349000, promotion_applied=False)
    with pytest.raises(Exception, match="expected zero amount, got 349000"):
        validate_gopay_amount(349000, promotion_applied=True)


@pytest.mark.parametrize(
    ("status", "body", "mode", "retryable"),
    [
        (400, '{"detail":"Our systems have detected unusual activity. Please try again later."}', "unusual_activity", True),
        (429, '{"detail":"Too many requests"}', "rate_limited", True),
        (503, '{"detail":"temporarily unavailable"}', "upstream_transient", True),
        (401, '{"detail":"invalid token"}', "access_token_invalid", False),
    ],
)
def test_checkout_failure_classification(status, body, mode, retryable) -> None:
    assert checkout.classify_checkout_create_failure(status, body) == (mode, retryable)


def test_create_checkout_exposes_structured_unusual_activity(monkeypatch) -> None:
    monkeypatch.setattr(checkout, "openai_sentinel_headers", lambda *_a, **_k: {})
    monkeypatch.setattr(
        checkout,
        "stage_http_request",
        lambda *_a, **_k: SimpleNamespace(
            status_code=400,
            text='{"detail":"Our systems have detected unusual activity. Please try again later."}',
        ),
    )
    config = ExtractionConfig(
        access_token="fixture",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="ID",
        payment_method="gopay",
        apply_checkout_update=False,
    )
    with pytest.raises(CheckoutCreateError) as captured:
        checkout.create_checkout(config, SimpleNamespace(), None)
    assert captured.value.failure_mode == "unusual_activity"
    assert captured.value.retryable is True


def test_unusual_activity_rotates_proxy_but_invalid_at_stops() -> None:
    calls: list[str] = []

    def retryable_extractor(config, **_kwargs):
        calls.append(config.checkout_proxy)
        if len(calls) == 1:
            raise CheckoutCreateError(
                400,
                "unusual activity",
                failure_mode="unusual_activity",
                retryable=True,
            )
        return {"ok": True}

    config = ExtractionConfig(
        access_token="fixture",
        checkout_proxy="http://proxy-1.example:8080",
        update_proxy="",
        country="ID",
        payment_method="gopay",
        apply_checkout_update=False,
        retry_count=1,
        proxy_pool=(
            "http://proxy-1.example:8080",
            "http://proxy-2.example:8080",
        ),
    )
    manager = TaskManager(retryable_extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        assert _terminal(manager, task_id)["status"] == "succeeded"
        assert calls == [
            "http://proxy-1.example:8080",
            "http://proxy-2.example:8080",
        ]
    finally:
        manager.close()

    calls.clear()

    def terminal_extractor(config, **_kwargs):
        calls.append(config.checkout_proxy)
        raise CheckoutCreateError(
            401,
            "invalid token",
            failure_mode="access_token_invalid",
            retryable=False,
        )

    manager = TaskManager(terminal_extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        snapshot = _terminal(manager, task_id)
        assert snapshot["status"] == "failed"
        assert snapshot["attempt"] == 1
        assert calls == ["http://proxy-1.example:8080"]
    finally:
        manager.close()


def test_batch_validation_distinguishes_families_methods_and_failures() -> None:
    report = validate_checkout_batch(
        [
            {"status_code": 200, "payload": {"checkout_session_id": "oaics_one", "payment_method_types": ["gopay", "card"]}},
            {"status_code": 200, "payload": {"checkout_session": {"id": "cs_two", "payment_method_types": ["gopay"]}}},
            {"status_code": 400, "payload": '{"detail":"unusual activity"}'},
            {"status_code": 401, "payload": '{"detail":"invalid token"}'},
        ]
    )
    assert report["sample_count"] == 4
    assert report["success_count"] == 2
    assert report["session_kinds"] == {"openai_custom_checkout": 1, "stripe_checkout": 1}
    assert report["failure_modes"] == {"unusual_activity": 1, "access_token_invalid": 1}
    assert report["payment_methods"] == ["gopay", "card"]
