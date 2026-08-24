from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import payment_link_extractor.mk_gcash as mk
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


def test_complete_upstream_project_is_copied_into_site_folder():
    project = ROOT / "payment_link_extractor" / "mk_gcash_open_source"
    assert project == mk.MK_GCASH_PROJECT_DIR
    assert (project / "app.py").is_file()
    assert (project / "web" / "index.html").is_file()
    assert (project / "tests" / "test_app.py").is_file()
    assert len([path for path in project.rglob("*") if path.is_file() and "__pycache__" not in path.parts]) == 22
    manifest = json.loads((ROOT / "mk_gcash_project_manifest.json").read_text(encoding="utf-8"))
    assert manifest["tracked_file_count"] == 22
    for relative, expected in manifest["sha256"].items():
        assert hashlib.sha256((project / relative).read_bytes()).hexdigest() == expected


def test_vendored_mk_gcash_core_matches_upstream_manifest():
    manifest = json.loads((ROOT / "mk_gcash_core_manifest.json").read_text(encoding="utf-8"))
    assert manifest["commit"] == "2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5"
    for relative, expected in manifest["sha256"].items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected
        if relative not in {"sentinel.py", "sentinel_bridge.js"}:
            assert hashlib.sha256((mk.MK_GCASH_PROJECT_DIR / relative).read_bytes()).hexdigest() == expected


def test_gcash_sentinel_launches_hide_windows_console():
    sentinel = (mk.MK_GCASH_PROJECT_DIR / "sentinel.py").read_text(encoding="utf-8")
    bridge = (mk.MK_GCASH_PROJECT_DIR / "sentinel_bridge.js").read_text(encoding="utf-8")
    assert "CREATE_NO_WINDOW" in sentinel
    assert "windowsHide: true" in bridge


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


class FakeUpstreamApp:
    def __init__(self, status="success"):
        self.status = status
        self.payload = None
        self.cancelled = False
        self.CHAIN_MANAGER = SimpleNamespace(get_tasks=lambda _job: [self.raw])
        self.raw = {
            "client_account_id": "acct_fixture",
            "status": status,
            "current_step": "follow_redirect",
            "checkout_session_id": "oaics_fixture",
            "payment_method_id": "cpmt_fixture",
            "checkout_amount": 1000,
            "adyen_url": "https://checkoutshopper-live.adyen.com/fixture",
            "gcash_url": "https://m.gcash.com/gcash-login-web/index.html?netAuthId=fixture",
            "payment_route": "adyen_redirect",
            "qr_text": "qr-fixture",
            "qr_short": "https://short.fixture",
            "net_auth_id": "fixture",
            "qr_expires_at": 123,
            "monitor_id": "monitor-fixture",
            "callback_status": "waiting_scan",
            "error_message": "upstream failed" if status != "success" else "",
        }

    def create_job(self, payload):
        self.payload = payload
        return {"job_id": "local_fixture", "accounts": [{"id": "acct_fixture"}]}

    def public_job(self, _job_id):
        return {
            "accounts": [{
                "id": "acct_fixture",
                "email": "explicit@example.com",
                "name": "Explicit Name",
                "status": self.status,
                "current_step": self.raw["current_step"],
                "link": self.raw["gcash_url"],
                "error": self.raw["error_message"],
            }],
        }

    def cancel_job(self, _job_id):
        self.cancelled = True


def test_direct_upstream_app_call_maps_result_and_payload(monkeypatch):
    fake = FakeUpstreamApp()
    monkeypatch.setattr(mk, "_UPSTREAM_APP", fake)
    stages = []
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="proxy.example:8080:user:pass",
        update_proxy="",
        payment_method="gcash",
        proxy_pool=("proxy.example:8080:user:pass", "proxy2.example:8080:user:pass"),
        account_email="explicit@example.com",
        account_name="Explicit Name",
        retry_count=4,
    )
    result = extract_mk_gcash_payment_link(config, stage_callback=stages.append)
    assert fake.payload["proxy_pool"] == ["proxy.example:8080:user:pass", "proxy2.example:8080:user:pass"]
    assert fake.payload["max_attempts"] == 5
    assert result.checkout_session_id == "oaics_fixture"
    assert result.payment_method_id == "cpmt_fixture"
    assert result.provider_value.startswith("https://m.gcash.com/")
    assert result.billing.to_dict()["name"] == "Explicit Name"
    assert result.extra["mk_gcash_project_dir"] == str(mk.MK_GCASH_PROJECT_DIR)
    assert stages == ["redirect_resolution", "completed"]


def test_direct_upstream_failure_is_propagated(monkeypatch):
    fake = FakeUpstreamApp(status="failed")
    monkeypatch.setattr(mk, "_UPSTREAM_APP", fake)
    with pytest.raises(ProtocolError, match="upstream failed"):
        extract_mk_gcash_payment_link(gcash_config())


def test_direct_upstream_cancellation_calls_cancel(monkeypatch):
    fake = FakeUpstreamApp()
    monkeypatch.setattr(mk, "_UPSTREAM_APP", fake)
    event = SimpleNamespace(is_set=lambda: True)
    with pytest.raises(ExtractionCancelled):
        extract_mk_gcash_payment_link(gcash_config(), cancel_event=event)
    assert fake.cancelled is True


def test_upstream_loader_points_at_copied_app(monkeypatch):
    monkeypatch.setattr(mk, "_UPSTREAM_APP", None)
    app = mk._load_upstream_app()
    assert Path(app.__file__).resolve() == (mk.MK_GCASH_PROJECT_DIR / "app.py").resolve()
    assert Path(__import__("gcash_chain").__file__).resolve() == (mk.MK_GCASH_PROJECT_DIR / "gcash_chain.py").resolve()
    assert getattr(__import__("gcash_chain").GCashChain, "_site_timeout_hook", False) is True
