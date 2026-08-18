from __future__ import annotations

import inspect
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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
    assert len(supported["countries"]) == 197
    assert len(fields) == 32
    assert required_address_fields("DE") == ("line1", "postcode", "city")
    assert required_address_fields("SG") == ("line1", "postcode")


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
    assert "app.js?v=20260818-session-runtime-1" in html


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


def test_dynamic_task_concurrency_resizes_without_losing_stage_events() -> None:
    blocker = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    peak = 0

    def extractor(config, *, cancel_event, stage_callback):
        nonlocal active, peak
        stage_callback("checkout")
        with counter_lock:
            active += 1
            peak = max(peak, active)
        try:
            blocker.wait(3)
            return {"ok": True, "billing_country": config.country}
        finally:
            with counter_lock:
                active -= 1

    manager = TaskManager(extractor, max_workers=3, concurrency=1)
    _, subscriber = manager.subscribe()
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example.test:8080",
        update_proxy="",
        apply_checkout_update=False,
    )
    try:
        task_ids = [manager.create(config)["task_id"] for _ in range(3)]
        deadline = time.time() + 2
        while time.time() < deadline:
            with counter_lock:
                if active == 1:
                    break
            time.sleep(0.02)
        with counter_lock:
            assert active == 1

        assert manager.set_concurrency(2) == 2
        deadline = time.time() + 2
        while time.time() < deadline:
            with counter_lock:
                if active == 2:
                    break
            time.sleep(0.02)
        with counter_lock:
            assert active == 2
            assert peak == 2

        blocker.set()
        deadline = time.time() + 3
        while time.time() < deadline:
            if all((manager.get(task_id) or {}).get("status") == "succeeded" for task_id in task_ids):
                break
            time.sleep(0.02)
        assert all((manager.get(task_id) or {}).get("status") == "succeeded" for task_id in task_ids)
        streamed = []
        while not subscriber.empty():
            streamed.append(subscriber.get_nowait())
        assert any(event["type"] == "task.concurrency" for event in streamed)
        assert any(event["type"] == "task.stage" and event["data"]["stage"] == "checkout" for event in streamed)
    finally:
        blocker.set()
        manager.unsubscribe(subscriber)
        manager.close()


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
        updated = client.post(
            "/api/tasks/concurrency",
            headers=headers,
            json={"concurrency": 3},
        )
        assert updated.status_code == 200
        assert updated.get_json()["concurrency"] == 3
        assert updated.get_json()["max_concurrency"] == 4
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
