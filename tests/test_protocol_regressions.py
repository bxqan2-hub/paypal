from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from flask import Flask


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "paypal_agreement_protocol"
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))

import web as protocol_web  # noqa: E402
from paypal_agreement_protocol import herosms as herosms_module  # noqa: E402
from payment_link_extractor.web import paypal_protocol as protocol_bridge  # noqa: E402


class ProtocolRetryTests(unittest.TestCase):
    def make_job(self) -> protocol_web.WebJob:
        return protocol_web.WebJob(
            id="retryfixture",
            owner_device_id="devicefixture",
            ba_token="BA-RETRYFIXTURE01",
            phone="+447700900123",
        )

    def test_automatic_retry_uses_a_new_phone_for_every_attempt(self) -> None:
        job = self.make_job()
        phones: list[str] = []
        replacement_phones = iter(["+447700900124", "+447700900125"])

        def attempt(current: protocol_web.WebJob) -> None:
            phones.append(current.phone)
            if len(phones) < 3:
                raise RuntimeError(f"transient failure {len(phones)}")
            current.complete({"status": "success"})

        def rotate_phone() -> str:
            phone = next(replacement_phones)
            job.phone = phone
            job.retry_phone_required = False
            job.retry_previous_phone = ""
            job.status = "queued"
            return phone

        with (
            patch.object(protocol_web, "_run_job_attempt", side_effect=attempt),
            patch.object(job, "wait_for_retry_phone", side_effect=rotate_phone) as rotation,
            patch.object(protocol_web, "record_protocol_metric"),
            patch.object(protocol_web, "record_payment_audit"),
        ):
            protocol_web.run_job(job)

        self.assertEqual(phones, ["+447700900123", "+447700900124", "+447700900125"])
        self.assertEqual(rotation.call_count, 2)
        self.assertEqual(job.retry_count, 2)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.result["retry_count"], 2)
        self.assertTrue(any("停止使用旧手机号" in item["message"] for item in job.logs))

    def test_retry_phone_gate_rejects_old_phone_and_codes(self) -> None:
        job = self.make_job()
        job.prepare_retry(RuntimeError("transient"))

        with self.assertRaisesRegex(ValueError, "新的有效手机号"):
            job.submit_input("123456")
        with self.assertRaisesRegex(ValueError, "旧手机号"):
            job.submit_input("+447700900123")

        job.submit_input("+447700900124")
        self.assertEqual(job.wait_for_retry_phone(), "+447700900124")
        self.assertFalse(job.retry_phone_required)

    def test_sms_watcher_rotates_immediately_when_protocol_retry_starts(self) -> None:
        class FakeClient:
            poll_interval = 0.01

            def __init__(self) -> None:
                self.finished: list[tuple[str, int]] = []
                self.status_calls = 0

            def finish(self, activation_id: str, status: int = 6) -> None:
                self.finished.append((activation_id, status))

            def get_status(self, _activation_id: str):
                self.status_calls += 1
                raise AssertionError("retry rotation must not poll the old activation")

        class FakeJob:
            id = "retry-job"
            status = "awaiting_otp"
            retry_count = 1
            max_retries = 2
            country = "GB"

            def __init__(self) -> None:
                self.logs: list[tuple[str, str]] = []
                self.submitted: list[str] = []

            def add_log(self, level: str, message: str) -> None:
                self.logs.append((level, message))

            def emit_event(self, *_args) -> None:
                return None

            def set_sms_activation(self, *_args) -> None:
                return None

            def submit_input(self, value: str) -> None:
                self.submitted.append(value)
                self.status = "completed"

            def cancel(self) -> None:
                self.status = "cancelled"

        initial = {"activation_id": "activation-old", "phone": "+447700900123"}
        replacement = {"activation_id": "activation-new", "phone": "+447700900124"}
        client = FakeClient()
        job = FakeJob()
        with protocol_bridge._SMS_RESERVATIONS_LOCK:
            protocol_bridge._SMS_RESERVATIONS_BY_PHONE.clear()
            protocol_bridge._SMS_RESERVATIONS_BY_ACTIVATION.clear()
        try:
            self.assertTrue(protocol_bridge._reserve_new_sms_activation(initial))
            with (
                patch.object(protocol_bridge, "_sms_client", return_value=client),
                patch.object(protocol_bridge._protocol, "get_job", return_value=job),
                patch.object(protocol_bridge, "_acquire_unique_sms_number", return_value=replacement),
            ):
                protocol_bridge._watch_sms_job(job.id, initial)
        finally:
            with protocol_bridge._SMS_RESERVATIONS_LOCK:
                protocol_bridge._SMS_RESERVATIONS_BY_PHONE.clear()
                protocol_bridge._SMS_RESERVATIONS_BY_ACTIVATION.clear()

        self.assertEqual(job.submitted, ["+447700900124"])
        self.assertIn(("activation-old", 8), client.finished)
        self.assertEqual(client.status_calls, 0)

    def test_consumed_ba_failure_is_not_retried(self) -> None:
        job = self.make_job()
        error = protocol_web.ProtocolResultError({
            "status": "error",
            "error_code": "BA_ALREADY_AUTHORIZED",
            "error": "billing agreement was already authorized",
            "paypal_authorized": True,
        })

        with (
            patch.object(protocol_web, "_run_job_attempt", side_effect=error) as attempt,
            patch.object(protocol_web, "record_protocol_metric"),
            patch.object(protocol_web, "record_payment_audit"),
        ):
            protocol_web.run_job(job)

        self.assertEqual(attempt.call_count, 1)
        self.assertEqual(job.retry_count, 0)
        self.assertEqual(job.status, "failed")


