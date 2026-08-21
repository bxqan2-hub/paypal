from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "paypal_agreement_protocol"
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))

from paypal.models import SessionState  # noqa: E402
from paypal.proxy import ProxyConfig, ProxyEntry  # noqa: E402
from paypal.session import PayPalSession, PayPalTransportError  # noqa: E402
import web as protocol_web  # noqa: E402


PROXY = "http://probe-user:probe-pass@proxy.example:8080"


def test_proxy_entry_stable_id_is_deterministic_and_secret_free() -> None:
    first = ProxyEntry.parse(PROXY)
    second = ProxyEntry.parse(PROXY)
    changed = ProxyEntry.parse("http://probe-user:other-pass@proxy.example:8080")

    assert first.stable_id == second.stable_id
    assert first.stable_id != changed.stable_id
    assert len(first.stable_id) == 12
    assert "probe-pass" not in first.stable_id


def test_retry_phone_kind_distinguishes_reuse_from_rotation() -> None:
    assert protocol_web.retry_phone_kind("+447700900123", "+44 7700 900123") == (
        "reused current phone",
        True,
    )
    assert protocol_web.retry_phone_kind("+447700900123", "+447700900124") == (
        "new phone",
        False,
    )


def test_job_id_is_used_for_shared_task_log_context() -> None:
    record = {"extra": {"task_id": "-", "job_id": "job-123"}}
    assert protocol_web is not None
    # Import the filter through the shared logging module so this test covers
    # the actual sink configuration rather than a protocol-only workaround.
    from payment_link_extractor.logging_utils import _task_context_filter

    assert _task_context_filter(record) is True
    assert record["extra"]["task_id"] == "job-123"


def test_proxy_probe_uses_curl_transport_when_business_engine_is_curl() -> None:
    calls: list[dict] = []

    class Response:
        status_code = 302

    class FakeCurlSession:
        def __init__(self, **kwargs):
            calls.append({"init": kwargs})

        def get(self, url, **kwargs):
            calls.append({"url": url, **kwargs})
            return Response()

        def close(self):
            calls.append({"close": True})

    config = ProxyConfig(enabled=True, entry=ProxyEntry.parse(PROXY))
    with (
        patch.object(protocol_web, "CurlSession", FakeCurlSession),
        patch.object(protocol_web, "CurlRequestsError", None),
        patch.dict(os.environ, {"PAYPAL_HTTP_ENGINE": "curl_cffi"}, clear=False),
    ):
        ok, reason = protocol_web.proxy_probe(config, timeout_seconds=1.5)

    assert ok is True
    assert "HTTP 302" in reason
    assert calls[0]["init"]["impersonate"] == os.getenv("PAYPAL_CURL_IMPERSONATE", "chrome")
    request = next(item for item in calls if "url" in item)
    assert request["url"] == "https://www.paypal.com/"
    assert request["timeout"] == 1.5
    assert request["allow_redirects"] is False


def test_proxy_probe_switches_on_curl_transport_error() -> None:
    class FakeCurlSession:
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            from curl_cffi.requests import RequestsError

            raise RequestsError("Failed to perform, curl: (28) timed out")

        def close(self):
            pass

    config = ProxyConfig(enabled=True, entry=ProxyEntry.parse(PROXY))
    with (
        patch.object(protocol_web, "CurlSession", FakeCurlSession),
        patch.dict(os.environ, {"PAYPAL_HTTP_ENGINE": "curl_cffi"}, clear=False),
    ):
        ok, reason = protocol_web.proxy_probe(config)

    assert ok is False
    assert "28" in reason


def test_proxy_probe_surfaces_configuration_errors_instead_of_rotating() -> None:
    class FakeCurlSession:
        def __init__(self, **kwargs):
            pass

        def get(self, url, **kwargs):
            raise ValueError("invalid local proxy setup")

        def close(self):
            pass

    config = ProxyConfig(enabled=True, entry=ProxyEntry.parse(PROXY))
    with (
        patch.object(protocol_web, "CurlSession", FakeCurlSession),
        patch.dict(os.environ, {"PAYPAL_HTTP_ENGINE": "curl_cffi"}, clear=False),
    ):
        with pytest.raises(protocol_web.ProxyPreflightConfigurationError):
            protocol_web.proxy_probe(config)


def test_session_wraps_transport_failure_without_exposing_query_parameters() -> None:
    session = PayPalSession(
        SessionState(),
        country="GB",
        locale="en_GB",
    )

    class FailingClient:
        def get(self, url, **kwargs):
            raise httpx.ConnectTimeout("curl: (28) timeout")

    original_client = session.client
    session.client = FailingClient()
    try:
        with pytest.raises(PayPalTransportError) as caught:
            session.get("https://www.paypal.com/graphql?token=secret-value")
    finally:
        original_client.close()

    error = caught.value
    assert error.error_code == "PAYPAL_TRANSPORT_ERROR"
    assert error.transport_code == "curl_28"
    assert "secret-value" not in str(error)
    assert "https://www.paypal.com/graphql" in str(error)


def test_protocol_transport_retry_switches_proxy_without_consuming_phone() -> None:
    first = "http://a:pass-a@proxy-a.example:8001"
    second = "http://b:pass-b@proxy-b.example:8002"
    initial = ProxyConfig(enabled=True, entry=ProxyEntry.parse(first))
    job = protocol_web.WebJob(
        id="transport-retry-fixture",
        owner_device_id="device-fixture",
        ba_token="BA-TRANSPORTFIXTURE01",
        phone="+447700900123",
        country="GB",
        max_retries=1,
        proxy_enabled=True,
        proxy_label=initial.label,
        _proxy_config=initial,
        _proxy_pool=[first, second],
    )
    selected: list[ProxyEntry] = []

    class FakeFlow:
        def __init__(self, **kwargs):
            selected.append(kwargs["proxy_config"].entry)

        def run(self):
            if len(selected) == 1:
                raise PayPalTransportError(
                    "POST",
                    "https://www.paypal.com/graphql?token=secret",
                    httpx.ConnectTimeout("curl: (28) timeout"),
                )
            return {"status": "success", "return_url": "https://merchant.fixture/return?status=success"}

    with (
        patch.object(protocol_web, "find_authorization_checkpoint", return_value=None),
        patch.object(protocol_web.random, "shuffle", side_effect=lambda values: None),
        patch.object(protocol_web, "proxy_probe", return_value=(True, "HTTP 302")),
        patch.object(protocol_web, "generate_card"),
        patch.object(protocol_web, "WebIdentityElevationPayPalFlow", FakeFlow),
        patch.object(protocol_web, "_authorization_checkpoint_from_result", return_value=None),
        patch.object(protocol_web, "record_protocol_metric"),
        patch.object(protocol_web, "record_payment_audit"),
    ):
        # The fake card is only inspected by the flow constructor in this
        # fixture, so avoid invoking the real card source.
        protocol_web.generate_card.return_value = type("Card", (), {
            "number": "4111111111111111",
            "expiry": "12/2030",
            "cvv": "123",
            "card_type": "Visa",
        })()
        protocol_web.run_job(job)

    assert selected == [ProxyEntry.parse(first), ProxyEntry.parse(second)]
    assert job.phone == "+447700900123"
    assert job.retry_count == 1
    assert job.status == "completed"
    assert any(event["type"] == "protocol.retry.phone_reused" for event in job.events)
