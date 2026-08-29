from __future__ import annotations

import json
from pathlib import Path

from tools.har_cdp_gopay_summary import render, summarize


def test_cdp_summary_keeps_body_structure_without_secret_values(tmp_path: Path) -> None:
    source = tmp_path / "capture.har"
    source.write_text(
        json.dumps(
            {
                "log": {
                    "version": "1.2",
                    "entries": [
                        {
                            "request": {
                                "method": "POST",
                                "url": "https://chatgpt.com/backend-api/payments/checkout",
                                "headers": [
                                    {"name": "OpenAI-Sentinel-Token", "value": "secret-proof"},
                                    {"name": "oai-device-id", "value": "D" * 36},
                                ],
                                "postData": {
                                    "text": json.dumps(
                                        {
                                            "entry_point": "all_plans_pricing_modal",
                                            "plan_name": "chatgptplusplan",
                                            "billing_details": {"country": "ID", "currency": "IDR"},
                                            "checkout_ui_mode": "custom",
                                        }
                                    )
                                },
                            },
                            "response": {
                                "status": 200,
                                "content": {
                                    "text": json.dumps(
                                        {
                                            "checkout_provider": "stripe",
                                            "client_secret": "secret-client-value",
                                            "status": "open",
                                        }
                                    )
                                },
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    report = summarize(source)
    text = render(report)
    assert report["entry_count"] == 1
    assert report["endpoints"]["/backend-api/payments/checkout"]["count"] == 1
    assert "secret-proof" not in text
    assert "secret-client-value" not in text


def test_cdp_summary_reports_stripe_coverage_when_api_entries_are_present(tmp_path: Path) -> None:
    source = tmp_path / "capture-with-stripe.har"
    source.write_text(
        json.dumps(
            {
                "log": {
                    "entries": [
                        {
                            "request": {
                                "method": "POST",
                                "url": "https://api.stripe.com/v1/payment_pages/cs_live_x/init",
                                "postData": {"text": "amount=34900000"},
                            },
                            "response": {
                                "status": 200,
                                "content": {"text": "{\"payment_method_types\":[\"gopay\"]}"},
                            },
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    text = render(summarize(source))
    assert "`api.stripe.com` entries: `1`" in text
    assert "Stripe API init/elements/tax_region/confirm, and Midtrans bodies are present" in text
    assert "are absent from this capture" not in text
