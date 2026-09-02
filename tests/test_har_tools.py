from __future__ import annotations

import json
import base64
from pathlib import Path

import pytest

from tools.har_capture import (
    HARRecorder,
    Socks5HttpBridge,
    audit_har_completeness,
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


def test_capture_flush_ignores_late_network_events_in_body_command() -> None:
    recorder = HARRecorder(object())  # type: ignore[arg-type]
    recorder.states["pending"] = {
        "request_id": "pending",
        "request_event": {"wallTime": 0, "timestamp": 1},
        "request": {"method": "GET", "url": "https://example.com/pending", "headers": {}},
        "response": {"status": 200, "mimeType": "application/json", "headers": {}},
        "body": "",
        "base64Encoded": False,
    }
    calls: list[str] = []

    def fake_command(method: str, params: dict | None = None) -> dict:
        calls.append(method)
        if method == "Network.getResponseBody":
            recorder.handle(
                {
                    "method": "Network.requestWillBeSent",
                    "params": {
                        "requestId": "late",
                        "timestamp": 2,
                        "wallTime": 0,
                        "request": {"method": "GET", "url": "https://example.com/late", "headers": {}},
                    },
                }
            )
            recorder.handle(
                {
                    "method": "Network.responseReceived",
                    "params": {
                        "requestId": "late",
                        "response": {"status": 200, "mimeType": "application/json", "headers": {}},
                    },
                }
            )
            return {"body": '{"saved":true}', "base64Encoded": False}
        return {}

    recorder.command = fake_command  # type: ignore[method-assign]
    recorder.flush_pending()
    assert calls == ["Network.getResponseBody"]
    assert recorder.states == {}
    assert len(recorder.entries) == 1
    assert recorder.entries[0]["response"]["content"]["text"] == '{"saved":true}'


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


def test_nested_cdp_command_preserves_outer_command_response() -> None:
    class FakeCDP:
        next_id = 0

        def __init__(self) -> None:
            self.sent: list[dict] = []
            self.messages = [
                {"method": "Network.loadingFinished", "params": {"requestId": "r1", "timestamp": 1.1}},
                {"id": 1, "result": {"outer": True}},
                {"id": 2, "result": {"body": '{"nested":true}', "base64Encoded": False}},
            ]

        def send_json(self, value: dict) -> None:
            self.sent.append(value)

        def recv_json(self) -> dict:
            return self.messages.pop(0)

    cdp = FakeCDP()
    recorder = HARRecorder(cdp, stream_responses=False)  # type: ignore[arg-type]
    recorder.states["r1"] = {
        "request_id": "r1",
        "request_event": {"wallTime": 0, "timestamp": 1},
        "request": {"method": "GET", "url": "https://example.com/data", "headers": {}},
        "response": {"status": 200, "mimeType": "application/json", "headers": {}},
        "error": "",
    }
    assert recorder.command("Outer.command") == {"outer": True}
    assert recorder.entries[0]["response"]["content"]["text"] == '{"nested":true}'
    assert [item["method"] for item in cdp.sent] == ["Outer.command", "Network.getResponseBody"]


def test_streamed_response_is_used_when_get_response_body_is_empty() -> None:
    recorder = HARRecorder(object(), response_body_retries=2)  # type: ignore[arg-type]
    raw = b'{"consult":"complete"}'

    def fake_command(method: str, params: dict | None = None) -> dict:
        if method == "Network.streamResourceContent":
            return {"bufferedData": base64.b64encode(raw).decode("ascii")}
        if method == "Network.getResponseBody":
            return {"body": "", "base64Encoded": False}
        return {}

    recorder.command = fake_command  # type: ignore[method-assign]
    recorder.handle(
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "r1",
                "timestamp": 1,
                "wallTime": 0,
                "request": {"method": "POST", "url": "https://example.com/mgw.htm", "headers": {}, "postData": "x=1"},
            },
        }
    )
    recorder.handle(
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "r1",
                "response": {
                    "status": 200,
                    "mimeType": "application/json",
                    "headers": {"content-length": str(len(raw))},
                },
            },
        }
    )
    recorder.handle({"method": "Network.loadingFinished", "params": {"requestId": "r1", "timestamp": 1.1}})
    content = recorder.entries[0]["response"]["content"]
    assert content["encoding"] == "base64"
    assert base64.b64decode(content["text"]) == raw
    assert recorder.entries[0]["_capture"]["responseBodySource"] == "Network.streamResourceContent"
    assert "responseBodyMissing" not in recorder.entries[0]["_capture"]


