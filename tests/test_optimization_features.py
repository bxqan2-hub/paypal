from __future__ import annotations

import inspect
import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "paypal_agreement_protocol"
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))

import web as protocol_web  # noqa: E402
from paypal.country_schema import required_address_fields  # noqa: E402
from paypal.flow import PayPalFlow  # noqa: E402
from paypal.manual_browser import browser_launch_profile  # noqa: E402
from paypal.models import CardInfo, SessionState, generate_address, generate_user  # noqa: E402
from paypal.proxy import ProxyConfig, ProxyEntry  # noqa: E402
from payment_link_extractor.models import ExtractionConfig  # noqa: E402
from payment_link_extractor.web.app import create_app  # noqa: E402
from payment_link_extractor.web.events import make_event  # noqa: E402
from payment_link_extractor.web.tasks import TaskManager  # noqa: E402


def test_country_catalogs_are_complete_and_keep_verified_schemas() -> None:
    supported = json.loads(
        (PROTOCOL_ROOT / "data" / "paypal_supported_countries.json").read_text(encoding="utf-8")
    )
    fields = json.loads(
        (PROTOCOL_ROOT / "data" / "country_discovery" / "country_field_catalog.json").read_text(encoding="utf-8")
    )
    supported_codes = {str(item.get("code") or "").upper() for item in supported["countries"]}
    cached_codes = {
        str(item.get("code") or "").upper()
        for item in supported["countries"]
        if item.get("schema_cached")
    }

    assert len(supported["countries"]) == 197
    assert len(fields) == 43  # 32 discovered + 11 restored; CA overlaps both catalogs
    assert protocol_web.VERIFIED_PROTOCOL_COUNTRIES.issubset(fields)
    assert cached_codes == (set(fields) & supported_codes)
    assert len(cached_codes) == 42  # catalog-only TR is not a supported protocol country
    for country in protocol_web.VERIFIED_PROTOCOL_COUNTRIES - {"CA"}:
        assert required_address_fields(country) == ("line1", "city", "postalCode")
    # Keep the richer discovered CA schema instead of downgrading it to the old generic profile.
    assert required_address_fields("CA") == ("line1", "city", "state", "postcode")
    assert required_address_fields("DE") == ("line1", "postcode", "city")
    assert required_address_fields("SG") == ("line1", "postcode")


def test_dynamic_default_gate_is_limited_to_paypal_schema_countries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(protocol_web, "ENABLE_DYNAMIC_COUNTRIES", True)
    catalog = json.loads(
        (PROTOCOL_ROOT / "data" / "country_discovery" / "country_field_catalog.json").read_text(
            encoding="utf-8"
        )
    )
    supported_codes = {
        str(item.get("code") or "").upper()
        for item in json.loads(
            (PROTOCOL_ROOT / "data" / "paypal_supported_countries.json").read_text(encoding="utf-8")
        )["countries"]
    }
    expected_dynamic = set(catalog) & supported_codes
    allowed = protocol_web.enabled_protocol_country_codes()

    assert protocol_web.dynamic_schema_country_codes() == expected_dynamic
    assert len(expected_dynamic) == 42  # merged schemas minus catalog-only TR
    assert len(allowed) == 42
    assert {"DE", "ES", "IE", "SG"}.issubset(allowed)
    assert "AD" not in allowed
    assert "TR" not in allowed

    monkeypatch.setattr(protocol_web, "ENABLE_DYNAMIC_COUNTRIES", False)
    assert protocol_web.enabled_protocol_country_codes() == protocol_web.VERIFIED_PROTOCOL_COUNTRIES

    # A missing/corrupt dynamic catalog must fail closed to the original 12 countries.
    monkeypatch.setattr(protocol_web, "ENABLE_DYNAMIC_COUNTRIES", True)
    monkeypatch.setattr(protocol_web, "ROOT", tmp_path)
    assert protocol_web.dynamic_schema_country_codes() == set()
    assert protocol_web.enabled_protocol_country_codes() == protocol_web.VERIFIED_PROTOCOL_COUNTRIES


