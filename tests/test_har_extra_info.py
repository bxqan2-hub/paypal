from __future__ import annotations

from tools.har_capture import HARRecorder


def test_extra_info_headers_and_associated_cookies_are_preserved() -> None:
    recorder = HARRecorder(object(), stream_responses=False)  # type: ignore[arg-type]

    def fake_command(method: str, params: dict | None = None) -> dict:
        if method == "Network.getResponseBody":
            return {"body": '{"ok":true}', "base64Encoded": False}
        return {}

    recorder.command = fake_command  # type: ignore[method-assign]
    recorder.handle(
        {
            "method": "Network.requestWillBeSentExtraInfo",
            "params": {
                "requestId": "r1",
                "headers": {"x-wire": "present", "cookie": "sid=fixture"},
                "associatedCookies": [
                    {"cookie": {"name": "sid", "value": "fixture", "domain": "chatgpt.com", "path": "/"}}
                ],
            },
        }
    )
    recorder.handle(
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "r1",
                "timestamp": 1,
                "wallTime": 0,
                "request": {
                    "method": "GET",
                    "url": "https://chatgpt.com/backend-api/payments/checkout",
                    "headers": {"x-event-only": "kept"},
                },
            },
        }
    )
    recorder.handle(
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "r1",
                "response": {"status": 200, "mimeType": "application/json", "headers": {}},
            },
        }
    )
    recorder.handle(
        {
            "method": "Network.responseReceivedExtraInfo",
            "params": {"requestId": "r1", "headers": {"x-response-wire": "yes"}},
        }
    )
    recorder.handle({"method": "Network.loadingFinished", "params": {"requestId": "r1", "timestamp": 1.1}})

    entry = recorder.entries[0]
    request_headers = {item["name"].lower(): item["value"] for item in entry["request"]["headers"]}
    response_headers = {item["name"].lower(): item["value"] for item in entry["response"]["headers"]}
    assert request_headers == {"x-wire": "present", "cookie": "sid=fixture", "x-event-only": "kept"}
    assert response_headers == {"x-response-wire": "yes"}
    assert entry["request"]["cookies"][0]["name"] == "sid"
    assert entry["request"]["cookies"][0]["value"] == "fixture"