class HeroSMSNoNumbersRegressionTests(unittest.TestCase):
    def test_no_numbers_404_is_classified_as_retryable_inventory_error(self) -> None:
        client = herosms_module.HeroSMSClient()
        client.api_key = "fixture-key"
        request = httpx.Request("GET", client.base_url)
        response = httpx.Response(
            404,
            request=request,
            json={"title": "NO_NUMBERS", "details": "Numbers Not Found. Try Later"},
        )

        with patch.object(herosms_module.httpx, "get", return_value=response):
            with self.assertRaises(herosms_module.HeroSMSNoNumbersError) as raised:
                client._request("getNumberV2", country=16, service="ts")

        self.assertEqual(raised.exception.provider_code, "NO_NUMBERS")
        self.assertTrue(raised.exception.retryable)
        self.assertNotIn("HTTP 404", str(raised.exception))

    def test_number_acquisition_retries_no_inventory_with_backoff(self) -> None:
        client = herosms_module.HeroSMSClient()
        client.number_retry_attempts = 3
        client.number_retry_delay = 0.25
        no_numbers = herosms_module.HeroSMSNoNumbersError
        responses = [
            no_numbers(),
            no_numbers(),
            {"activationId": "activation-new", "phoneNumber": "447700900124"},
        ]

        with (
            patch.object(client, "resolve_country_id", return_value=16),
            patch.object(client, "resolve_service_code", return_value="ts"),
            patch.object(client, "_request", side_effect=responses) as request_call,
            patch.object(herosms_module.time, "sleep") as sleep,
        ):
            activation = client.acquire_number("GB")

        self.assertEqual(activation["phone"], "+447700900124")
        self.assertEqual(request_call.call_count, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [0.25, 0.5])

    def test_number_acquisition_reports_attempt_count_after_inventory_exhaustion(self) -> None:
        client = herosms_module.HeroSMSClient()
        client.number_retry_attempts = 3
        client.number_retry_delay = 0.25

        with (
            patch.object(client, "resolve_country_id", return_value=16),
            patch.object(client, "resolve_service_code", return_value="ts"),
            patch.object(
                client,
                "_request",
                side_effect=[
                    herosms_module.HeroSMSNoNumbersError(),
                    herosms_module.HeroSMSNoNumbersError(),
                    herosms_module.HeroSMSNoNumbersError(),
                ],
            ),
            patch.object(herosms_module.time, "sleep") as sleep,
        ):
            with self.assertRaises(herosms_module.HeroSMSNoNumbersError) as raised:
                client.acquire_number("GB")

        self.assertEqual(raised.exception.attempts, 3)
        self.assertEqual([item.args[0] for item in sleep.call_args_list], [0.25, 0.5])

    def test_sms_number_route_returns_structured_no_inventory_result(self) -> None:
        class NoInventoryClient:
            def acquire_number(self, *_args, **_kwargs):
                raise herosms_module.HeroSMSNoNumbersError(
                    attempts=4, retry_after_seconds=4,
                )

        app = Flask("herosms-no-numbers-test")
        protocol_bridge.register_paypal_protocol(app)
        with patch.object(protocol_bridge, "_sms_client", return_value=NoInventoryClient()):
            response = app.test_client().post(
                "/paypal-pay/api/sms/number", json={"country": "GB"},
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["code"], "NO_NUMBERS")
        self.assertTrue(payload["retryable"])
        self.assertEqual(payload["attempts"], 4)
        self.assertIn("已自动重试 4 次", payload["error"])


