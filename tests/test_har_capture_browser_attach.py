from __future__ import annotations

from collections import deque

from tools.har_capture_browser_attach import BrowserConnection


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
