from __future__ import annotations

import json
import os
import base64
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlsplit

from gopay import gopay_extract


class FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        url: str = "https://api.stripe.com/v1/payment_pages/cs_live_response_secret",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.url = url
        self.text = json.dumps(payload)
        self.headers: dict[str, str] = {}

    def json(self) -> object:
        return self._payload


def valid_init_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "ppage_init_secret",
        "config_id": "config_init",
        "init_checksum": "init-checksum-secret",
        "elements_options": {"amount": 0, "currency": "idr"},
        "payment_method_types": ["card", "link", "gopay"],
    }
    payload.update(overrides)
    return payload


def confirm_context() -> dict[str, object]:
    return {
        "stripe_js_id": "stripe-js-secret",
        "guid": "guid-secret",
        "muid": "muid-secret",
        "sid": "sid-secret",
        "elements_session_id": "elements_session_secret",
        "elements_session_config_id": "elements-config-secret",
        "payment_element_config_id": "payment-element-config-secret",
        "config_id": "stripe-init-config-secret",
        "runtime_version": "runtime-test",
        "stripe_version": "stripe-version-test",
        "checkout_amount": 0,
    }


def billing_profile() -> dict[str, str]:
    return {
        "name": "Test User",
        "email": "test.user@example.com",
        "line1": "Jl Merdeka 18",
        "city": "Bandung",
        "postal_code": "40111",
        "state": "Jawa Barat",
        "country": "ID",
    }


class GoPayConfirmProtocolTests(unittest.TestCase):
    def test_normalizes_access_and_session_tokens_without_upi_runtime(self) -> None:
        access_token, session_token = gopay_extract.normalize_token(
            json.dumps(
                {
                    "accessToken": "access-token-value",
                    "cookies": "foo=bar; __Secure-next-auth.session-token=session-value",
                }
            )
        )

        self.assertEqual(access_token, "access-token-value")
        self.assertEqual(session_token, "session-value")

    def test_rejects_oaics_checkout_id_explicitly(self) -> None:
        with self.assertRaisesRegex(
            gopay_extract.GoPayUnavailableError,
            "rejects oaics_ checkout",
        ):
            gopay_extract.require_stripe_checkout_id("oaics_example")

        self.assertEqual(
            gopay_extract.require_stripe_checkout_id("cs_live_example"),
            "cs_live_example",
        )

    def test_confirm_body_contains_required_gopay_fields(self) -> None:
        body = gopay_extract.build_gopay_confirm_body(
            checkout_id="cs_live_checkout_secret",
            stripe_pk="pk_live_publishable_secret",
            init_payload=valid_init_payload(),
            ctx=confirm_context(),
            billing=billing_profile(),
            dynamic_token_fields={
                "js_checksum": "js-checksum-secret",
                "rv_timestamp": "1787600000000",
            },
        )

        self.assertEqual(body["payment_method_data[type]"], "gopay")
        self.assertEqual(body["expected_payment_method_type"], "gopay")
        self.assertEqual(body["expected_amount"], "0")
        self.assertEqual(
            body["client_attribution_metadata[payment_method_selection_flow]"],
            "merchant_specified",
        )
        self.assertEqual(
            body[
                "payment_method_data[client_attribution_metadata]"
                "[payment_method_selection_flow]"
            ],
            "merchant_specified",
        )
        self.assertEqual(body["js_checksum"], "js-checksum-secret")
        self.assertEqual(body["rv_timestamp"], "1787600000000")
        self.assertEqual(body["payment_method_data[billing_details][address][country]"], "ID")
        self.assertEqual(
            body["client_attribution_metadata[checkout_config_id]"],
            "payment-element-config-secret",
        )
        self.assertEqual(
            body[
                "payment_method_data[client_attribution_metadata][checkout_config_id]"
            ],
            "stripe-init-config-secret",
        )

    def test_confirm_body_rejects_nonzero_amount(self) -> None:
        with self.assertRaisesRegex(
            gopay_extract.GoPayUnavailableError,
            "expected zero amount, got 2000",
        ):
            gopay_extract.build_gopay_confirm_body(
                checkout_id="cs_live_checkout_secret",
                stripe_pk="pk_live_publishable_secret",
                init_payload=valid_init_payload(
                    elements_options={"amount": 2000, "currency": "idr"}
                ),
                ctx=confirm_context(),
                billing=billing_profile(),
                dynamic_token_fields={
                    "js_checksum": "checksum",
                    "rv_timestamp": "1787600000000",
                },
            )


