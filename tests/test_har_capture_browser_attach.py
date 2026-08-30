from __future__ import annotations

from collections import deque

from tools.har_capture_browser_attach import (
    AUTO_ATTACH_PARAMS,
    BrowserConnection,
    CHILD_AUTO_ATTACH_PARAMS,
    TargetRecorder,
    _capture_completeness_audit,
    build_parser,
)


def test_browser_connection_dispatches_flattened_target_events() -> None:
    connection = BrowserConnection.__new__(BrowserConnection)
    connection._queues = {"session-a": deque([{"method": "Network.loadingFinished"}])}
    connection._global = deque()
    session_id, message = connection.recv_any()
    assert session_id == "session-a"
    assert message["method"] == "Network.loadingFinished"


def test_browser_connection_prioritizes_target_lifecycle_events() -> None:
    connection = BrowserConnection.__new__(BrowserConnection)
    connection._queues = {"session-a": deque([{"method": "Network.requestWillBeSent"}])}
    connection._global = deque(
        [{"method": "Target.attachedToTarget", "params": {"sessionId": "session-a"}}]
    )
    session_id, message = connection.recv_any()
    assert session_id == ""
    assert message["method"] == "Target.attachedToTarget"


def test_browser_capture_defaults_to_nonblocking_streaming() -> None:
    args = build_parser().parse_args(["--cdp-port", "60943", "--output", "capture.har"])
    assert args.fetch_responses is False
    assert args.no_fetch_responses is False
    assert args.heartbeat_seconds == 5.0
    assert args.duration == 0


def test_target_enable_pauses_new_targets_until_network_is_ready() -> None:
    calls: list[tuple[str, dict | None]] = []

    class FakeRecorder:
        max_body_bytes = 1024

        def command(self, method: str, params: dict | None = None) -> dict:
            calls.append((method, params))
            return {}

    target = TargetRecorder.__new__(TargetRecorder)
    target.recorder = FakeRecorder()
    target.fetch_responses = False
    target.enable()
    assert AUTO_ATTACH_PARAMS["waitForDebuggerOnStart"] is True
    assert CHILD_AUTO_ATTACH_PARAMS["waitForDebuggerOnStart"] is False
    assert ("Target.setAutoAttach", CHILD_AUTO_ATTACH_PARAMS) in calls
    assert calls[-1] == ("Runtime.runIfWaitingForDebugger", None)


def test_capture_audit_accepts_bodyless_snapshot_and_reports_missing_stripe_targets() -> None:
    def entry(url: str, method: str, status: int, request_body: str = "", response_body: str = "") -> dict:
        value = {
            "request": {"url": url, "method": method},
            "response": {"status": status, "content": {}},
        }
        if request_body:
            value["request"]["postData"] = {"text": request_body}
        if response_body:
            value["response"]["content"] = {"text": response_body}
        return value

    har = {
        "log": {
            "entries": [
                entry("https://chatgpt.com/backend-api/payments/checkout", "POST", 200, "{}", "{}"),
                entry("https://chatgpt.com/backend-api/payments/checkout/taxes", "POST", 200, "{}", "{}"),
                entry("https://chatgpt.com/backend-api/payments/checkout/snapshot", "POST", 204, "{}"),
                entry("https://chatgpt.com/backend-api/payments/checkout/approve", "POST", 200, "{}", "{}"),
                entry("https://pm-redirects.stripe.com/authorize/fixture", "GET", 302),
            ]
        }
    }
    audit = _capture_completeness_audit(har)
    assert audit["channel"] == "gopay"
    assert "gopay_checkout_snapshot:response_body_missing" not in audit["issues"]
    assert "gopay_redirect:response_body_missing" not in audit["issues"]
    assert "gopay_stripe_init:entry_missing" in audit["issues"]