def test_fetch_interception_captures_body_before_continuing_response() -> None:
    recorder = HARRecorder(object(), stream_responses=False)  # type: ignore[arg-type]
    calls: list[str] = []

    def fake_command(method: str, params: dict | None = None) -> dict:
        calls.append(method)
        if method == "Fetch.getResponseBody":
            return {"body": '{"authorisation":"complete"}', "base64Encoded": False}
        if method == "Network.getResponseBody":
            raise AssertionError("Fetch body should avoid the Network fallback")
        return {}

    recorder.command = fake_command  # type: ignore[method-assign]
    recorder.handle(
        {
            "method": "Network.requestWillBeSent",
            "params": {
                "requestId": "network-1",
                "timestamp": 1,
                "wallTime": 0,
                "request": {"method": "POST", "url": "https://example.com/mgw.htm", "headers": {}, "postData": "x=1"},
            },
        }
    )
    recorder.handle(
        {
            "method": "Fetch.requestPaused",
            "params": {"requestId": "fetch-1", "networkId": "network-1", "responseStatusCode": 200},
        }
    )
    recorder.handle(
        {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "network-1",
                "response": {"status": 200, "mimeType": "application/json", "headers": {}},
            },
        }
    )
    recorder.handle(
        {"method": "Network.loadingFinished", "params": {"requestId": "network-1", "timestamp": 1.1}}
    )
    assert calls == ["Fetch.getResponseBody", "Fetch.continueRequest"]
    assert recorder.entries[0]["response"]["content"]["text"] == '{"authorisation":"complete"}'
    assert recorder.entries[0]["_capture"]["responseBodySource"] == "Fetch.getResponseBody"


def test_completeness_audit_reports_and_accepts_full_gcash_flow() -> None:
    markers = [
        "/backend-api/payments/checkout/taxes",
        "/backend-api/payments/checkout/confirm",
        "/backend-api/payments/checkout/custom_payment_method/start",
        "/backend-api/sentinel/req",
        "/c4/v3/key-agreement/handshake",
        "ap.mobilewallet.gka.authorisation.stateless.consult",
        "ap.mobilewallet.short.dynamic.link",
        "ap.mobilewallet.gka.query.result",
    ]
    entries = []
    for index, marker in enumerate(markers):
        url = "https://example.com/mgw.htm" if marker.startswith("ap.mobilewallet") else "https://example.com" + marker
        entries.append(
            {
                "request": {"method": "POST", "url": url, "postData": {"text": marker + "=fixture"}},
                "response": {"status": 200, "content": {"text": '{"ok":true}'}, "headers": []},
                "_capture": {"responseBodySource": "Network.getResponseBody"},
            }
        )
    har = {"log": {"entries": entries, "_capture": {}}}
    complete = audit_har_completeness(har)
    assert complete["complete"] is True
    assert complete["channel"] == "gcash"
    entries[2]["response"]["content"] = {}
    entries[2]["_capture"]["responseBodyMissing"] = True
    incomplete = audit_har_completeness(har)
    assert incomplete["complete"] is False
    assert "custom_payment_method_start:response_body_missing" in incomplete["issues"]


