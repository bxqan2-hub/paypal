from __future__ import annotations

import json
import inspect
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from tools.roxy_mitm_control import (
    CaptureState,
    cleanup_stale_mitmweb,
    close_cdp_browser,
    create_roxy_window,
    force_stop_process_tree,
    HTML,
    merge_hybrid_har,
    request_cdp_stop,
    resolve_workspace_id,
)


class FixtureHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(
            json.dumps({"code": 0, "data": {"rows": [{"id": "143859"}]}}).encode()
        )

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length))
        self.requests.append({"path": self.path, "token": self.headers.get("token"), "payload": payload})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"code": 0, "data": {"dirId": "new-dir"}}).encode())


def test_roxy_workspace_and_window_payload() -> None:
    FixtureHandler.requests.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        workspace_id = resolve_workspace_id(base_url, "secret")
        dir_id = create_roxy_window(base_url, "secret", workspace_id, "MITM-GOPAY", 8899)
    finally:
        server.shutdown()
        server.server_close()
    assert workspace_id == "143859"
    assert dir_id == "new-dir"
    request = FixtureHandler.requests[0]
    assert request["token"] == "secret"
    payload = request["payload"]
    assert payload["randomFingerprint"] is True
    assert payload["proxyInfo"] == {
        "proxyMethod": "custom",
        "proxyCategory": "HTTP",
        "ipType": "IPV4",
        "protocol": "HTTP",
        "host": "127.0.0.1",
        "port": "8899",
        "proxyUserName": "",
        "proxyPassword": "",
    }


def test_cleanup_stale_mitmweb_is_noop_off_windows(monkeypatch) -> None:
    monkeypatch.setattr("tools.roxy_mitm_control.os.name", "posix")
    cleanup_stale_mitmweb((8899, 8081))


def test_request_cdp_stop_uses_marker_without_ctrl_break(tmp_path: Path) -> None:
    class Process:
        def poll(self) -> None:
            return None

        def send_signal(self, _signal: object) -> None:
            raise AssertionError("CDP stop must not send CTRL_BREAK_EVENT")

    marker = tmp_path / "capture-cdp.stop"
    request_cdp_stop(Process(), marker)
    assert marker.read_text(encoding="ascii") == "stop"


def test_discard_closes_window_and_removes_capture_files(tmp_path: Path, monkeypatch) -> None:
    output = tmp_path / "momo-roxy-mitm.har"
    cdp_output = tmp_path / "momo-roxy-mitm-cdp.har"
    paths = [
        output,
        output.with_suffix(".mitm"),
        output.with_suffix(".report.md"),
        output.with_suffix(".summary.md"),
        output.with_suffix(".stop"),
        cdp_output,
        cdp_output.with_name(f".{cdp_output.name}.checkpoint"),
        cdp_output.with_suffix(".stop"),
    ]
    for path in paths:
        path.write_text("fixture", encoding="utf-8")

    class Process:
        pid = 123

        def poll(self) -> None:
            return None

    killed: list[object] = []
    closed: list[int | None] = []
    monkeypatch.setattr("tools.roxy_mitm_control.force_stop_process_tree", lambda process: killed.append(process))
    monkeypatch.setattr("tools.roxy_mitm_control.close_cdp_browser", lambda port: closed.append(port))
    monkeypatch.setattr("tools.roxy_mitm_control.cleanup_stale_mitmweb", lambda _ports: None)
    state = CaptureState(root=tmp_path, proxy_port=8899, web_port=8081)
    state.process = Process()  # type: ignore[assignment]
    state.cdp_process = Process()  # type: ignore[assignment]
    state.output = str(output)
    state.stop_file = output.with_suffix(".stop")
    state.cdp_stop_file = cdp_output.with_suffix(".stop")
    state.cdp_output = cdp_output
    state.cdp_port = 62345

    result = state.discard()

    assert result["message"] == "本次抓包已放弃，窗口已关闭，未保存文件"
    assert result["running"] is False
    assert killed == [state.cdp_process, state.process] or len(killed) == 2
    assert closed == [62345]
    assert all(not path.exists() for path in paths)
    assert "CAPTURE_DISCARDED=1" in result["logs"]


def test_control_panel_exposes_discard_action() -> None:
    assert 'id="discard"' in HTML
    assert "/api/discard" in HTML


def test_startup_failures_use_discard_path() -> None:
    source = inspect.getsource(CaptureState.start)
    assert source.count("self.discard()") == 2
    assert "self.stop()" not in source


def test_hybrid_merge_only_supplements_tls_passthrough_hosts(tmp_path: Path, monkeypatch) -> None:
    def entry(url: str, marker: str) -> dict[str, object]:
        return {
            "startedDateTime": f"2026-09-01T00:00:0{marker}Z",
            "request": {"method": "POST", "url": url, "postData": {"text": "{}"}},
            "response": {"status": 200, "content": {"text": "{}"}},
        }

    mitm_output = tmp_path / "capture.har"
    cdp_output = tmp_path / "capture-cdp.har"
    mitm_output.write_text(
        json.dumps({"log": {"entries": [entry("https://api.stripe.com/v1/payment_pages/test/init", "2")]}}),
        encoding="utf-8",
    )
    cdp_output.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        entry("https://chatgpt.com/backend-api/payments/checkout", "1"),
                        entry("https://api.stripe.com/v1/payment_pages/test/init", "3"),
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("tools.roxy_mitm_control.audit_har_completeness", lambda _har: {"complete": True})

    def fake_finalize(output: Path, _channel: str) -> dict[str, object]:
        har = json.loads(output.read_text(encoding="utf-8"))
        return {"entries": len(har["log"]["entries"]), "audit": {"complete": True}}

    monkeypatch.setattr("tools.roxy_mitm_control.finalize_capture", fake_finalize)
    result = merge_hybrid_har(mitm_output, cdp_output, "gopay")
    merged = json.loads(mitm_output.read_text(encoding="utf-8"))
    entries = merged["log"]["entries"]

    assert result["entries"] == 2
    assert [item["request"]["url"] for item in entries] == [
        "https://chatgpt.com/backend-api/payments/checkout",
        "https://api.stripe.com/v1/payment_pages/test/init",
    ]
    assert entries[0]["_capture"]["source"] == "roxy-cdp-supplement"
    assert merged["log"]["_capture"]["recorder"] == "mitmproxy+roxy-cdp"
    assert merged["log"]["_capture"]["cdpSupplementEntryCount"] == 1
    assert not cdp_output.exists()