class GoPayCheckoutEligibilityTests(unittest.TestCase):
    def _stripe_init(self, payload: dict[str, object]) -> tuple[dict[str, object], dict[str, object]]:
        session = Mock()
        session.post.return_value = FakeResponse(payload)
        checkout = {
            "checkout_id": "cs_live_checkout_secret",
            "stripe_pk": "pk_live_publishable_secret",
        }
        with (
            patch.object(gopay_extract, "dump_http", return_value=None),
            patch.object(gopay_extract, "log_event"),
        ):
            return gopay_extract.stripe_init(session, checkout)

    def test_accepts_zero_idr_checkout_with_gopay(self) -> None:
        payload, context = self._stripe_init(valid_init_payload())

        self.assertEqual(payload["payment_method_types"], ["card", "link", "gopay"])
        self.assertEqual(context["checkout_amount"], 0)

    def test_rejects_nonzero_wrong_currency_or_missing_gopay(self) -> None:
        cases = (
            (
                valid_init_payload(
                    elements_options={"amount": 2000, "currency": "idr"}
                ),
                "expected zero amount, got 2000",
            ),
            (
                valid_init_payload(elements_options={"amount": 0, "currency": "usd"}),
                "expected IDR, got usd",
            ),
            (
                valid_init_payload(payment_method_types=["card", "link"]),
                "GoPay is not available",
            ),
        )

        for payload, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(
                    gopay_extract.GoPayUnavailableError,
                    message,
                ):
                    self._stripe_init(payload)


class GoPayApprovalTests(unittest.TestCase):
    def test_http_200_blocked_is_a_business_rejection(self) -> None:
        with self.assertRaisesRegex(
            gopay_extract.GoPayApproveBlockedError,
            "HTTP 200 business rejection",
        ):
            gopay_extract.classify_approve_response(200, {"result": "blocked"})

    def test_blocked_approval_captures_payment_page_before_reraising(self) -> None:
        chatgpt_session = Mock()
        stripe_session = Mock()
        stripe_session.headers = {}
        checkout = {
            "checkout_id": "cs_live_checkout_secret",
            "stripe_pk": "pk_live_publishable_secret",
            "processor_entity": "openai_llc",
        }
        context = confirm_context()
        confirm_payload = {
            "status": "open",
            "payment_status": "unpaid",
            "submission_attempt": {"state": "requires_approval"},
        }

        with (
            patch.object(
                gopay_extract,
                "proxy_for_indonesia",
                return_value="http://user-country-ID:pass@proxy.example:8080",
            ),
            patch.object(
                gopay_extract,
                "build_chatgpt_session",
                return_value=chatgpt_session,
            ),
            patch.object(gopay_extract, "create_checkout", return_value=checkout),
            patch.object(gopay_extract, "load_bootstrap_context"),
            patch.object(gopay_extract, "new_session", return_value=stripe_session),
            patch.object(
                gopay_extract,
                "stripe_init",
                return_value=(valid_init_payload(), context),
            ),
            patch.object(gopay_extract, "stripe_elements_session"),
            patch.object(
                gopay_extract,
                "indonesia_billing_profile",
                return_value=billing_profile(),
            ),
            patch.object(gopay_extract, "sync_checkout_taxes"),
            patch.object(gopay_extract, "sync_checkout_snapshot"),
            patch.object(
                gopay_extract,
                "stripe_confirm_gopay",
                return_value=confirm_payload,
            ),
            patch.object(
                gopay_extract,
                "chatgpt_approve",
                side_effect=gopay_extract.GoPayApproveBlockedError("blocked"),
            ),
            patch.object(
                gopay_extract,
                "snapshot_payment_page_once",
                return_value={"submission_attempt": {"state": "requires_approval"}},
            ) as snapshot_mock,
            patch.object(gopay_extract, "poll_payment_page") as poll_mock,
            patch.object(gopay_extract, "log_event"),
        ):
            with self.assertRaises(gopay_extract.GoPayApproveBlockedError):
                gopay_extract.run_gopay_flow("access-token", "session-token", "seed")

        snapshot_mock.assert_called_once_with(
            stripe_session,
            checkout,
            context,
            stage="approve_blocked",
        )
        poll_mock.assert_not_called()


