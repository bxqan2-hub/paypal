from __future__ import annotations

import base64
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from payment_link_extractor import upi
from payment_link_extractor.application import extract_payment_link
from payment_link_extractor.channels import payment_channel
from payment_link_extractor.errors import ConfigurationError, ExtractionCancelled, ProtocolError
from payment_link_extractor.errors import NetworkError
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.upi import worker
from payment_link_extractor.web.routes import _config_from_payload
from payment_link_extractor.web.tasks import TaskManager


ROOT = Path(__file__).resolve().parents[1]


def token(email="upi-test@example.test"):
    encode = lambda value: base64.urlsafe_b64encode(json.dumps(value).encode()).decode().rstrip("=")
    return encode({"alg": "HS256"}) + "." + encode({"https://api.openai.com/profile": {"email": email}, "exp": 4102444800}) + ".fixture"


def config():
    return ExtractionConfig(
        access_token=token(), checkout_proxy="socks5h://test:pass@in.example.test:1080",
        update_proxy="", country="US", payment_method="upi",
    )


def success():
    return {
        "ok": True, "email": "upi-test@example.test", "due": 0,
        "url": "https://payments.stripe.com/upi/instructions/TEST_ONLY",
        "qr_png": "https://qr.stripe.com/TEST_ONLY.png",
        "expires_at": str(int(time.time()) + 300),
        "billing": {"name": "Test", "line1": "Test address", "city": "Test city", "state": "Test state", "postal_code": "000000"},
    }


def wait_for_task(get_snapshot):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = get_snapshot()
        if snapshot["status"] in {"succeeded", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.01)
    pytest.fail("UPI task did not finish within five seconds")


@pytest.fixture
def bridged_core(monkeypatch, request):
    """Run the real JSON-lines worker, replacing only its remote extractor."""
    mode = getattr(request, "param", "success")
    processes = []
    original_popen = subprocess.Popen
    monkeypatch.setenv("MIN_GCASH_UPI_TEST", "must-not-reach-upi")
    script = (
        "import json, os, sys, time\n"
        "from payment_link_extractor.upi import worker\n"
        f"mode = {mode!r}\n"
        f"expected_token = {token()!r}\n"
        f"expected_proxy = {config().checkout_proxy!r}\n"
        f"result = json.loads({json.dumps(success())!r})\n"
        "def extract(account, proxy, *, tries, emit):\n"
        "    assert account['email'] == 'upi-test@example.test'\n"
        "    assert account['access_token'] == expected_token\n"
        "    assert proxy == expected_proxy and tries == 2\n"
        "    assert os.environ['PYTHON_BIN'] == sys.executable\n"
        "    assert os.environ['SENTINEL_PYTHON'] == sys.executable\n"
        "    assert not any(k.startswith('MIN_GCASH_') for k in os.environ)\n"
        "    emit('stage checkout')\n"
        "    if mode == 'wait': time.sleep(60)\n"
        "    if mode == 'exit': os._exit(3)\n"
        "    if mode == 'network_before': raise worker.NetworkDown('PRIVATE_DETAIL')\n"
        "    emit('stage stripe_confirm')\n"
        "    if mode == 'network_after': raise worker.NetworkDown('PRIVATE_DETAIL')\n"
        "    if mode == 'runtime_failure': raise RuntimeError('PRIVATE_DETAIL')\n"
        "    emit('stage instructions')\n"
        "    return result\n"
        "worker.extract = extract\n"
        "raise SystemExit(worker.main())\n"
    )

    def launch(command, *args, **kwargs):
        if command[:4] == [sys.executable, "-u", "-m", "payment_link_extractor.upi.worker"]:
            assert len(command) == 4  # Credentials must travel over stdin, not argv.
            assert kwargs["cwd"] == ROOT
            process = original_popen([sys.executable, "-u", "-c", script], *args, **kwargs)
            processes.append(process)
            return process
        return original_popen(command, *args, **kwargs)

    monkeypatch.setattr(upi.subprocess, "Popen", launch)
    yield processes
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def test_upi_core_is_byte_identical_to_pinned_upstream():
    folder = ROOT / "payment_link_extractor/upi"
    manifest = json.loads((folder / "UPSTREAM.json").read_text(encoding="utf-8"))
    assert manifest["commit"] == "f3cbc40f97e4b1de03fc8eff63f5caaedb285334"
    assert len(manifest["files"]) == 21
    for name, expected in manifest["files"].items():
        assert hashlib.sha256((folder / "core" / name).read_bytes()).hexdigest() == expected, name


