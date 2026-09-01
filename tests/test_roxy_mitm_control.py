from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tools.roxy_mitm_control import cleanup_stale_mitmweb, create_roxy_window, resolve_workspace_id


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