def _gopay_fixture(*, include_redirect: bool = True) -> dict:
    """A complete GoPay extraction corpus (stripe init/confirm/elements/poll +
    chatgpt checkout/taxes/snapshot/approve)."""
    gopay_checkpoints = [
        ("POST", "https://chatgpt.com/backend-api/payments/checkout", '{"plan":"plus","promo":true}'),
        ("POST", "https://chatgpt.com/backend-api/payments/checkout/taxes", '{"checkout_session_id":"cs_live_x"}'),
        ("POST", "https://chatgpt.com/backend-api/payments/checkout/snapshot", '{"snapshot":{}}'),
        ("POST", "https://api.stripe.com/v1/payment_pages/cs_live_x/init", "expected_amount=0&key=pk_live_x"),
        ("GET", "https://api.stripe.com/v1/elements/sessions?key=pk_live_x", ""),
        ("POST", "https://api.stripe.com/v1/payment_pages/cs_live_x/confirm", "expected_amount=0&payment_method_data[type]=gopay"),
        ("POST", "https://chatgpt.com/backend-api/payments/checkout/approve", '{"checkout_session_id":"cs_live_x"}'),
        ("GET", "https://api.stripe.com/v1/payment_pages/cs_live_x", ""),
    ]
    entries = []
    for method, url, body in gopay_checkpoints:
        entry = {
            "request": {"method": method, "url": url, "postData": {"text": body}},
            "response": {"status": 200, "content": {"text": '{"result":"approved"}'}, "headers": []},
            "_capture": {"responseBodySource": "Network.getResponseBody"},
        }
        if method == "GET":
            entry["request"].pop("postData")
        entries.append(entry)
    if include_redirect:
        entries.append(
            {
                "request": {
                    "method": "GET",
                    "url": "https://pm-redirects.stripe.com/authorize/pm_pay_page-session?client_reference_id=x",
                },
                "response": {
                    "status": 200,
                    "content": {"text": "<html>gopay authorize</html>"},
                    "headers": [],
                },
                "_capture": {"responseBodySource": "Network.getResponseBody"},
            }
        )
    return {"log": {"entries": entries, "_capture": {}}}


def test_gopay_audit_accepts_bodyless_204_snapshot() -> None:
    har = _gopay_fixture()
    snapshot = next(
        entry for entry in har["log"]["entries"]
        if entry["request"]["url"].endswith("/backend-api/payments/checkout/snapshot")
    )
    snapshot["response"] = {"status": 204, "content": {}}
    snapshot["_capture"] = {"responseBodyMissing": True}
    audit = audit_har_completeness(har)
    assert "gopay_checkout_snapshot:response_body_missing" not in audit["issues"]
    assert audit["complete"] is True


def test_momo_audit_uses_vietnam_gateway_checkpoints() -> None:
    urls = [
        ("POST", "https://chatgpt.com/backend-api/payments/checkout", '{"x":1}'),
        ("POST", "https://chatgpt.com/backend-api/payments/checkout/taxes", '{"x":1}'),
        ("GET", "https://api.stripe.com/v1/elements/sessions", ""),
        ("POST", "https://api.stripe.com/v1/confirmation_tokens", '{"x":1}'),
        ("POST", "https://chatgpt.com/backend-api/payments/checkout/confirm", '{"x":1}'),
        ("POST", "https://api.stripe.com/v1/payment_intents/pi_fixture/confirm", '{"x":1}'),
        ("GET", "https://payment.momo.vn/v2/gateway/pay", ""),
        ("POST", "https://payment.momo.vn/v2/gateway/querySession", "session=fixture"),
    ]
    entries = [
        {
            "request": {"method": method, "url": url, "postData": {"text": body} if body else {}},
            "response": {"status": 200, "content": {"text": "{}"}},
        }
        for method, url, body in urls
    ]
    audit = audit_har_completeness({"log": {"entries": entries}})
    assert audit["channel"] == "momo"
    assert audit["complete"] is True
    assert audit["issues"] == []


def test_momo_audit_accepts_bodyless_browser_query_session_poll() -> None:
    entries = [
        {
            "request": {"method": "GET", "url": "https://payment.momo.vn/v2/gateway/pay"},
            "response": {"status": 200, "content": {"text": "{}"}},
        },
        {
            "request": {"method": "POST", "url": "https://payment.momo.vn/v2/gateway/querySession"},
            "response": {"status": 200, "content": {"text": "{\"status\":1000}"}},
        },
    ]
    audit = audit_har_completeness({"log": {"entries": entries}})
    assert audit["channel"] == "momo"
    assert "momo_query_session:request_body_missing" not in audit["issues"]


