from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "paypal_agreement_protocol"
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))

import web as protocol_web  # noqa: E402
from paypal.models import CardInfo  # noqa: E402
from paypal.proxy import ProxyConfig, ProxyEntry  # noqa: E402


PROXY_A = "http://user-a:pass-a@proxy-a.example:8001"
PROXY_B = "http://user-b:pass-b@proxy-b.example:8002"
PROXY_C = "http://user-c:pass-c@proxy-c.example:8003"


def _entry(raw: str) -> ProxyEntry:
    return ProxyEntry.parse(raw)


def _job(proxy_pool: list[str], *, max_retries: int = 2) -> protocol_web.WebJob:
    initial = ProxyConfig(enabled=True, entry=_entry(proxy_pool[0]))
    return protocol_web.WebJob(
        id="proxyretryfixture",
        owner_device_id="devicefixture",
        ba_token="BA-PROXYRETRYFIXTURE01",
        phone="+447700900123",
        country="GB",
        max_retries=max_retries,
        proxy_enabled=True,
        proxy_label=initial.label,
        _proxy_config=initial,
        _proxy_pool=list(proxy_pool),
    )


def _rotate_phone(job: protocol_web.WebJob, phone: str) -> str:
    job.phone = phone
    job.retry_phone_required = False
    job.retry_previous_phone = ""
    job.status = "queued"
    return phone


def test_select_working_proxy_excludes_used_entry_and_duplicate_lines() -> None:
    first = _entry(PROXY_A)
    second = _entry(PROXY_B)
    preferred = ProxyConfig(enabled=True, entry=first)

    with (
        patch.object(protocol_web.random, "shuffle", side_effect=lambda values: None),
        patch.object(protocol_web, "proxy_probe", return_value=(True, "HTTP 302")) as probe,
    ):
        selected = protocol_web.select_working_proxy(
            [PROXY_A, PROXY_A, PROXY_B],
            preferred,
            country="GB",
            exclude_entries={first},
        )

    assert selected.entry == second
    assert probe.call_count == 1
    assert probe.call_args.args[0].entry == second


def test_protocol_payment_retries_use_distinct_proxies_a_b_c() -> None:
    job = _job([PROXY_A, PROXY_B, PROXY_C])
    selected_entries: list[ProxyEntry] = []
    replacement_phones = iter(["+447700900124", "+447700900125"])

    class FakeProtocolFlow:
        def __init__(self, **kwargs):
            selected_entries.append(kwargs["proxy_config"].entry)

        def run(self):
            if len(selected_entries) < 3:
                raise RuntimeError(f"protocol failure {len(selected_entries)}")
            return {
                "status": "success",
                "return_url": "https://merchant.fixture/return?status=success",
            }

    def rotate_phone() -> str:
        return _rotate_phone(job, next(replacement_phones))

    with (
        patch.object(protocol_web, "find_authorization_checkpoint", return_value=None),
        patch.object(protocol_web.random, "shuffle", side_effect=lambda values: None),
        patch.object(protocol_web, "proxy_probe", return_value=(True, "HTTP 302")),
        patch.object(
            protocol_web,
            "generate_card",
            return_value=CardInfo("4111111111111111", "12/2030", "123"),
        ),
        patch.object(protocol_web, "WebIdentityElevationPayPalFlow", FakeProtocolFlow),
        patch.object(protocol_web, "_authorization_checkpoint_from_result", return_value=None),
        patch.object(job, "wait_for_retry_phone", side_effect=rotate_phone) as phone_rotation,
        patch.object(protocol_web, "record_protocol_metric"),
        patch.object(protocol_web, "record_payment_audit"),
    ):
        protocol_web.run_job(job)

    assert selected_entries == [_entry(PROXY_A), _entry(PROXY_B), _entry(PROXY_C)]
    assert len(set(selected_entries)) == 3
    assert phone_rotation.call_count == 2
    assert job.retry_count == 2
    assert job.status == "completed"
    proxy_events = [event for event in job.events if event["type"] == "protocol.retry.proxy_changed"]
    assert len(proxy_events) == 2
    assert [event["data"]["attempted_proxy_count"] for event in proxy_events] == [2, 3]


def test_protocol_retry_stops_when_only_used_proxy_remains() -> None:
    job = _job([PROXY_A, PROXY_A])
    selected_entries: list[ProxyEntry] = []

    class FailingProtocolFlow:
        def __init__(self, **kwargs):
            selected_entries.append(kwargs["proxy_config"].entry)

        def run(self):
            raise RuntimeError("protocol failure")

    with (
        patch.object(protocol_web, "find_authorization_checkpoint", return_value=None),
        patch.object(protocol_web.random, "shuffle", side_effect=lambda values: None),
        patch.object(protocol_web, "proxy_probe", return_value=(True, "HTTP 302")) as probe,
        patch.object(
            protocol_web,
            "generate_card",
            return_value=CardInfo("4111111111111111", "12/2030", "123"),
        ),
        patch.object(protocol_web, "WebIdentityElevationPayPalFlow", FailingProtocolFlow),
        patch.object(
            job,
            "wait_for_retry_phone",
            side_effect=lambda: _rotate_phone(job, "+447700900124"),
        ) as phone_rotation,
        patch.object(protocol_web, "record_protocol_metric"),
        patch.object(protocol_web, "record_payment_audit"),
    ):
        protocol_web.run_job(job)

    assert selected_entries == [_entry(PROXY_A)]
    assert probe.call_count == 1
    assert phone_rotation.call_count == 0
    assert job.retry_count == 0
    assert job.status == "failed"
    assert job.result["error_code"] == "PROTOCOL_PROXY_POOL_EXHAUSTED"
    assert "No unused proxy remains" in job.result["error"]


