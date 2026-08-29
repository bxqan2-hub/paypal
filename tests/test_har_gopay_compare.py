from __future__ import annotations

import json
from pathlib import Path

from tools.har_gopay_compare import render, summarize


def _entry(url: str, *, method: str = "GET", status: int = 200, headers=None, post=None, response_text: str = "") -> dict:
    request = {
        "method": method,
        "url": url,
        "headers": headers or [],
        "queryString": [],
        "bodySize": 0,
    }
    if post is not None:
        request["postData"] = post
    return {
        "startedDateTime": "2026-08-30T00:00:00.000Z",
        "time": 1,
        "request": request,
        "response": {
            "status": status,
            "headers": [],
            "cookies": [],
            "content": {"mimeType": "application/json", "text": response_text},
            "redirectURL": "",
        },
    }


def test_gopay_har_compare_redacts_tokens_and_tracks_contract(tmp_path: Path) -> None:
    har = {
        "log": {
            "version": "1.2",
            "entries": [
                _entry(
                    "https://chatgpt.com/backend-api/sentinel/req",
                    method="POST",
                    post={
                        "mimeType": "text/plain;charset=UTF-8",
                        "text": json.dumps({"flow": "chatgpt_checkout", "id": "I" * 36, "p": "P" * 20}),
                    },
                ),
                _entry(
                    "https://chatgpt.com/backend-api/payments/checkout",
                    method="POST",
                    headers=[
                        {"name": "OpenAI-Sentinel-Token", "value": "secret-proof"},
                        {"name": "oai-device-id", "value": "D" * 36},
                        {"name": "oai-language", "value": "id-ID"},
                    ],
                ),
                _entry(
                    "https://api.stripe.com/v1/elements/sessions?deferred_intent%5Bamount%5D=34900000&deferred_intent%5Bpayment_method_types%5D%5B0%5D=card&deferred_intent%5Bpayment_method_types%5D%5B1%5D=gopay",
                ),
                _entry(
                    "https://api.stripe.com/v1/payment_pages/cs_live_secret/",
                    method="POST",
                    post={
                        "mimeType": "application/x-www-form-urlencoded",
                        "params": [
                            {"name": "tax_region[country]", "value": "ID"},
                            {"name": "tax_region[line1]", "value": "hidden-address"},
                        ],
                        "text": "",
                    },
                ),
                _entry(
                    "https://app.midtrans.com/snap/v1/transactions/00000000-0000-0000-0000-000000000000",
                    response_text=json.dumps(
                        {
                            "transaction_details": {"gross_amount": "349000", "currency": "IDR"},
                            "recommended_payment_method": "gopay",
                            "enabled_payments": [{"type": "gopay"}, {"type": "qris"}],
                        }
                    ),
                ),
            ],
        }
    }
    source = tmp_path / "sample.har"
    source.write_text(json.dumps(har), encoding="utf-8")
    report = summarize(source)
    text = render([report])
    assert report["sentinel_flows"] == {"chatgpt_checkout": 1}
    assert report["header_lengths"]["openai-sentinel-token"] == [12]
    assert report["stripe_tax_steps"][0]["field_count"] == 2
    assert report["midtrans_transactions"][0]["recommended"] == "gopay"
    assert "secret-proof" not in text
    assert "hidden-address" not in text
    assert "cs_live_secret" not in text
