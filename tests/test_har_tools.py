from __future__ import annotations

import json
import base64
from pathlib import Path

import pytest

from tools.har_capture import (
    HARRecorder,
    Socks5HttpBridge,
    check_socks5_proxy,
    infer_proxy_country,
    locale_profile_for_proxy,
)
from tools.har_utils import analyze_har, entry_summary, markdown_report


def _fixture() -> dict:
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "fixture", "version": "1"},
            "entries": [
                {
                    "startedDateTime": "2026-08-24T00:00:00.000Z",
                    "time": 12.5,
                    "request": {
                        "method": "POST",
                        "url": "https://chatgpt.com/backend-api/payments/checkout/confirm?access_token=at-fixture",
                        "headers": {
                            "Authorization": "Bearer at-fixture",
                            "oai-client-build-number": "9748354",
                            "Content-Type": "application/json",
                        },
                        "postData": {"text": '{"checkout_session_id":"oaics_fixture","secret":"raw"}'},
                    },
                    "response": {
                        "status": 200,
                        "headers": {"content-type": "application/json"},
                        "content": {
                            "mimeType": "application/json",
                            "text": '{"result":{"shortUrl":"https://m.gcash/s/fixture","qrCode":"secret-qr"}}',
                        },
                    },
                },
                {
                    "request": {
                        "method": "GET",
                        "url": "https://m.gcash.com/page",
                        "headers": {},
                    },
                    "response": {"status": 302, "headers": {}, "content": {"mimeType": "text/html"}},
                },
            ],
        }
    }


def test_analyze_har_extracts_contract_and_redacts_sensitive_values(tmp_path: Path) -> None:
    path = tmp_path / "fixture.har"
    path.write_text(json.dumps(_fixture()), encoding="utf-8")

    report = analyze_har(path)

    assert report["entry_count"] == 2
    assert report["selected_count"] == 2
    assert report["counts"]["statuses"] == {"200": 1, "302": 1}
    assert report["observations"]["oai_client_build_numbers"] == ["9748354"]
    assert report["observations"]["short_urls"] == ["https://m.gcash/s/fixture"]
    request = report["entries"][0]
    assert "Bearer at-fixture" not in json.dumps(request)
    assert "raw" not in json.dumps(request)
    assert request["path"].endswith("/confirm")


def test_analyze_har_filters_by_host_path_status_and_method(tmp_path: Path) -> None:
    path = tmp_path / "fixture.har"
    path.write_text(json.dumps(_fixture()), encoding="utf-8")

    report = analyze_har(path, host="m.gcash.com", method="GET", status=302)

    assert report["selected_count"] == 1
    assert report["entries"][0]["host"] == "m.gcash.com"
    assert report["entries"][0]["method"] == "GET"


def test_entry_summary_can_include_raw_values_and_markdown_is_reusable() -> None:
    entry = _fixture()["log"]["entries"][0]
    raw = entry_summary(0, entry, redact=False)
    assert "Bearer at-fixture" in json.dumps(raw)

    report = {"source": "fixture.har", "sha256": "abc", "entry_count": 1, "selected_count": 1, "counts": {}, "observations": {}, "entries": [raw]}
    markdown = markdown_report(report)
    assert "# HAR analysis" in markdown
    assert "checkout/confirm" in markdown


def test_socks5_bridge_allocates_local_http_endpoint() -> None:
    bridge = Socks5HttpBridge("socks5://127.0.0.1:1080:user:password")
    try:
        assert bridge.proxy_server.startswith("http://127.0.0.1:")
    finally:
        bridge.close()


def test_socks5_bridge_rejects_malformed_proxy() -> None:
    with pytest.raises(ValueError):
        Socks5HttpBridge("not-a-proxy")