def test_failed_preflight_entry_is_not_retried_after_protocol_failure() -> None:
    job = _job([PROXY_A, PROXY_B, PROXY_C])
    probed_entries: list[ProxyEntry] = []
    selected_entries: list[ProxyEntry] = []

    def probe(config: ProxyConfig) -> tuple[bool, str]:
        probed_entries.append(config.entry)
        if config.entry == _entry(PROXY_A):
            return False, "connect failed"
        return True, "HTTP 302"

    class FailOnceProtocolFlow:
        def __init__(self, **kwargs):
            selected_entries.append(kwargs["proxy_config"].entry)

        def run(self):
            if len(selected_entries) == 1:
                raise RuntimeError("protocol failure")
            return {
                "status": "success",
                "return_url": "https://merchant.fixture/return?status=success",
            }

    with (
        patch.object(protocol_web, "find_authorization_checkpoint", return_value=None),
        patch.object(protocol_web.random, "shuffle", side_effect=lambda values: None),
        patch.object(protocol_web, "proxy_probe", side_effect=probe),
        patch.object(
            protocol_web,
            "generate_card",
            return_value=CardInfo("4111111111111111", "12/2030", "123"),
        ),
        patch.object(protocol_web, "WebIdentityElevationPayPalFlow", FailOnceProtocolFlow),
        patch.object(protocol_web, "_authorization_checkpoint_from_result", return_value=None),
        patch.object(
            job,
            "wait_for_retry_phone",
            side_effect=lambda: _rotate_phone(job, "+447700900124"),
        ) as phone_rotation,
        patch.object(protocol_web, "record_protocol_metric"),
        patch.object(protocol_web, "record_payment_audit"),
    ):
        protocol_web.run_job(job)

    assert probed_entries == [_entry(PROXY_A), _entry(PROXY_B), _entry(PROXY_C)]
    assert selected_entries == [_entry(PROXY_B), _entry(PROXY_C)]
    assert phone_rotation.call_count == 1
    assert job.retry_count == 1
    assert job.status == "completed"


def test_unavailable_unused_proxies_stop_without_another_phone_retry() -> None:
    job = _job([PROXY_A, PROXY_B, PROXY_C])
    probed_entries: list[ProxyEntry] = []
    selected_entries: list[ProxyEntry] = []

    def probe(config: ProxyConfig) -> tuple[bool, str]:
        probed_entries.append(config.entry)
        if config.entry == _entry(PROXY_A):
            return True, "HTTP 302"
        return False, "connect failed"

    class FailingProtocolFlow:
        def __init__(self, **kwargs):
            selected_entries.append(kwargs["proxy_config"].entry)

        def run(self):
            raise RuntimeError("protocol failure")

    with (
        patch.object(protocol_web, "find_authorization_checkpoint", return_value=None),
        patch.object(protocol_web.random, "shuffle", side_effect=lambda values: None),
        patch.object(protocol_web, "proxy_probe", side_effect=probe),
        patch.object(
            protocol_web,
            "generate_card",
            return_value=CardInfo("4111111111111111", "12/2030", "123"),
        ),
        patch.object(protocol_web, "WebIdentityElevationPayPalFlow", FailingProtocolFlow),
        patch.object(
            job,
            "wait_for_retry_phone",
            side_effect=lambda: _rotate_phone(job, "+447700900124"),
        ) as phone_rotation,
        patch.object(protocol_web, "record_protocol_metric"),
        patch.object(protocol_web, "record_payment_audit"),
    ):
        protocol_web.run_job(job)

    assert probed_entries == [_entry(PROXY_A), _entry(PROXY_B), _entry(PROXY_C)]
    assert selected_entries == [_entry(PROXY_A)]
    assert phone_rotation.call_count == 1
    assert job.retry_count == 1
    assert job.status == "failed"
    assert job.result["error_code"] == "PROTOCOL_PROXY_UNAVAILABLE"


def test_proxy_pool_exhaustion_is_not_retryable() -> None:
    job = _job([PROXY_A])
    job._attempted_proxy_entries.add(_entry(PROXY_A))
    error = protocol_web.ProtocolProxyPoolExhaustedError("no unused proxy")

    assert protocol_web._job_failure_is_retryable(job, error) is False
    with pytest.raises(protocol_web.ProtocolProxyPoolExhaustedError):
        protocol_web.select_working_proxy(
            [PROXY_A],
            job._proxy_config,
            country="GB",
            exclude_entries=job._attempted_proxy_entries,
        )