def test_registry_and_web_payload_pin_india_without_checkout_update():
    channel = payment_channel("upi")
    assert (channel.country, channel.currency, channel.result_field) == ("IN", "INR", "upi_url")
    assert not channel.uses_legacy_transport and not channel.uses_checkout_update
    cfg = _config_from_payload({"access_token": token(), "payment_method": "upi", "country": "DE", "proxy_pool": [config().checkout_proxy], "max_attempts": 3})
    assert cfg.country == "IN"
    assert cfg.checkout_proxy == cfg.update_proxy == config().checkout_proxy
    assert cfg.retry_count == 2


def test_dispatch_uses_upstream_adapter_and_decodes_email(monkeypatch):
    captured = {}
    def core(account, proxy, cancel, stage):
        captured.update(account=account, proxy=proxy)
        return success()
    monkeypatch.setattr(upi, "_run_core", core)
    result = extract_payment_link(config(), transport_factory=object()).to_dict()
    assert captured["account"]["email"] == "upi-test@example.test"
    assert captured["account"]["access_token"] == token()
    assert captured["proxy"] == config().checkout_proxy
    assert (result["payment_method"], result["billing_country"], result["currency"]) == ("upi", "IN", "INR")
    assert result["upi_url"] == success()["url"]
    assert result["upi_verified"] is True and result["upi_seconds_left"] > 0
    assert not any(key in result for key in ("gopay_url", "gcash_url", "paypal_url", "momo_url", "access_token"))


def test_missing_email_does_not_invoke_core(monkeypatch):
    monkeypatch.setattr(upi, "_run_core", lambda *a: pytest.fail("must not run"))
    with pytest.raises(ConfigurationError, match="识别邮箱") as caught:
        extract_payment_link(replace(config(), access_token="opaque-test-token"))
    assert caught.value.retryable is False


@pytest.mark.parametrize("raw,expected", [
    ("in.example.test:1080:test:pass", "http://test:pass@in.example.test:1080"),
    ("socks5h://in.example.test:1080:test:pass", "socks5h://test:pass@in.example.test:1080"),
    ("in.example.test:8080", "http://in.example.test:8080"),
    ("socks5://test:p%40ss@in.example.test:1080", "socks5://test:p%40ss@in.example.test:1080"),
])
def test_upi_normalizes_ui_proxy_formats_before_core(monkeypatch, raw, expected):
    captured = []
    monkeypatch.setattr(upi, "_run_core", lambda account, proxy, *args: captured.append(proxy) or success())
    extract_payment_link(replace(config(), checkout_proxy=raw))
    assert captured == [expected]


def test_upi_reuses_existing_socks_bridge_for_vendor_exports(monkeypatch):
    from urllib.parse import urlsplit, unquote
    import iprocket_chain_bridge as bridge

    started, captured = [], []
    monkeypatch.setattr(bridge, "ensure_background_server", lambda: started.append(True))
    monkeypatch.setenv("IPROCKET_CHAIN_PROXY", "http://127.0.0.1:18796")
    monkeypatch.setattr(upi, "_run_core", lambda account, proxy, *args: captured.append(proxy) or success())
    extract_payment_link(replace(config(), checkout_proxy="proxy.iprocket.io:9595:TEST_USER:TEST_PASS"))
    proxy = urlsplit(captured[0])
    assert (proxy.scheme, proxy.hostname, proxy.port) == ("http", "127.0.0.1", 18796)
    encoded = proxy.username.removeprefix("iprb_")
    assert base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode() == "socks5|proxy.iprocket.io|9595|TEST_USER"
    assert unquote(proxy.password) == "TEST_PASS" and started == [True]


