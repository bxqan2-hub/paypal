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
