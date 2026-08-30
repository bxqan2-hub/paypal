from __future__ import annotations

import queue
import time

import pytest

from payment_link_extractor.errors import ConfigurationError, ExtractionCancelled, ProtocolError
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.web.routes import _config_from_payload
from payment_link_extractor.web.tasks import TaskManager


def _wait_for_terminal(manager: TaskManager, task_id: str) -> dict:
    deadline = time.time() + 3
    while time.time() < deadline:
        snapshot = manager.get(task_id) or {}
        if snapshot.get("status") in {"succeeded", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.01)
    raise AssertionError("task did not become terminal")


def _events(subscriber: queue.Queue) -> list[dict]:
    events = []
    while not subscriber.empty():
        events.append(subscriber.get_nowait())
    return events


def test_failed_attempt_restarts_full_flow_with_next_proxy_until_success() -> None:
    calls: list[tuple[str, str, list[str]]] = []

    def extractor(config, *, cancel_event, stage_callback):
        stages: list[str] = []
        stage_callback("eligibility_check")
        stages.append("eligibility_check")
        stage_callback("checkout")
        stages.append("checkout")
        calls.append((config.checkout_proxy, config.update_proxy, stages))
        if len(calls) < 3:
            raise RuntimeError(f"attempt {len(calls)} failed")
        return {"ok": True, "billing_country": config.country}

    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://checkout-ip-1.example:8080",
        update_proxy="http://update-ip-1.example:8080",
        retry_count=2,
        checkout_proxy_attempts=(
            "http://checkout-ip-1.example:8080",
            "http://checkout-ip-2.example:8080",
            "http://checkout-ip-3.example:8080",
        ),
        update_proxy_attempts=(
            "http://update-ip-1.example:8080",
            "http://update-ip-2.example:8080",
            "http://update-ip-3.example:8080",
        ),
    )
    manager = TaskManager(extractor, max_workers=1)
    _, subscriber = manager.subscribe()
    try:
        task_id = manager.create(config)["task_id"]
        snapshot = _wait_for_terminal(manager, task_id)
        events = _events(subscriber)

        assert snapshot["status"] == "succeeded"
        assert snapshot["attempt"] == 3
        assert snapshot["max_attempts"] == 3
        assert snapshot["checkout_proxy"] == "http://checkout-ip-3.example:8080"
        assert [call[0] for call in calls] == [
            "http://checkout-ip-1.example:8080",
            "http://checkout-ip-2.example:8080",
            "http://checkout-ip-3.example:8080",
        ]
        assert [call[1] for call in calls] == [
            "http://update-ip-1.example:8080",
            "http://update-ip-2.example:8080",
            "http://update-ip-3.example:8080",
        ]
        assert all(call[2] == ["eligibility_check", "checkout"] for call in calls)
        retry_events = [event for event in events if event["type"] == "task.retrying"]
        assert [event["data"]["next_attempt"] for event in retry_events] == [2, 3]
        assert all(event["data"]["ip_rotated"] is True for event in retry_events)
    finally:
        manager.unsubscribe(subscriber)
        manager.close()