class FrontendRegressionTests(unittest.TestCase):
    def test_logs_phone_replacement_and_bulk_push_are_wired(self) -> None:
        protocol_js = (PROTOCOL_ROOT / "web_static" / "app.js").read_text(encoding="utf-8")
        extractor_js = (ROOT / "payment_link_extractor" / "web" / "static" / "app.js").read_text(encoding="utf-8")
        extractor_html = (ROOT / "payment_link_extractor" / "web" / "templates" / "index.html").read_text(encoding="utf-8")

        self.assertIn("function isTerminalJob", protocol_js)
        self.assertIn("if (!isTerminalJob(job)) state.jobLogTimer", protocol_js)
        self.assertNotIn("if (!terminal(job)) state.jobLogTimer", protocol_js)
        self.assertIn("replaceRetryableTerminalNumber", protocol_js)
        self.assertIn("if (isCompletedJob(existingRows[index]?.job)) continue;", protocol_js)
        self.assertIn(">获取新号</button>", protocol_js)
        self.assertIn('id="push-selected-paypal"', extractor_html)
        self.assertIn("function pushPaypalTasks", extractor_js)
        self.assertIn("links.join('\\n')", extractor_js)

    def test_multiline_entry_parallel_start_and_phone_rebinding_are_wired(self) -> None:
        protocol_js = (PROTOCOL_ROOT / "web_static" / "app.js").read_text(encoding="utf-8")

        self.assertIn(".filter(item => !isDefaultDemoBa(item)).join('\\n')", protocol_js)
        self.assertNotIn(".filter(item => item && !isDefaultDemoBa(item)).join('\\n')", protocol_js)
        self.assertIn("function reconcileBaPhoneAssignments()", protocol_js)
        self.assertIn("state.baTokenOrder = nextTokens;", protocol_js)
        self.assertIn("const next = new Map(state.batchAccountMap);", protocol_js)
        self.assertIn("state.batchJobIds = [...new Set([...state.batchJobIds, ...incomingIds])]", protocol_js)
        self.assertIn("function currentQueueHasActiveJob()", protocol_js)
        self.assertNotIn("$('submitButton').disabled = hasActive;", protocol_js)
        self.assertIn("duplicatePhoneIndexes(phoneCandidates)", protocol_js)
        self.assertIn("function phoneIdentity(value)", protocol_js)
        self.assertIn("if (!sharedWithAnotherAccount) await cancelSmsActivation(current);", protocol_js)
        bridge_source = (ROOT / "payment_link_extractor" / "web" / "paypal_protocol.py").read_text(encoding="utf-8")
        self.assertIn('"HEROSMS_ROTATION_WAIT_SECONDS", 120.0, 30.0, 300.0', bridge_source)

    def test_phone_duplicate_detection_ignores_formatting(self) -> None:
        self.assertEqual(
            protocol_web.duplicate_phone_indexes([
                "+44 7700 900122",
                "447700900133",
                "44 (7700) 900-122",
            ]),
            [2],
        )


class SmsReservationRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        with protocol_bridge._SMS_RESERVATIONS_LOCK:
            protocol_bridge._SMS_RESERVATIONS_BY_PHONE.clear()
            protocol_bridge._SMS_RESERVATIONS_BY_ACTIVATION.clear()

    def tearDown(self) -> None:
        self.setUp()

    def test_unique_acquire_rejects_a_phone_reserved_by_another_account(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.items = [
                    {"activation_id": "activation-2", "phone": "+447700900122"},
                    {"activation_id": "activation-3", "phone": "+447700900133"},
                ]
                self.finished: list[tuple[str, int]] = []

            def acquire_number(self, *_args, **_kwargs):
                return self.items.pop(0)

            def finish(self, activation_id: str, status: int = 6) -> None:
                self.finished.append((activation_id, status))

        first = {"activation_id": "activation-1", "phone": "+447700900122"}
        self.assertTrue(protocol_bridge._reserve_new_sms_activation(first, owner="job-1"))
        client = FakeClient()

        result = protocol_bridge._acquire_unique_sms_number(client, "GB", owner="job-2")

        self.assertEqual(result["activation_id"], "activation-3")
        self.assertEqual(client.finished, [("activation-2", 6)])
        self.assertTrue(protocol_bridge._sms_activation_is_reserved("activation-1"))
        self.assertTrue(protocol_bridge._sms_activation_is_reserved("activation-3"))

    def test_duplicate_provider_activation_is_never_cancelled(self) -> None:
        class FakeClient:
            def __init__(self) -> None:
                self.finished: list[tuple[str, int]] = []

            def acquire_number(self, *_args, **_kwargs):
                return {"activation_id": "activation-1", "phone": "+447700900122"}

            def finish(self, activation_id: str, status: int = 6) -> None:
                self.finished.append((activation_id, status))

        first = {"activation_id": "activation-1", "phone": "+447700900122"}
        self.assertTrue(protocol_bridge._reserve_new_sms_activation(first, owner="job-1"))
        client = FakeClient()

        with self.assertRaises(protocol_bridge.HeroSMSError):
            protocol_bridge._acquire_unique_sms_number(client, "GB", owner="job-2")

        self.assertEqual(client.finished, [])
        self.assertTrue(protocol_bridge._sms_activation_is_reserved("activation-1"))


if __name__ == "__main__":
    unittest.main()