class GoPayBillingIdentityTests(unittest.TestCase):
    @staticmethod
    def _token(email: str) -> str:
        payload = json.dumps(
            {"https://api.openai.com/profile": {"email": email}},
            separators=(",", ":"),
        ).encode()
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        return f"header.{encoded}.signature"

    def test_account_email_is_read_from_access_token_profile(self) -> None:
        self.assertEqual(
            gopay_extract.account_email_from_token(
                self._token("account@example.com")
            ),
            "account@example.com",
        )

    def test_billing_prefers_account_email_and_allows_explicit_override(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GOPAY_EMAIL", None)
            billing = gopay_extract.indonesia_billing_profile(
                "account@example.com"
            )
        self.assertEqual(billing["email"], "account@example.com")

        with patch.dict(
            os.environ,
            {"GOPAY_EMAIL": "configured@example.com"},
            clear=False,
        ):
            billing = gopay_extract.indonesia_billing_profile(
                "account@example.com"
            )
        self.assertEqual(billing["email"], "configured@example.com")

    def test_payment_diagnostics_include_nested_payment_error(self) -> None:
        diagnostics = gopay_extract.payment_page_diagnostics(
            {
                "submission_attempt": {
                    "state": "failed",
                    "error": {
                        "code": "checkout_approval_payment_failure_with_payment_error"
                    },
                    "manual_approval_updates": {
                        "payment_error": {
                            "type": "invalid_request_error",
                            "decline_code": "provider_rejected",
                            "message": "provider did not create a mandate",
                        }
                    },
                }
            }
        )

        self.assertEqual(
            diagnostics["error"]["code"],
            "checkout_approval_payment_failure_with_payment_error",
        )
        self.assertEqual(
            diagnostics["error"]["decline_code"], "provider_rejected"
        )
        self.assertIn("mandate", diagnostics["error"]["message"])


class GoPayDiagnosticRedactionTests(unittest.TestCase):
    def test_forced_http_dump_removes_protocol_secrets(self) -> None:
        secrets = {
            "access_token": "access-token-secret",
            "session_token": "session-token-secret",
            "stripe_pk": "pk_live_publishable_secret",
            "checkout": "cs_live_checkout_secret",
            "elements": "elements_session_elements_secret",
            "payment_page": "ppage_payment_secret",
            "attempt": "cs_attempt_attempt_secret",
            "js_checksum": "js-checksum-secret",
            "rv_timestamp": "1787600000000-secret",
            "init_checksum": "init-checksum-secret",
            "rqdata": "captcha-rqdata-secret",
            "email": "alice@example.com",
            "redirect": "redirect-result-secret",
        }
        request_body = {
            **secrets,
            "checkout_session_id": secrets["checkout"],
            "elements_session_id": secrets["elements"],
            "payment_page_id": secrets["payment_page"],
            "attempt_id": secrets["attempt"],
            "redirect_url": (
                "https://gopay.example/authorize/secret-path?"
                f"redirectResult={secrets['redirect']}&client_secret=top-secret"
            ),
        }
        response = FakeResponse(
            {
                "email": secrets["email"],
                "client_secret": "response-client-secret",
                "next_action": {
                    "redirect_to_url": {
                        "url": request_body["redirect_url"],
                    }
                },
                "submission_attempt": {"id": secrets["attempt"]},
            },
            url=(
                "https://api.stripe.com/v1/payment_pages/"
                f"{secrets['checkout']}?key={secrets['stripe_pk']}"
            ),
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.object(gopay_extract, "DUMP_DIR", Path(temp_dir)),
                patch.object(gopay_extract, "_dump_counter", 0),
            ):
                dump_path = gopay_extract.dump_http(
                    response,
                    "redaction-test",
                    request_body,
                    "POST",
                    request_body["redirect_url"],
                    force=True,
                )
            self.assertIsNotNone(dump_path)
            rendered = Path(dump_path).read_text(encoding="utf-8")
            payload = json.loads(rendered)

        for label, secret in secrets.items():
            with self.subTest(secret=label):
                self.assertNotIn(secret, rendered)
        self.assertNotIn("response-client-secret", rendered)
        self.assertNotIn("top-secret", rendered)
        self.assertEqual(payload["request"]["body"]["js_checksum"], "***")
        self.assertEqual(payload["request"]["body"]["rv_timestamp"], "***")
        self.assertEqual(payload["request"]["body"]["init_checksum"], "***")
        self.assertEqual(payload["request"]["body"]["rqdata"], "***")
        self.assertEqual(payload["request"]["body"]["email"], "a***@example.com")
        self.assertIn("...#", payload["request"]["body"]["checkout_session_id"])
        self.assertIn("?redacted#", payload["request"]["url"])


class GoPayProxyCompatibilityTests(unittest.TestCase):
    def test_host_port_user_password_seed_is_normalized_and_rewritten_to_id(self) -> None:
        seed = (
            "proxy.example:7878:"
            "user-country-US-session-66218873:password-secret"
        )
        with patch.dict(
            os.environ,
            {"GOPAY_PROXY_DEFAULT_SCHEME": "socks5h"},
            clear=False,
        ):
            normalized = gopay_extract.normalize_proxy_url(seed)
            indonesia = gopay_extract.proxy_for_indonesia(seed)

        normalized_parts = urlsplit(normalized)
        self.assertEqual(normalized_parts.scheme, "socks5h")
        self.assertEqual(normalized_parts.hostname, "proxy.example")
        self.assertEqual(normalized_parts.port, 7878)
        self.assertEqual(
            unquote(normalized_parts.username or ""),
            "user-country-US-session-66218873",
        )
        self.assertEqual(unquote(normalized_parts.password or ""), "password-secret")

        indonesia_parts = urlsplit(indonesia)
        self.assertEqual(
            unquote(indonesia_parts.username or ""),
            "user-country-ID-session-66218873",
        )
        self.assertEqual(unquote(indonesia_parts.password or ""), "password-secret")

    def test_bare_seed_auto_mode_offers_all_supported_protocols(self) -> None:
        seed = "proxy.example:7878:user-country-US-session-one:password-secret"
        with patch.dict(
            os.environ,
            {"GOPAY_PROXY_DEFAULT_SCHEME": "auto"},
            clear=False,
        ):
            candidates = gopay_extract.proxy_url_candidates(seed)

        self.assertEqual(
            [urlsplit(candidate).scheme for candidate in candidates],
            ["socks5h", "socks5", "http", "https"],
        )

    def test_explicit_proxy_protocol_is_preserved_without_probe(self) -> None:
        seed = "https://user-country-US:password@proxy.example:7878"
        with patch.object(gopay_extract, "_probe_proxy_candidate") as probe:
            resolved = gopay_extract.resolve_proxy_url(seed)

        self.assertEqual(urlsplit(resolved).scheme, "https")
        probe.assert_not_called()

    def test_auto_mode_selects_first_available_protocol_by_priority(self) -> None:
        seed = "proxy.example:7878:user-country-US-session-one:password-secret"

        def probe(candidate: str) -> tuple[bool, int, str]:
            scheme = urlsplit(candidate).scheme
            if scheme in {"socks5h", "http"}:
                return True, 403 if scheme == "socks5h" else 200, ""
            return False, 0, "Timeout"

        with (
            patch.dict(os.environ, {"GOPAY_PROXY_DEFAULT_SCHEME": "auto"}, clear=False),
            patch.object(gopay_extract, "_probe_proxy_candidate", side_effect=probe),
            patch.object(gopay_extract, "log_event"),
        ):
            resolved = gopay_extract.resolve_proxy_url(seed)

        self.assertEqual(urlsplit(resolved).scheme, "socks5h")


if __name__ == "__main__":
    unittest.main()