@pytest.mark.parametrize("raw", ["ftp://in.example.test:1080", "http://in.example.test:invalid"])
def test_invalid_proxy_still_stops_before_core(monkeypatch, raw):
    monkeypatch.setattr(upi, "_run_core", lambda *args: pytest.fail("invalid proxy must not launch core"))
    with pytest.raises(ProtocolError, match="有效的 IN") as caught:
        extract_payment_link(replace(config(), checkout_proxy=raw))
    assert caught.value.retryable is False


def test_json_email_supported_for_non_jwt_token(monkeypatch):
    captured = {}
    monkeypatch.setattr(upi, "_run_core", lambda account, *args: captured.update(account) or success())
    extract_payment_link(replace(config(), access_token="opaque-test-token", account_email="opaque@example.test"))
    assert captured["email"] == "opaque@example.test"


@pytest.mark.parametrize("overrides", [
    {"due": 199900}, {"due": None}, {"url": "https://example.test/upi/instructions/x"},
    {"url": "http://payments.stripe.com/upi/instructions/x"},
    {"expires_at": "1"},
])
def test_result_validation_never_returns_bad_link(monkeypatch, overrides):
    monkeypatch.setattr(upi, "_run_core", lambda *args: {**success(), **overrides})
    with pytest.raises(ProtocolError) as caught:
        extract_payment_link(config())
    assert caught.value.retryable is False


def test_foreign_qr_is_not_renderable(monkeypatch):
    monkeypatch.setattr(upi, "_run_core", lambda *args: {**success(), "qr_png": "https://example.test/tracker.png"})
    assert extract_payment_link(config()).extra["upi_qr_png"] == ""


def test_worker_calls_original_extract_and_streams_only_known_stages(monkeypatch):
    events = []
    def fake_extract(account, proxy, *, tries, emit):
        assert account["access_token"] == "TEST_TOKEN"
        assert proxy == "socks5h://TEST_PROXY:1080" and tries == 2
        for text in ("stage checkout", "SECRET_LOG_MUST_NOT_ESCAPE", "stage stripe_confirm", "stage instructions"):
            emit(text)
        return success()
    monkeypatch.setattr(worker, "extract", fake_extract)
    worker.run({"account": {"access_token": "TEST_TOKEN"}, "proxy": "socks5h://TEST_PROXY:1080"}, events.append)
    assert "SECRET_LOG_MUST_NOT_ESCAPE" not in json.dumps(events)
    assert "TEST_TOKEN" not in json.dumps(events)
    assert {"type": "stage", "stage": "checkout_committed"} in events
    assert events[-1]["ok"] is True


@pytest.mark.parametrize("committed", [False, True])
def test_network_retry_only_before_confirm(monkeypatch, committed):
    def failing(account, proxy, *, tries, emit):
        if committed:
            emit("stage stripe_confirm")
        raise worker.NetworkDown("PRIVATE_PROXY_PASSWORD")
    monkeypatch.setattr(worker, "extract", failing)
    events = []
    worker.run({"account": {}, "proxy": "test"}, events.append)
    assert events[-1]["retryable"] is (not committed)
    assert "PRIVATE_PROXY_PASSWORD" not in json.dumps(events)


@pytest.mark.parametrize("code", ["nonzero_due", "declined", "link_unverified", "checkout_http_401"])
def test_core_business_rejection_not_automatically_retried(monkeypatch, code):
    monkeypatch.setattr(worker, "extract", lambda *args, **kwargs: {"ok": False, "error": code})
    events = []
    worker.run({"account": {}, "proxy": "test"}, events.append)
    assert events[-1]["error"] == code and events[-1]["retryable"] is False


def test_pre_cancel_never_launches_worker(monkeypatch):
    cancelled = threading.Event()
    cancelled.set()
    monkeypatch.setattr(upi, "_run_core", lambda *a: pytest.fail("must not run"))
    with pytest.raises(ExtractionCancelled):
        extract_payment_link(config(), cancel_event=cancelled)


def test_worker_is_spawnable_without_any_network():
    completed = subprocess.run(
        [sys.executable, "-m", "payment_link_extractor.upi.worker"],
        input="{}" + chr(10), text=True, encoding="utf-8", capture_output=True,
        cwd=ROOT, timeout=20,
    )
    assert completed.returncode == 2
    assert json.loads(completed.stdout)["error"] == "invalid_input"