def test_proxy_bridge_api_keeps_direct_1024proxy_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []
    monkeypatch.setenv("PAYPAL_PROXY_USE_BRIDGE", "1")
    monkeypatch.setitem(
        sys.modules,
        "oai_iprocket_chain_bridge",
        SimpleNamespace(ensure_background_server=lambda: calls.append(True) or True),
    )
    entry = ProxyEntry.parse("gw.1024proxy.io:3000:USER:PASS")
    assert entry.uses_bridge is True
    assert entry.url.startswith("http://iprb_")
    assert ProxyConfig(True, entry).prepare() is True
    assert calls == [True]

    monkeypatch.setenv("PAYPAL_PROXY_USE_BRIDGE", "0")
    assert entry.uses_bridge is False
    assert entry.direct_url.startswith("socks5h://")
    assert entry.url == entry.direct_url


def test_signup_variables_treat_missing_normalization_flag_as_manual() -> None:
    flow = PayPalFlow.__new__(PayPalFlow)
    flow.country = "SG"
    flow.lang = "en"
    flow.user = generate_user("+6581234567", "SG")
    flow.user.email = "fixture@example.com"
    flow.card = CardInfo("4111111111111111", "12/2030", "123")
    flow.address = generate_address("SG")
    flow.address.street = "Cluny Road"
    flow.address.house_number = "1"
    flow.address.city = "Singapore"
    flow.address.postal_code = "259569"
    flow.state = SessionState(content_identifier="SG:en:fixture:compliance.signupTerms")
    flow.runtime_form_schema = {"address_fields": [], "kyc": {"fields": []}}

    variables = flow._build_signup_variables("EC-FIXTURE")
    quality = variables["billingAddress"]["accountQuality"]
    assert quality["autoCompleteType"] == "MANUAL"
    assert quality["isUserModified"] is True


def test_identity_elevation_is_default_but_original_remains_supported() -> None:
    assert inspect.signature(protocol_web.create_job).parameters["buyer_mode"].default == "identity_elevation"
    job = protocol_web.WebJob(
        id="buyerfixture",
        owner_device_id="devicefixture",
        ba_token="BA-BUYERFIXTURE01",
        phone="+447700900123",
    )
    assert job.buyer_mode == "identity_elevation"
    job.buyer_mode = "original"
    assert job.buyer_mode == "original"
    html = (PROTOCOL_ROOT / "web_static" / "index.html").read_text(encoding="utf-8")
    assert '<option value="identity_elevation" selected>' in html
    assert '<option value="original">' in html


def test_explicit_original_mode_dispatches_original_flow() -> None:
    selected: list[str] = []

    class FakeOriginalFlow:
        def __init__(self, **kwargs):
            selected.append("original")
            self.job = kwargs["job"]

        def run(self):
            return {
                "status": "success",
                "return_url": "https://merchant.fixture/return?status=success",
            }

        def close(self):
            return None

    class ForbiddenElevationFlow:
        def __init__(self, **kwargs):
            raise AssertionError("identity elevation must not run in explicit original mode")

    proxy = ProxyConfig(True, ProxyEntry("127.0.0.1", 9999, "", ""))
    job = protocol_web.WebJob(
        id="originalfixture",
        owner_device_id="devicefixture",
        ba_token="BA-ORIGINALFIXTURE01",
        phone="+447700900123",
        country="GB",
        buyer_mode="original",
        max_card_attempts=1,
        proxy_enabled=True,
        _proxy_config=proxy,
        _proxy_pool=["http://127.0.0.1:9999"],
    )
    with (
        patch.object(protocol_web, "find_authorization_checkpoint", return_value=None),
        patch.object(protocol_web, "select_working_proxy", return_value=proxy),
        patch.object(protocol_web, "generate_card", return_value=CardInfo("4111111111111111", "12/2030", "123")),
        patch.object(protocol_web, "WebPayPalFlow", FakeOriginalFlow),
        patch.object(protocol_web, "WebIdentityElevationPayPalFlow", ForbiddenElevationFlow),
        patch.object(protocol_web, "_authorization_checkpoint_from_result", return_value=None),
        patch.object(protocol_web, "record_payment_audit"),
    ):
        protocol_web._run_job_attempt(job)
    assert selected == ["original"]
    assert job.status == "completed"
    assert job.result["redirect_status"] == "success"