def test_proxy_region_controls_locale_and_timezone_without_exposing_credentials() -> None:
    assert infer_proxy_country("proxy.example:3000:user-region-PH-sid-abc:pass") == "PH"
    country, profile = locale_profile_for_proxy("proxy.example:3000:user-region-PH-sid-abc:pass")
    assert country == "PH"
    assert profile == {"lang": "en-US", "accept_lang": "en-US,en", "timezone": "Asia/Manila"}
    unknown_country, unknown_profile = locale_profile_for_proxy("proxy.example:3000:user:pass")
    assert unknown_country == "UN"
    assert unknown_profile["lang"] == "en-US"


def test_proxy_check_uses_the_same_local_http_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeBridge:
        proxy_server = "http://127.0.0.1:4567"

        def __init__(self, value: str) -> None:
            assert value == "proxy.example:1080:user:pass"
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class FakeResponse:
        status = 204

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _: int) -> bytes:
            return b""

    class FakeOpener:
        def open(self, request: object, timeout: int) -> FakeResponse:
            assert timeout == 15
            return FakeResponse()

    monkeypatch.setattr("tools.har_capture.Socks5HttpBridge", FakeBridge)
    monkeypatch.setattr("tools.har_capture.build_opener", lambda *_: FakeOpener())
    assert check_socks5_proxy("proxy.example:1080:user:pass") == (True, "204", 0)


def test_capture_preserves_valid_base64_when_body_limit_truncates() -> None:
    recorder = HARRecorder(object(), max_body_bytes=3)  # type: ignore[arg-type]
    encoded = base64.b64encode(b"abcdef").decode("ascii")
    recorder._finish(
        {
            "request_event": {"wallTime": 0, "timestamp": 1},
            "request": {"method": "GET", "url": "https://example.com/file", "headers": {}},
            "response": {"status": 200, "mimeType": "application/octet-stream", "headers": {}},
            "body": encoded,
            "base64Encoded": True,
        },
        timestamp=1.1,
    )
    content = recorder.entries[0]["response"]["content"]
    assert content["encoding"] == "base64"
    assert base64.b64decode(content["text"]) == b"abc"
    assert content["size"] == 3
    assert content["_captureTruncated"] is True


def test_capture_flushes_in_flight_request_and_keeps_post_data() -> None:
    recorder = HARRecorder(object())  # type: ignore[arg-type]
    recorder.states["1"] = {
        "request_event": {"wallTime": 0, "timestamp": 1},
        "request": {
            "method": "POST",
            "url": "https://example.com/submit",
            "headers": {"content-type": "application/json"},
            "postData": '{"ok":true}',
        },
        "response": {"status": 0, "headers": {}},
        "body": "",
        "base64Encoded": False,
    }
    recorder.flush_pending()
    assert len(recorder.entries) == 1
    assert recorder.entries[0]["request"]["postData"]["text"] == '{"ok":true}'


def test_capture_recovers_post_data_omitted_by_request_event() -> None:
    recorder = HARRecorder(object())  # type: ignore[arg-type]

    def fake_command(method: str, params: dict | None = None) -> dict:
        if method == "Network.getRequestPostData":
            return {"postData": '{"checkout_session_id":"fixture"}'}
        if method == "Network.getResponseBody":
            return {"body": "{}", "base64Encoded": False}
        return {}

    recorder.command = fake_command  # type: ignore[method-assign]
    recorder.handle(
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "r1",
                "timestamp": 1,
                "wallTime": 0,
                "request": {
                    "method": "POST",
                    "url": "https://example.com/checkout",
                    "headers": {"content-type": "application/json"},
                },
            },
        }
    )
    recorder.handle(
        {
            "method": "Network.responseReceived",
            "params": {"requestId": "r1", "response": {"status": 200, "headers": {}}},
        }
    )
    recorder.handle({"method": "Network.loadingFinished", "params": {"requestId": "r1", "timestamp": 1.1}})
    assert recorder.entries[0]["request"]["postData"]["text"] == '{"checkout_session_id":"fixture"}'
