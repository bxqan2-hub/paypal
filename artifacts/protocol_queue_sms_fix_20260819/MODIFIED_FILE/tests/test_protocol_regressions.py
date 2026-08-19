from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_ROOT = ROOT / "paypal_agreement_protocol"
if str(PROTOCOL_ROOT) not in sys.path:
    sys.path.insert(0, str(PROTOCOL_ROOT))

import web as protocol_web  # noqa: E402
from payment_link_extractor.web import paypal_protocol as protocol_bridge  # noqa: E402


class ProtocolRetryTests(unittest.TestCase):
    def make_job(self) -> protocol_web.WebJob:
        return protocol_web.WebJob(
            id="retryfixture",
            owner_device_id="devicefixture",
            ba_token="BA-RETRYFIXTURE01",
            phone="+447700900123",
        )

    def test_automatic_retry_keeps_current_phone_and_stops_after_success(self) -> None:
        job = self.make_job()
        phones: list[str] = []

        def attempt(current: protocol_web.WebJob) -> None:
            phones.append(current.phone)
            if len(phones) < 3:
                raise RuntimeError(f"transient failure {len(phones)}")
            current.complete({"status": "success"})

        with (
            patch.object(protocol_web, "_run_job_attempt", side_effect=attempt),
            patch.object(protocol_web, "record_protocol_metric"),
            patch.object(protocol_web, "record_payment_audit"),
        ):
            protocol_web.run_job(job)

        self.assertEqual(phones, ["+447700900123"] * 3)
        self.assertEqual(job.retry_count, 2)
        self.assertEqual(job.status, "completed")
        self.assertEqual(job.result["retry_count"], 2)
        self.assertTrue(any("继续使用当前手机号" in item["message"] for item in job.logs))

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
        bridge_source = (ROOT / "payment_link_extractor" / "web" / "paypal_protocol.py").read_text(encoding="utf-8")
        self.assertIn('"HEROSMS_ROTATION_WAIT_SECONDS", 120.0, 30.0, 300.0', bridge_source)


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
