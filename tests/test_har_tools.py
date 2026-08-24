from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.har_capture import Socks5HttpBridge
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
