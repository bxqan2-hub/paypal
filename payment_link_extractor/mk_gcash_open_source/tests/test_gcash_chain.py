import unittest
from unittest.mock import patch

import gcash_chain


def checkout_response():
    return {
        "checkout_session_id": "oaics_fixture",
        "processor_entity": "openai_llc",
        "checkout_session": {
            "amount_total": 0,
            "custom_payment_methods": [
                {"id": "cpmt_fixture", "options": {"type": "static"}},
            ],
        },
    }


class GCashChainProtocolTests(unittest.TestCase):
    def test_browser_aligned_protocol_order_and_headers(self):
        requests = []
        sentinel_calls = []
        chain = gcash_chain.GCashChain(
            token="fixture-token",
            client_account_id="fixture-client",
            account_id="fixture-account",
            billing_email="holder@example.com",
            billing_name="Fixture Holder",
        )

        def fake_sentinel(**kwargs):
            sentinel_calls.append(kwargs)
            return '{"flow":"' + kwargs["flow"] + '"}', ""

        def fake_request(method, url, headers, data=None, **kwargs):
            requests.append({
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": data,
            })
            if url == gcash_chain.CHECKOUT_BASE:
                if sum(item["url"] == gcash_chain.CHECKOUT_BASE for item in requests) == 1:
                    raise RuntimeError("HTTP 400 unusual activity")
                return checkout_response(), {}
            if url.endswith("/taxes"):
                return checkout_response(), {}
            if url.endswith("/confirm"):
                return {
                    "status": "success",
                    "confirm_return_url": "https://chatgpt.com/checkout/verify",
                }, {}
            if url.endswith("/custom_payment_method/start"):
                return {
                    "status": "requires_action",
                    "next_action": {
                        "paymentMethodType": "gcash",
                        "method": "GET",
                        "type": "redirect",
                        "url": (
                            "https://checkoutshopper-live.adyen.com/"
                            "checkoutshopper/checkoutPaymentRedirect"
                            "?redirectData=fixture"
                        ),
                    },
                }, {}
            raise AssertionError(f"unexpected request: {method} {url}")

        def fake_follow_redirect():
            chain.gcash_url = (
                "https://m.gcash.com/gcash-login-web/index.html"
                "?netAuthId=fixture"
            )
            chain.callback_status = "waiting_scan"

        with (
            patch.object(chain, "_preflight_proxy", return_value={"country": "PH", "ip": ""}),
            patch.object(chain, "_verify_proxy_stability"),
            patch.object(chain, "_prepare_frontend_context"),
            patch.object(chain, "_bootstrap"),
            patch.object(chain, "_follow_redirect", side_effect=fake_follow_redirect),
            patch.object(gcash_chain, "_request", side_effect=fake_request),
            patch.object(gcash_chain, "mint_sentinel_sync", side_effect=fake_sentinel),
        ):
            result = chain.run()

        self.assertEqual("success", result["status"])
        self.assertEqual("", result["error_message"])
        self.assertEqual(
            [
                gcash_chain.CHECKOUT_BASE,
                gcash_chain.CHECKOUT_BASE,
                f"{gcash_chain.CHECKOUT_BASE}/taxes",
                f"{gcash_chain.CHECKOUT_BASE}/confirm",
                f"{gcash_chain.CHECKOUT_BASE}/custom_payment_method/start",
            ],
            [request["url"] for request in requests],
        )
        self.assertFalse(any(request["url"].endswith("/update") for request in requests))
        self.assertEqual(
            ["chatgpt_checkout", "checkout_session_approval"],
            [call["flow"] for call in sentinel_calls],
        )

        first_create, create, taxes, confirm, start = requests
        self.assertNotIn("OpenAI-Sentinel-Token", first_create["headers"])
        self.assertEqual("fixture-account", create["headers"]["ChatGPT-Account-Id"])
        self.assertEqual(
            '{"flow":"chatgpt_checkout"}',
            create["headers"]["OpenAI-Sentinel-Token"],
        )
        self.assertEqual("plus-1-month-free", create["body"]["promo_campaign"]["promo_campaign_id"])
        self.assertTrue(create["body"]["promo_campaign"]["is_coupon_from_query_param"])
        self.assertTrue(create["body"]["check_card_proxy"])
        self.assertEqual({"country": "PH", "currency": "PHP"}, create["body"]["billing_details"])
        self.assertEqual(
            "https://chatgpt.com/?promo_campaign=plus-1-month-free",
            create["headers"]["Referer"],
        )

        self.assertNotIn("OpenAI-Sentinel-Token", taxes["headers"])
        self.assertEqual("holder@example.com", taxes["body"]["checkout_email"])
        self.assertEqual("Fixture Holder", taxes["body"]["billing_name"])
        self.assertEqual({"country": "PH"}, taxes["body"]["billing_address"])
        self.assertIsNone(taxes["body"]["tax_id"])

        self.assertEqual("fixture-account", confirm["headers"]["ChatGPT-Account-Id"])
        self.assertEqual(
            '{"flow":"checkout_session_approval"}',
            confirm["headers"]["OpenAI-Sentinel-Token"],
        )
        self.assertEqual("cpmt_fixture", confirm["body"]["selected_payment_method_type"])
        self.assertNotIn("OpenAI-Sentinel-Token", start["headers"])
        self.assertEqual("adyen_redirect", chain.payment_route)
        self.assertEqual(chain.device_id, sentinel_calls[0]["device_id"])
        self.assertEqual(chain.device_id, sentinel_calls[1]["device_id"])
        self.assertEqual(
            "https://chatgpt.com/checkout/openai_llc/oaics_fixture",
            sentinel_calls[1]["page_url"],
        )

    def test_adyen_redirect_requires_exact_host_path_and_redirect_data(self):
        valid = (
            "https://checkoutshopper-live.adyen.com/"
            "checkoutshopper/checkoutPaymentRedirect?redirectData=fixture"
        )
        self.assertTrue(gcash_chain._is_adyen_checkout_redirect(valid))
        self.assertFalse(gcash_chain._is_adyen_checkout_redirect(valid.replace("redirectData=fixture", "x=1")))
        self.assertFalse(gcash_chain._is_adyen_checkout_redirect(valid.replace("checkoutshopper-live", "evil")))

    def test_create_does_not_mint_sentinel_for_non_risk_failure(self):
        chain = gcash_chain.GCashChain(
            token="fixture-token",
            client_account_id="fixture-client",
            proxy="proxy.example:8080:user:pass",
        )
        with (
            patch.object(chain, "_prepare_frontend_context"),
            patch.object(chain, "_bootstrap"),
            patch.object(chain, "_verify_proxy_stability"),
            patch.object(
                gcash_chain,
                "_request",
                side_effect=RuntimeError("HTTP 403 Cloudflare"),
            ),
            patch.object(gcash_chain, "mint_sentinel_sync") as mint,
        ):
            with self.assertRaisesRegex(RuntimeError, "403"):
                chain._create_checkout()
        mint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