def test_identity_and_original_modes_dispatch_different_flows() -> None:
    selected: list[str] = []

    class FakeFlow:
        label = "base"

        def __init__(self, **kwargs):
            selected.append(self.label)
            self.job = kwargs["job"]

        def run(self):
            return {
                "status": "success",
                "return_url": "https://merchant.fixture/return?status=success",
            }

        def close(self):
            return None

    class FakeOriginalFlow(FakeFlow):
        label = "original"

    class FakeIdentityFlow(FakeFlow):
        label = "identity_elevation"

    proxy = ProxyConfig(True, ProxyEntry("127.0.0.1", 9999, "", ""))
    for buyer_mode, expected in (("identity_elevation", "identity_elevation"), ("original", "original")):
        job = protocol_web.WebJob(
            id=f"mode-{buyer_mode}",
            owner_device_id="devicefixture",
            ba_token=f"BA-{buyer_mode.upper()}FIXTURE01",
            phone="+447700900123",
            country="GB",
            buyer_mode=buyer_mode,
            max_card_attempts=1,
            proxy_enabled=True,
            _proxy_config=proxy,
            _proxy_pool=["http://127.0.0.1:9999"],
        )
        with (
            patch.object(protocol_web, "find_authorization_checkpoint", return_value=None),
            patch.object(protocol_web, "select_working_proxy", return_value=proxy),
            patch.object(protocol_web, "generate_card", return_value=CardInfo("4111111111111111", "12/2030", "123")),
            patch.object(protocol_web, "WebPayPalFlow", FakeOriginalFlow),
            patch.object(protocol_web, "WebIdentityElevationPayPalFlow", FakeIdentityFlow),
            patch.object(protocol_web, "_authorization_checkpoint_from_result", return_value=None),
            patch.object(protocol_web, "record_payment_audit"),
        ):
            protocol_web._run_job_attempt(job)
        assert selected[-1] == expected
        assert job.status == "completed"


def test_protocol_inputs_are_transient_and_legacy_prefill_is_removed() -> None:
    javascript = (PROTOCOL_ROOT / "web_static" / "app.js").read_text(encoding="utf-8")
    backend = (PROTOCOL_ROOT / "web.py").read_text(encoding="utf-8")
    html = (PROTOCOL_ROOT / "web_static" / "index.html").read_text(encoding="utf-8")
    assert "localStorage.setItem(LAST_BA_PREFILL_KEY" not in javascript
    assert "readLastProtocolInputs" not in javascript
    assert "localStorage.removeItem(LEGACY_LAST_BA_PREFILL_KEY)" in javascript
    assert "sessionStorage.setItem(PROTOCOL_FORM_STATE_KEY" in javascript
    assert "synchronizeProcessRuntime(health.runtime_id)" in javascript
    assert "function updateBuyerModeHint()" in javascript
    assert "updateBuyerModeHint();" in javascript
    assert '"runtime_id": PROCESS_RUNTIME_ID' in backend
    assert "paypal.protocol.runtime.v2" in javascript
    assert "app.js?v=20260818-account-history-1" in html