def test_momo_audit_accepts_setup_intent_confirmation() -> None:
    entries = [
        {
            "request": {"method": "POST", "url": "https://chatgpt.com/backend-api/payments/checkout", "postData": {"text": "{}"}},
            "response": {"status": 200, "content": {"text": "{}"}},
        },
        {
            "request": {"method": "POST", "url": "https://api.stripe.com/v1/setup_intents/seti_fixture/confirm", "postData": {"text": "{}"}},
            "response": {"status": 200, "content": {"text": "{}"}},
        },
        {
            "request": {"method": "GET", "url": "https://payment.momo.vn/v2/gateway/pay"},
            "response": {"status": 200, "content": {"text": "{}"}},
        },
        {
            "request": {"method": "POST", "url": "https://payment.momo.vn/v2/gateway/querySession"},
            "response": {"status": 200, "content": {"text": "{}"}},
        },
    ]
    audit = audit_har_completeness({"log": {"entries": entries}})
    assert "momo_stripe_confirm:entry_missing" not in audit["issues"]


def test_gopay_audit_classifies_and_accepts_full_gopay_flow() -> None:
    har = _gopay_fixture(include_redirect=True)
    complete = audit_har_completeness(har)
    assert complete["channel"] == "gopay"
    assert complete["complete"] is True
    assert complete["issues"] == []
    assert complete["gopayRedirect"]["found"] is True
    assert complete["gopayRedirect"]["sha256"]


def test_gopay_audit_reports_missing_redirect_and_no_gopay_sentinel_gate() -> None:
    har = _gopay_fixture(include_redirect=False)
    incomplete = audit_har_completeness(har)
    assert incomplete["channel"] == "gopay"
    assert incomplete["complete"] is False
    assert "gopay_redirect:entry_missing" in incomplete["issues"]
    # Sentinel is minted outside the browser; it must never gate GoPay captures.
    assert all("sentinel_req" not in issue for issue in incomplete["issues"])


def test_gopay_audit_rejects_confusable_elements_response_as_approve() -> None:
    """The /elements/sessions GET must not satisfy the approve POST checkpoint."""
    har = _gopay_fixture(include_redirect=True)
    approve = har["log"]["entries"][6]
    approve["request"]["method"] = "GET"
    approve["request"]["url"] = "https://api.stripe.com/v1/elements/sessions"
    approve["request"]["postData"] = {}
    audit = audit_har_completeness(har)
    assert audit["channel"] == "gopay"
    assert "gopay_approve:entry_missing" in audit["issues"]


def test_gopay_observations_capture_redirect_and_methods_from_all_sources(tmp_path: Path) -> None:
    """Redirect detection must span request url / redirect header / response body,
    and stripe init metadata (payment_method_types, amount) must be extracted."""
    har = _gopay_fixture(include_redirect=True)
    entries = har["log"]["entries"]
    # rotate coverage: entry 3 keeps an init body carrying methods + amount,
    # entry 7's redirect is delivered via a 302 Location header with no body.
    entries[3]["response"]["content"]["text"] = json.dumps(
        {"payment_method_types": ["gopay", "card"], "amount_total": 174211}
    )
    entries[7]["request"]["url"] = "https://chatgpt.com/backend-api/payments/checkout/approve"
    entries[7]["response"] = {
        "status": 302,
        "headers": [{"name": "location", "value": "https://pm-redirects.stripe.com/authorize/pm_pay_page-session"}],
        "content": {"text": ""},
    }
    har_path = tmp_path / "gopay.har"
    har_path.write_text(json.dumps(har, ensure_ascii=False), encoding="utf-8")
    report = analyze_har(har_path)
    observations = report["observations"]
    assert len(observations["gopay_redirects"]) >= 2
    assert all(item["sha256"] and item["host"] == "pm-redirects.stripe.com" for item in observations["gopay_redirects"])
    assert {"gopay", "card"} <= set(observations["gopay_payment_methods"])
    assert "174211" in observations["gopay_amounts"]
    assert any(
        item["checkpoint"] == "gopay_stripe_init" and item["path"].endswith("/init")
        for item in observations["gopay_stripe_init"]
    )