def test_retry_exhaustion_reports_last_full_attempt() -> None:
    calls: list[str] = []

    def extractor(config, *, cancel_event, stage_callback):
        calls.append(config.checkout_proxy)
        stage_callback("checkout")
        raise RuntimeError("still failed")

    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://checkout-ip-1.example:8080",
        update_proxy="",
        apply_checkout_update=False,
        retry_count=1,
        checkout_proxy_attempts=(
            "http://checkout-ip-1.example:8080",
            "http://checkout-ip-2.example:8080",
        ),
    )
    manager = TaskManager(extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        snapshot = _wait_for_terminal(manager, task_id)

        assert snapshot["status"] == "failed"
        assert snapshot["attempt"] == 2
        assert snapshot["max_attempts"] == 2
        assert calls == [
            "http://checkout-ip-1.example:8080",
            "http://checkout-ip-2.example:8080",
        ]
        assert snapshot["error"] == "still failed"
    finally:
        manager.close()


def test_user_cancellation_does_not_start_an_automatic_retry() -> None:
    calls: list[str] = []

    def extractor(config, *, cancel_event, stage_callback):
        calls.append(config.checkout_proxy)
        raise ExtractionCancelled("cancelled by fixture")

    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://checkout-ip-1.example:8080",
        update_proxy="",
        apply_checkout_update=False,
        retry_count=2,
        checkout_proxy_attempts=(
            "http://checkout-ip-1.example:8080",
            "http://checkout-ip-2.example:8080",
            "http://checkout-ip-3.example:8080",
        ),
    )
    manager = TaskManager(extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        snapshot = _wait_for_terminal(manager, task_id)

        assert snapshot["status"] == "cancelled"
        assert snapshot["attempt"] == 1
        assert calls == ["http://checkout-ip-1.example:8080"]
    finally:
        manager.close()


def test_route_config_accepts_manual_retry_count_and_proxy_attempt_plan() -> None:
    config = _config_from_payload(
        {
            "access_token": "fixture-token",
            "checkout_proxy": "http://checkout-ip-1.example:8080",
            "update_proxy": "http://update-ip-1.example:8080",
            "retry_count": 2,
            "checkout_proxy_attempts": [
                "http://checkout-ip-1.example:8080",
                "http://checkout-ip-2.example:8080",
                "http://checkout-ip-3.example:8080",
            ],
            "update_proxy_attempts": [
                "http://update-ip-1.example:8080",
                "http://update-ip-2.example:8080",
                "http://update-ip-3.example:8080",
            ],
            "country": "GB",
            "payment_method": "paypal",
            "email": "explicit@example.com",
            "name": "Explicit Name",
        }
    )

    assert config.retry_count == 2
    assert config.checkout_proxy_attempts[-1] == "http://checkout-ip-3.example:8080"
    assert config.update_proxy_attempts[-1] == "http://update-ip-3.example:8080"
    assert config.account_email == "explicit@example.com"
    assert config.account_name == "Explicit Name"


@pytest.mark.parametrize("value", (-1, 11, True, "bad"))
def test_route_config_rejects_invalid_retry_count(value) -> None:
    with pytest.raises(ConfigurationError, match="retry_count"):
        _config_from_payload(
            {
                "access_token": "fixture-token",
                "checkout_proxy": "http://checkout.example:8080",
                "update_proxy": "http://update.example:8080",
                "retry_count": value,
            }
        )


def test_retry_ui_uses_the_mk_single_proxy_pool_contract() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    html = (root / "payment_link_extractor" / "web" / "templates" / "index.html").read_text(
        encoding="utf-8"
    )
    source = (root / "payment_link_extractor" / "web" / "static" / "app.js").read_text(
        encoding="utf-8"
    )

    assert 'id="failure-retry-count"' in html
    assert 'id="proxy-pool"' in html
    assert "所有支付步骤共用一个自备节点池" in html
    assert "payload.proxy_pool" in source
    assert "payload.max_attempts = retryCount + 1;" in source
    assert 'event.type === "task.retrying"' in source


def test_route_config_maps_mk_proxy_pool_to_every_payment_step() -> None:
    config = _config_from_payload(
        {
            "access_token": "fixture-token",
            "proxy_pool": [
                "http://proxy-1.example:8080",
                "http://proxy-2.example:8080",
            ],
            "max_attempts": 5,
            "country": "GB",
            "payment_method": "paypal",
        }
    )

    assert config.retry_count == 4
    assert config.checkout_proxy == "http://proxy-1.example:8080"
    assert config.update_proxy == config.checkout_proxy
    assert config.checkout_proxy_attempts == config.update_proxy_attempts
    assert config.proxy_pool == (
        "http://proxy-1.example:8080",
        "http://proxy-2.example:8080",
    )


def test_route_config_accepts_explicit_gopay_four_segment_proxies() -> None:
    config = _config_from_payload(
        {
            "access_token": "fixture-token",
            "payment_method": "gopay",
            "country": "ID",
            "checkout_proxy": "http://legacy-entry.example:8080",
            "update_proxy": "http://legacy-update.example:8080",
            "gopay_checkout_proxy": "http://a.example:8080",
            "gopay_promotion_proxy": "http://b.example:8080",
            "gopay_provider_proxy": "http://c.example:8080",
            "gopay_approve_proxy": "http://d.example:8080",
            "retry_count": 0,
        }
    )
    assert (
        config.gopay_checkout_proxy,
        config.gopay_promotion_proxy,
        config.gopay_provider_proxy,
        config.gopay_approve_proxy,
    ) == (
        "http://a.example:8080",
        "http://b.example:8080",
        "http://c.example:8080",
        "http://d.example:8080",
    )


def test_gopay_401_stops_attempts_but_other_protocol_errors_retry() -> None:
    calls: list[str] = []

    def extractor(config, *, cancel_event, stage_callback):
        calls.append(config.checkout_proxy)
        raise ProtocolError(401, "access token invalid")

    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy-1.example:8080",
        update_proxy="http://proxy-1.example:8080",
        country="ID",
        payment_method="gopay",
        retry_count=2,
        proxy_pool=(
            "http://proxy-1.example:8080",
            "http://proxy-2.example:8080",
            "http://proxy-3.example:8080",
        ),
    )
    manager = TaskManager(extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        snapshot = _wait_for_terminal(manager, task_id)
        assert snapshot["status"] == "failed"
        assert snapshot["attempt"] == 1
        assert len(calls) == 1
        assert snapshot["error"] == "access token invalid"
    finally:
        manager.close()


def test_gopay_checkout_boundary_consumes_account_and_blocks_retry() -> None:
    calls: list[str] = []

    def extractor(config, *, cancel_event, stage_callback):
        del cancel_event
        calls.append(config.checkout_proxy)
        stage_callback("eligibility_check")
        stage_callback("eligibility_confirmed")
        stage_callback("checkout")
        stage_callback("checkout_committed")
        stage_callback("payment_confirmation")
        raise ProtocolError(409, "ChatGPT manual approval blocked")

    proxies = (
        "http://id-proxy-1.example:8080",
        "http://id-proxy-2.example:8080",
    )
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy=proxies[0],
        update_proxy=proxies[0],
        payment_method="gopay",
        country="ID",
        retry_count=1,
        checkout_proxy_attempts=proxies,
        update_proxy_attempts=proxies,
        proxy_pool=proxies,
    )
    manager = TaskManager(extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        snapshot = _wait_for_terminal(manager, task_id)
        assert snapshot["status"] == "failed"
        assert snapshot["attempt"] == 1
        assert snapshot["max_attempts"] == 2
        assert snapshot["checkout_opportunity_consumed"] is True
        assert len(calls) == 1
    finally:
        manager.close()


def test_gopay_proxy_pool_attempts_are_random_and_same_proxy_per_attempt() -> None:
    calls: list[tuple[str, str]] = []

    def extractor(config, *, cancel_event, stage_callback):
        calls.append((config.checkout_proxy, config.update_proxy))
        if len(calls) < 3:
            raise ProtocolError(409, "expected zero amount, got 34900000")
        return {"ok": True, "amount_due_minor": 0, "currency": "IDR"}

    pool = (
        "http://proxy-1.example:8080",
        "http://proxy-2.example:8080",
        "http://proxy-3.example:8080",
    )
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy=pool[0],
        update_proxy=pool[0],
        country="ID",
        payment_method="gopay",
        retry_count=2,
        proxy_pool=pool,
    )
    manager = TaskManager(extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        snapshot = _wait_for_terminal(manager, task_id)
        assert snapshot["status"] == "succeeded"
        assert len(calls) == 3
        assert len({checkout for checkout, _ in calls}) == 3
        assert all(checkout == update for checkout, update in calls)
        assert {checkout for checkout, _ in calls} == set(pool)
    finally:
        manager.close()


def test_gopay_legacy_attempt_lists_cannot_split_one_attempt_across_proxies() -> None:
    calls: list[tuple[str, str]] = []

    def extractor(config, *, cancel_event, stage_callback):
        calls.append((config.checkout_proxy, config.update_proxy))
        if len(calls) == 1:
            raise ProtocolError(409, "promo eligibility rejected: state=not_eligible")
        return {"ok": True, "amount_due_minor": 0, "currency": "IDR"}

    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://checkout-1.example:8080",
        update_proxy="http://update-1.example:8080",
        country="ID",
        payment_method="gopay",
        retry_count=1,
        checkout_proxy_attempts=(
            "http://checkout-1.example:8080",
            "http://checkout-2.example:8080",
        ),
        update_proxy_attempts=(
            "http://update-1.example:8080",
            "http://update-2.example:8080",
        ),
    )
    manager = TaskManager(extractor, max_workers=1)
    try:
        task_id = manager.create(config)["task_id"]
        snapshot = _wait_for_terminal(manager, task_id)
        assert snapshot["status"] == "succeeded"
        assert calls == [
            ("http://checkout-1.example:8080", "http://checkout-1.example:8080"),
            ("http://checkout-2.example:8080", "http://checkout-2.example:8080"),
        ]
    finally:
        manager.close()


def test_gopay_exhausts_unique_proxy_pool_without_retry_cap() -> None:
    calls: list[str] = []

    def extractor(config, *, cancel_event, stage_callback):
        calls.append(config.checkout_proxy)
        raise ProtocolError(409, "promo eligibility rejected: state=not_eligible")

    pool = tuple(f"http://proxy-{index}.example:8080" for index in range(1, 101))
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy=pool[0],
        update_proxy=pool[0],
        country="ID",
        payment_method="gopay",
        retry_count=0,
        proxy_pool=pool,
    )
    manager = TaskManager(extractor, max_workers=1)
    try:
        created = manager.create(config)
        assert created["max_attempts"] == len(pool)
        snapshot = _wait_for_terminal(manager, created["task_id"])
        assert snapshot["status"] == "failed"
        assert snapshot["attempt"] == len(pool)
        assert snapshot["max_attempts"] == len(pool)
        assert len(calls) == len(pool)
        assert set(calls) == set(pool)
    finally:
        manager.close()
