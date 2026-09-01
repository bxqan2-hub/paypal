from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from pathlib import Path

from tools.roxy_mitm_control import (
    cleanup_stale_mitmweb,
    create_roxy_window,
    merge_hybrid_har,
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