def test_browser_launch_profile_is_cross_platform(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    executable = tmp_path / "browser.exe"
    executable.write_bytes(b"fixture")
    monkeypatch.setenv("PAYPAL_BROWSER_EXECUTABLE", str(executable))
    assert browser_launch_profile(platform_name="nt") == {
        "headless": True,
        "executable_path": str(executable),
    }
    assert browser_launch_profile(platform_name="posix") == {
        "headless": False,
        "executable_path": str(executable),
    }


@pytest.fixture
def controlled_task_manager():
    """Real worker threads, individually released without network or sleeps."""
    managers = []

    def create(total=20, concurrency=8, max_workers=32):
        state = SimpleNamespace(
            condition=threading.Condition(), permits=[threading.Event() for _ in range(total)],
            started=[], active=set(), peak=0, failures=set(), results={}, completed={}, streamed=[],
        )

        def extractor(config, *, cancel_event, stage_callback):
            index = int(config.access_token.removeprefix("fixture-"))
            stage_callback("checkout")
            with state.condition:
                state.started.append(index)
                state.active.add(index)
                state.peak = max(state.peak, len(state.active))
                state.condition.notify_all()
            try:
                assert state.permits[index].wait(5), "fixture release timed out"
                if cancel_event.is_set():
                    from payment_link_extractor.errors import ExtractionCancelled
                    raise ExtractionCancelled("fixture cancelled")
                if index in state.failures:
                    raise RuntimeError("fixture extraction failure")
                stage_callback("confirm")
                return state.results.get(index, {"ok": True, "billing_country": config.country})
            finally:
                with state.condition:
                    state.active.remove(index)
                    state.condition.notify_all()

        state.manager = TaskManager(extractor, max_workers=max_workers, concurrency=concurrency)
        _, state.subscriber = state.manager.subscribe()
        managers.append(state)
        state.task_ids = [state.manager.create(ExtractionConfig(
            access_token=f"fixture-{index}", checkout_proxy="http://proxy.example.test:8080",
            update_proxy="", apply_checkout_update=False,
        ))["task_id"] for index in range(total)]
        return state

    yield create
    for state in managers:
        for permit in state.permits:
            permit.set()
        state.manager.close()
        state.manager.unsubscribe(state.subscriber)


def _wait_for_fixture_starts(state, count):
    with state.condition:
        assert state.condition.wait_for(lambda: len(state.started) >= count, timeout=3)
        assert len(state.started) == count


def _wait_for_fixture_terminal(state, indices):
    expected = {state.task_ids[index] for index in indices}
    while not expected.issubset(state.completed):
        event = state.subscriber.get(timeout=3)
        state.streamed.append(event)
        if event["type"] in {"task.succeeded", "task.failed", "task.cancelled"}:
            state.completed[event["task_id"]] = event["data"]["status"]


def test_dynamic_task_concurrency_resizes_without_losing_stage_events(controlled_task_manager) -> None:
    state = controlled_task_manager()
    _wait_for_fixture_starts(state, 8)
    assert state.manager.concurrency_snapshot() == {
        "concurrency": 8, "max_concurrency": 32, "active_slots": 8, "queued_tasks": 12,
    }
    assert state.peak == 8
    assert state.manager.set_concurrency(12) == 12
    _wait_for_fixture_starts(state, 12)
    assert len(state.active) == 12
    assert state.manager.concurrency_snapshot()["queued_tasks"] == 8
    for permit in state.permits:
        permit.set()
    _wait_for_fixture_terminal(state, range(20))
    state.manager.close()
    assert set(state.completed.values()) == {"succeeded"}
    assert len(state.started) == 20
    assert state.peak == 12
    assert state.manager.concurrency_snapshot()["active_slots"] == 0
    assert state.manager.concurrency_snapshot()["queued_tasks"] == 0
    assert any(event["type"] == "task.concurrency" for event in state.streamed)
    for stage in ("checkout", "confirm"):
        assert {event["task_id"] for event in state.streamed if event["type"] == "task.stage"
                and event["data"]["stage"] == stage} == set(state.task_ids)


def test_dynamic_concurrency_decrease_drains_existing_tasks_before_refilling(controlled_task_manager):
    state = controlled_task_manager()
    _wait_for_fixture_starts(state, 8)
    assert state.manager.set_concurrency(3) == 3
    assert len(state.active) == 8
    first = state.started[:5]
    for index in first:
        state.permits[index].set()
    _wait_for_fixture_terminal(state, first)
    with state.condition:
        assert state.condition.wait_for(lambda: len(state.active) == 3, timeout=3)
        assert not state.condition.wait_for(lambda: len(state.started) > 8, timeout=0.15)
    assert state.manager.concurrency_snapshot()["queued_tasks"] == 12
    state.permits[state.started[5]].set()
    _wait_for_fixture_starts(state, 9)
    assert len(state.active) == 3
    for permit in state.permits:
        permit.set()
    _wait_for_fixture_terminal(state, range(20))
    assert set(state.completed.values()) == {"succeeded"}
    assert state.peak == 8


def test_dynamic_concurrency_preserves_fifo_and_never_runs_queued_cancellation(controlled_task_manager):
    state = controlled_task_manager(total=6, concurrency=1, max_workers=8)
    _wait_for_fixture_starts(state, 1)
    assert state.started == [0]
    assert state.manager.cancel(state.task_ids[2])["status"] == "cancelled"
    assert state.manager.concurrency_snapshot()["queued_tasks"] == 4
    for count, index in enumerate((0, 1, 3, 4), start=2):
        state.permits[index].set()
        _wait_for_fixture_starts(state, count)
    state.permits[5].set()
    _wait_for_fixture_terminal(state, range(6))
    assert state.started == [0, 1, 3, 4, 5]
    assert state.peak == 1
    assert state.completed[state.task_ids[2]] == "cancelled"


def test_dynamic_concurrency_failure_and_active_cancel_release_exactly_one_slot(controlled_task_manager):
    state = controlled_task_manager(total=8, concurrency=3, max_workers=8)
    _wait_for_fixture_starts(state, 3)
    failed, cancelled = state.started[:2]
    state.failures.add(failed)
    state.permits[failed].set()
    _wait_for_fixture_terminal(state, [failed])
    _wait_for_fixture_starts(state, 4)
    assert len(state.active) == 3
    assert state.manager.cancel(state.task_ids[cancelled])["status"] == "cancel_requested"
    with state.condition:
        assert not state.condition.wait_for(lambda: len(state.started) > 4, timeout=0.15)
    state.permits[cancelled].set()
    _wait_for_fixture_terminal(state, [cancelled])
    _wait_for_fixture_starts(state, 5)
    assert len(state.active) == 3
    for permit in state.permits:
        permit.set()
    _wait_for_fixture_terminal(state, range(8))
    state.manager.close()
    assert state.completed[state.task_ids[failed]] == "failed"
    assert state.completed[state.task_ids[cancelled]] == "cancelled"
    assert sum(status == "succeeded" for status in state.completed.values()) == 6
    assert state.peak == 3
    assert state.manager.concurrency_snapshot()["active_slots"] == 0


def test_dynamic_concurrency_close_keeps_running_task_and_cancels_pending_without_record_loss(controlled_task_manager):
    from payment_link_extractor.web.tasks import TaskStateError

    state = controlled_task_manager(total=4, concurrency=1, max_workers=4)
    _wait_for_fixture_starts(state, 1)
    state.manager.close(wait=False)
    _wait_for_fixture_terminal(state, range(1, 4))
    assert state.started == [0]
    assert state.manager.get(state.task_ids[0])["status"] == "running"
    assert set(state.completed.values()) == {"cancelled"}
    assert state.manager.concurrency_snapshot() == {
        "concurrency": 1, "max_concurrency": 4, "active_slots": 1, "queued_tasks": 0,
    }
    before = {record["task_id"] for record in state.manager.list()}
    with pytest.raises(TaskStateError, match="closed"):
        state.manager.create(ExtractionConfig(
            access_token="fixture-99", checkout_proxy="http://proxy.example:8080", update_proxy="",
        ))
    with pytest.raises(TaskStateError, match="closed"):
        state.manager.retry(state.task_ids[1])
    assert {record["task_id"] for record in state.manager.list()} == before == set(state.task_ids)
    assert state.manager.get(state.task_ids[1])["status"] == "cancelled"
    state.permits[0].set()
    _wait_for_fixture_terminal(state, range(4))
    state.manager.close()
    assert state.completed[state.task_ids[0]] == "succeeded"
    assert state.started == [0]
    assert state.manager.concurrency_snapshot()["active_slots"] == 0


@pytest.mark.parametrize("enqueue_before_error", [False, True])
def test_dynamic_concurrency_submit_failure_finalizes_without_executing_orphan_work(
    controlled_task_manager, monkeypatch, enqueue_before_error,
):
    state = controlled_task_manager(total=0, concurrency=1, max_workers=1)
    state.permits.append(threading.Event())
    submit = state.manager._executor.submit
    orphan_futures = []

    def failed_submit(*args, **kwargs):
        if enqueue_before_error:
            # Real submit enqueues a WorkItem; the manager lock delays its
            # entry until submission failure has made the record terminal.
            orphan_futures.append(submit(*args, **kwargs))
        raise RuntimeError("fixture pool submission failure")

    with monkeypatch.context() as scoped:
        scoped.setattr(state.manager._executor, "submit", failed_submit)
        failed = state.manager.create(ExtractionConfig(
            access_token="fixture-0", checkout_proxy="http://proxy.example:8080", update_proxy="",
        ))
    state.task_ids.append(failed["task_id"])
    assert failed["status"] == "failed"
    assert failed["finished_at"]
    assert state.manager.concurrency_snapshot()["active_slots"] == 0
    for future in orphan_futures:
        future.result(timeout=3)
    assert state.started == []
    state.permits.append(threading.Event())
    state.task_ids.append(state.manager.create(ExtractionConfig(
        access_token="fixture-1", checkout_proxy="http://proxy.example:8080", update_proxy="",
    ))["task_id"])
    _wait_for_fixture_starts(state, 1)
    assert state.started == [1]
    state.permits[1].set()
    _wait_for_fixture_terminal(state, range(2))
    state.manager.close()
    assert state.completed == {state.task_ids[0]: "failed", state.task_ids[1]: "succeeded"}
    assert state.manager.concurrency_snapshot()["active_slots"] == 0


def test_dynamic_concurrency_result_conversion_failure_finishes_and_dispatches_next(controlled_task_manager):
    state = controlled_task_manager(total=2, concurrency=1, max_workers=2)
    _wait_for_fixture_starts(state, 1)
    convert = Mock(side_effect=RuntimeError("fixture result conversion failure"))
    state.results[0] = SimpleNamespace(to_dict=convert)
    state.permits[0].set()
    _wait_for_fixture_terminal(state, [0])
    _wait_for_fixture_starts(state, 2)
    convert.assert_called_once_with()
    assert state.completed[state.task_ids[0]] == "failed"
    assert state.manager.get(state.task_ids[0])["finished_at"]
    assert state.manager.concurrency_snapshot()["active_slots"] == 1
    state.permits[1].set()
    _wait_for_fixture_terminal(state, range(2))
    state.manager.close()
    assert state.completed[state.task_ids[1]] == "succeeded"
    assert state.peak == 1
    assert state.manager.concurrency_snapshot()["active_slots"] == 0


def test_dynamic_concurrency_latest_snapshot_survives_full_subscriber_queue(controlled_task_manager):
    state = controlled_task_manager(total=0, concurrency=1, max_workers=3)
    while not state.subscriber.full():
        state.subscriber.put_nowait(make_event("fixture", "task.log", {"message": "fixture backlog"}))
    state.manager.set_concurrency(2)
    streamed = []
    while not state.subscriber.empty():
        streamed.append(state.subscriber.get_nowait())
    assert len(streamed) == state.subscriber.maxsize
    assert streamed[-1]["type"] == "task.concurrency"
    assert streamed[-1]["data"] == state.manager.concurrency_snapshot() == {
        "concurrency": 2, "max_concurrency": 3, "active_slots": 0, "queued_tasks": 0,
    }


@pytest.mark.parametrize("payment_method", ["momo", "gopay"])
def test_dynamic_concurrency_cancel_during_retry_backoff_finishes_and_refills(monkeypatch, payment_method):
    from payment_link_extractor.web import tasks

    backoff_entered = threading.Event()
    attempts = []

    def backoff(attempt_index):
        backoff_entered.set()
        return 0.75

    def extractor(config, *, cancel_event, stage_callback):
        attempts.append(config.access_token)
        stage_callback("checkout")
        if config.access_token == "retry-fixture":
            raise RuntimeError("fixture retry")
        return {"ok": True}

    monkeypatch.setattr(tasks, "gopay_retry_backoff_seconds", backoff)
    manager = TaskManager(extractor, max_workers=2, concurrency=1)
    _, subscriber = manager.subscribe()
    try:
        first = manager.create(ExtractionConfig(
            access_token="retry-fixture", checkout_proxy="http://proxy.example:8080", update_proxy="",
            payment_method=payment_method, apply_checkout_update=False, retry_count=1,
        ))["task_id"]
        second = manager.create(ExtractionConfig(
            access_token="next-fixture", checkout_proxy="http://proxy.example:8080", update_proxy="",
            payment_method=payment_method, apply_checkout_update=False,
        ))["task_id"]
        assert backoff_entered.wait(3)
        assert manager.get(first)["stage"] == "retrying"
        assert manager.cancel(first)["status"] == "cancel_requested"
        state = SimpleNamespace(task_ids=[first, second], subscriber=subscriber, completed={}, streamed=[])
        _wait_for_fixture_terminal(state, range(2))
        manager.close()
        assert state.completed == {first: "cancelled", second: "succeeded"}
        assert attempts == ["retry-fixture", "next-fixture"]
        assert manager.get(first)["finished_at"]
        assert manager.concurrency_snapshot()["active_slots"] == 0
    finally:
        manager.close()
        manager.unsubscribe(subscriber)


def test_dynamic_concurrency_api_is_authenticated_and_bounded() -> None:
    app = create_app({
        "TESTING": True,
        "WEB_PASSWORD": "test-password",
        "TASK_WORKERS": 2,
        "TASK_MAX_WORKERS": 4,
    })
    client = app.test_client()
    headers = {"X-Workbench-Password": "test-password"}
    try:
        assert client.get("/api/tasks/concurrency").status_code == 401
        initial = client.get("/api/tasks/concurrency", headers=headers)
        assert initial.status_code == 200
        assert initial.get_json()["concurrency"] == 2
        assert initial.get_json()["queued_tasks"] == 0
        updated = client.post(
            "/api/tasks/concurrency",
            headers=headers,
            json={"concurrency": 3},
        )
        assert updated.status_code == 200
        assert updated.get_json()["concurrency"] == 3
        assert updated.get_json()["max_concurrency"] == 4
        for invalid in (0, -1, 5, 2.5, 2.0, True, False, None, "3", "", [], {}):
            response = client.post("/api/tasks/concurrency", headers=headers, json={"concurrency": invalid})
            assert response.status_code == 400, (invalid, response.get_json())
            assert client.get("/api/tasks/concurrency", headers=headers).get_json()["concurrency"] == 3
        for payload in ({}, [], "3", None):
            assert client.post("/api/tasks/concurrency", headers=headers, json=payload).status_code == 400
        for valid in (1, 4):
            response = client.post("/api/tasks/concurrency", headers=headers, json={"concurrency": valid})
            assert response.status_code == 200
            assert response.get_json()["concurrency"] == valid
    finally:
        app.extensions["payment_task_manager"].close()


def test_dynamic_concurrency_testing_mode_does_not_persist(monkeypatch, tmp_path):
    from payment_link_extractor.web import routes

    env_file = tmp_path / "custom.env"
    original = "OPLL_TASK_WORKERS=4\n# fixture configuration\n"
    env_file.write_text(original, encoding="utf-8")
    monkeypatch.setenv("OPLL_ENV_FILE", str(env_file))
    monkeypatch.setenv("OPLL_TASK_WORKERS", "4")
    save = Mock(side_effect=AssertionError("testing mode must not persist"))
    monkeypatch.setattr(routes, "set_key", save)
    app = create_app({"TESTING": True, "WEB_PASSWORD": "test-password", "TASK_MAX_WORKERS": 32})
    try:
        response = app.test_client().post(
            "/api/tasks/concurrency", headers={"X-Workbench-Password": "test-password"},
            json={"concurrency": 8},
        )
        assert response.status_code == 200
        assert response.get_json()["concurrency"] == 8
        assert env_file.read_text(encoding="utf-8") == original
        assert os.environ["OPLL_TASK_WORKERS"] == "4"
        save.assert_not_called()
    finally:
        app.extensions["payment_task_manager"].close()


def test_dynamic_concurrency_persists_only_worker_setting_and_restores_on_startup(monkeypatch, tmp_path):
    from dotenv import dotenv_values
    from payment_link_extractor.web.env import load_configured_env

    env_file = tmp_path / "custom.env"
    preserved = "OPLL_TASK_MAX_WORKERS=32\n# fixture comment\nFIXTURE_KEEP=unchanged\n"
    env_file.write_text("OPLL_TASK_WORKERS=4\n" + preserved, encoding="utf-8")
    monkeypatch.setenv("OPLL_ENV_FILE", str(env_file))
    monkeypatch.setenv("OPLL_TASK_WORKERS", "4")
    monkeypatch.setenv("OPLL_TASK_MAX_WORKERS", "32")
    monkeypatch.setenv("FIXTURE_KEEP", "unchanged")
    assert load_configured_env() == env_file.resolve()
    app = create_app({"TESTING": True, "WEB_PASSWORD": "test-password"})
    try:
        assert app.config["ENV_FILE"] == str(env_file.resolve())
        app.config.update(TESTING=False, ENV_FILE=str(env_file))
        response = app.test_client().post(
            "/api/tasks/concurrency", headers={"X-Workbench-Password": "test-password"},
            json={"concurrency": 8},
        )
        assert response.status_code == 200
        assert response.get_json()["concurrency"] == 8
        assert dotenv_values(env_file) == {
            "OPLL_TASK_WORKERS": "8", "OPLL_TASK_MAX_WORKERS": "32", "FIXTURE_KEEP": "unchanged",
        }
        assert preserved in env_file.read_text(encoding="utf-8")
        assert os.environ["OPLL_TASK_WORKERS"] == "8"
    finally:
        app.extensions["payment_task_manager"].close()
    monkeypatch.delenv("OPLL_TASK_WORKERS")
    restarted = create_app({"TESTING": True, "WEB_PASSWORD": "test-password"})
    try:
        assert restarted.extensions["payment_task_manager"].concurrency_snapshot() == {
            "concurrency": 8, "max_concurrency": 32, "active_slots": 0, "queued_tasks": 0,
        }
    finally:
        restarted.extensions["payment_task_manager"].close()


def test_dynamic_concurrency_persist_failure_keeps_runtime_and_env_unchanged(monkeypatch, tmp_path):
    from payment_link_extractor.web import routes

    env_file = tmp_path / "custom.env"
    original = "OPLL_TASK_WORKERS=4\n# fixture configuration\n"
    env_file.write_text(original, encoding="utf-8")
    monkeypatch.setenv("OPLL_ENV_FILE", str(env_file))
    monkeypatch.setenv("OPLL_TASK_WORKERS", "4")
    save = Mock(side_effect=OSError("fixture write failure"))
    monkeypatch.setattr(routes, "set_key", save)
    app = create_app({"TESTING": True, "WEB_PASSWORD": "test-password", "TASK_MAX_WORKERS": 32})
    try:
        app.config.update(TESTING=False, ENV_FILE=str(env_file))
        response = app.test_client().post(
            "/api/tasks/concurrency", headers={"X-Workbench-Password": "test-password"},
            json={"concurrency": 8},
        )
        assert response.status_code == 500
        assert app.extensions["payment_task_manager"].concurrency == 4
        assert env_file.read_text(encoding="utf-8") == original
        assert os.environ["OPLL_TASK_WORKERS"] == "4"
        save.assert_called_once_with(str(env_file), "OPLL_TASK_WORKERS", "8")
    finally:
        app.extensions["payment_task_manager"].close()


def test_extractor_and_protocol_events_share_secret_redaction() -> None:
    paypal_url = "https://www.paypal.com/agreements/approve?ba_token=BA-ABCDEFGH123456"
    event = make_event(
        "fixture",
        "task.test",
        {
            "access_token": "secret-access-token",
            "ba_token": "BA-SECRETEVENT01",
            "message": "Bearer header.payload.signature",
            "result": {"provider_url": paypal_url, "paypal_url": paypal_url},
        },
    )
    encoded = json.dumps(event)
    assert "secret-access-token" not in encoded
    assert "BA-SECRETEVENT01" not in encoded
    assert "header.payload.signature" not in encoded
    assert paypal_url in encoded
    assert event["data"]["result"]["provider_url"] == paypal_url
    assert event["data"]["result"]["paypal_url"] == paypal_url

    job = protocol_web.WebJob(
        id="eventfixture",
        owner_device_id="devicefixture",
        ba_token="BA-EVENTFIXTURE01",
        phone="+447700900123",
    )
    job.emit_event(
        "herosms.status",
        {
            "activation_id": "123456789012",
            "phone": "+447700900123",
            "otp": "123456",
            "status": "STATUS_WAIT_CODE",
        },
    )
    protocol_encoded = json.dumps(job.to_dict(), ensure_ascii=False)
    assert "123456789012" not in protocol_encoded
    assert "+447700900123" not in protocol_encoded
    assert '"otp": "123456"' not in protocol_encoded
    assert "STATUS_WAIT_CODE" in protocol_encoded


def test_protocol_step_emits_stage_event_not_cancelled() -> None:
    job = protocol_web.WebJob(
        id="stagefixture",
        owner_device_id="devicefixture",
        ba_token="BA-STAGEFIXTURE01",
        phone="+447700900123",
    )
    job.set_status("running", "Starting protocol")
    job.note_protocol_step("Phase 0: Initial page load")

    assert job.status == "running"
    assert job.events[-1]["type"] == "protocol.stage"
    assert job.events[-1]["data"] == {
        "status": "running",
        "stage": job.stage,
    }
    assert not any(event["type"] == "protocol.cancelled" for event in job.events)

    job.mark_cancelled()
    assert job.status == "cancelled"
    assert job.events[-1]["type"] == "protocol.cancelled"