@pytest.mark.parametrize("bridged_core,error_type,retryable", [
    ("network_before", NetworkError, True),
    ("network_after", NetworkError, False),
    ("runtime_failure", ProtocolError, False),
    ("exit", ProtocolError, False),
], indirect=["bridged_core"])
def test_process_bridge_preserves_failure_and_retry_contract(bridged_core, error_type, retryable):
    stages = []
    with pytest.raises(error_type) as caught:
        extract_payment_link(config(), stage_callback=stages.append)
    assert caught.value.retryable is retryable
    assert "PRIVATE_DETAIL" not in str(caught.value)
    assert "checkout" in stages
    assert len(bridged_core) == 1 and bridged_core[0].poll() is not None


@pytest.mark.parametrize("bridged_core", ["wait"], indirect=True)
def test_cancellation_stops_running_worker(bridged_core):
    cancelled = threading.Event()
    def on_stage(stage):
        if stage == "checkout":
            cancelled.set()
    with pytest.raises(ExtractionCancelled):
        extract_payment_link(config(), cancel_event=cancelled, stage_callback=on_stage)
    assert len(bridged_core) == 1 and bridged_core[0].poll() is not None


def test_proxy_affinity_for_whole_attempt():
    cfg = replace(config(), proxy_pool=("http://in1.example.test:8080", "http://in2.example.test:8080"), retry_count=1)
    attempt = TaskManager._config_for_attempt(cfg, 1, proxy_plan=cfg.proxy_pool)
    assert attempt.checkout_proxy == attempt.update_proxy == cfg.proxy_pool[1]


@pytest.mark.parametrize("after_confirm", [False, True])
def test_task_manager_does_not_retry_rejected_or_committed_upi(monkeypatch, after_confirm):
    calls = []
    def extract(cfg, *, cancel_event=None, stage_callback=None):
        calls.append(cfg)
        if after_confirm:
            stage_callback("checkout_committed")
        raise upi._failure("declined", retryable=after_confirm)
    manager = TaskManager(extractor=extract, max_workers=1)
    try:
        task = manager.create(replace(config(), retry_count=2))
        final = wait_for_task(lambda: manager.get(task["task_id"]))
        assert final["status"] == "failed" and len(calls) == 1
    finally:
        manager.close()


def test_upi_ui_uses_existing_email_parser_and_result_surfaces():
    html = (ROOT / "payment_link_extractor/web/templates/index.html").read_text(encoding="utf-8")
    source = (ROOT / "payment_link_extractor/web/static/app.js").read_text(encoding="utf-8")
    assert 'value="upi"' in html and 'id="upi-country-note"' in html
    assert "extractAccountEmail(token)" in source
    assert "开始 UPI 提炼" in source and 'isUpi ? "IN"' in source
    assert "renderUpiDetails(result)" in source and "result.upi_qr_png" in source


def test_http_api_accepts_at_and_india_proxy_and_returns_upi_result(bridged_core):
    from payment_link_extractor.web.app import create_app
    app = create_app({"TESTING": True, "WEB_PASSWORD": "test-password", "LOG_FILE": ""})
    client = app.test_client()
    headers = {"X-Workbench-Password": "test-password"}
    manager = app.extensions["payment_task_manager"]
    try:
        defaults = client.get("/api/defaults", headers=headers).get_json()
        assert {"value": "upi", "label": "UPI", "country": "IN", "currency": "INR"} in defaults["payment_methods"]
        response = client.post("/api/tasks", headers=headers, json={
            "access_token": token(), "payment_method": "upi", "proxy_pool": [config().checkout_proxy], "max_attempts": 1,
        })
        assert response.status_code == 202
        task_id = response.get_json()["task_id"]
        snapshot = wait_for_task(lambda: client.get("/api/tasks/" + task_id, headers=headers).get_json())
        assert snapshot["status"] == "succeeded"
        assert snapshot["account_email"] == "upi-test@example.test"
        assert snapshot["result"]["upi_url"] == success()["url"]
        assert snapshot["result"]["upi_verified"] is True
        assert token() not in json.dumps(snapshot)
        assert len(bridged_core) == 1 and bridged_core[0].poll() is not None
    finally:
        manager.close()
