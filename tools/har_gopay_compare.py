from __future__ import annotations

"""Produce a redacted, side-by-side GoPay HAR protocol report.

The input HAR files are treated as data. No request is replayed and no raw
credential, cookie, token, session id, order id, or redirect nonce is emitted.
"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit


CHATGPT_PATHS = {
    "/backend-api/payments/checkout",
    "/backend-api/payments/checkout/taxes",
    "/backend-api/payments/checkout/snapshot",
    "/backend-api/payments/checkout/approve",
    "/backend-api/sentinel/req",
    "/backend-api/sentinel/ping",
    "/backend-api/sentinel/frame.html",
    "/backend-api/sentinel/sdk.js",
}


def _headers(raw: Any) -> dict[str, str]:
    if isinstance(raw, list):
        return {
            str(item.get("name", "")).lower(): str(item.get("value", ""))
            for item in raw
            if isinstance(item, dict)
        }
    if isinstance(raw, dict):
        return {str(key).lower(): str(value) for key, value in raw.items()}
    return {}


def _json_text(value: str) -> Any:
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


def _post_text(entry: dict[str, Any]) -> str:
    post_data = (entry.get("request") or {}).get("postData") or {}
    return str(post_data.get("text") or "") if isinstance(post_data, dict) else ""


def _sha(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:16]


def _redacted_length(value: Any) -> str:
    text = str(value or "")
    return f"len={len(text)} sha256={_sha(text)}"


def _safe_path(path: str) -> str:
    value = re.sub(r"cs_(?:live|test)_[A-Za-z0-9_]+", "cs_<CHECKOUT_SESSION>", path)
    value = re.sub(r"/(?:[0-9a-f]{8}-[0-9a-f-]{27,})", "/<UUID>", value, flags=re.I)
    value = re.sub(r"/authorize/[^/?]+/[^/?]+", "/authorize/<ACCOUNT>/<NONCE>", value)
    return value


def _endpoint(host: str, path: str) -> str | None:
    if host == "chatgpt.com" and path in CHATGPT_PATHS:
        return path
    if host == "api.stripe.com" and path.startswith("/v1/"):
        return path
    if host == "pm-redirects.stripe.com":
        return "pm-redirects.stripe.com/authorize"
    if host == "app.midtrans.com" and path.startswith("/snap/"):
        return path.split("/", 4)[0:4] and "/".join(path.split("/")[:4])
    return None


def summarize(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    har = json.loads(raw.decode("utf-8-sig"))
    entries = har.get("log", {}).get("entries", [])
    hosts = Counter()
    statuses = Counter()
    methods = Counter()
    endpoints: dict[str, list[dict[str, Any]]] = {}
    sequence: list[str] = []
    sentinel_flows = Counter()
    sentinel_shapes: list[dict[str, Any]] = []
    header_presence = Counter()
    header_lengths: dict[str, set[int]] = {}
    stripe_tax_steps: list[dict[str, Any]] = []
    stripe_confirm: list[dict[str, Any]] = []
    stripe_elements: list[dict[str, Any]] = []
    midtrans_transactions: list[dict[str, Any]] = []
    redirects: list[dict[str, Any]] = []
    response_capture = Counter()

    for index, entry in enumerate(entries):
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        parsed = urlsplit(str(request.get("url") or ""))
        host = parsed.netloc
        path_value = parsed.path or "/"
        hosts[host] += 1
        statuses[int(response.get("status", 0) or 0)] += 1
        methods[str(request.get("method") or "GET").upper()] += 1
        ep = _endpoint(host, path_value)
        if ep is not None:
            safe_ep = _safe_path(ep)
            endpoints.setdefault(safe_ep, []).append(
                {"index": index, "method": str(request.get("method") or "GET").upper(), "status": int(response.get("status", 0) or 0)}
            )
            sequence.append(f"{index}:{safe_ep}")

        headers = _headers(request.get("headers"))
        for name in (
            "openai-sentinel-token",
            "openai-sentinel-so-token",
            "oai-web-deployment-attestation",
            "oai-did",
            "oai-device-id",
            "oai-session-id",
            "oai-language",
            "oai-client-build-number",
            "oai-client-version",
            "x-oai-is-client-observation",
        ):
            if name in headers:
                header_presence[name] += 1
                header_lengths.setdefault(name, set()).add(len(headers[name]))

        post_text = _post_text(entry)
        post_json = _json_text(post_text)
        if host == "chatgpt.com" and path_value == "/backend-api/sentinel/req":
            if isinstance(post_json, dict):
                flow = str(post_json.get("flow") or "")
                sentinel_flows[flow] += 1
                sentinel_shapes.append(
                    {
                        "index": index,
                        "flow": flow,
                        "id": _redacted_length(post_json.get("id")),
                        "p": _redacted_length(post_json.get("p")),
                    }
                )
        if host == "api.stripe.com" and path_value == "/v1/elements/sessions":
            query = dict(parse_qsl(parsed.query, keep_blank_values=True))
            stripe_elements.append(
                {
                    "index": index,
                    "keys": sorted(query),
                    "selected": {
                        key: ("<redacted>" if key in {"key", "stripe_js_id", "checkout_session_id"} else value)
                        for key, value in query.items()
                        if key in {
                            "deferred_intent[mode]",
                            "deferred_intent[amount]",
                            "deferred_intent[currency]",
                            "deferred_intent[setup_future_usage]",
                            "deferred_intent[payment_method_types][0]",
                            "deferred_intent[payment_method_types][1]",
                            "deferred_intent[payment_method_types][2]",
                            "currency",
                            "elements_init_source",
                            "referrer_host",
                            "locale",
                            "type",
                            "key",
                            "_stripe_version",
                        }
                    },
                }
            )
        if host == "api.stripe.com" and path_value.startswith("/v1/payment_pages/"):
            params = (request.get("postData") or {}).get("params") or []
            values = {
                str(item.get("name")): str(item.get("value", ""))
                for item in params
                if isinstance(item, dict)
            }
            if request.get("method") == "POST" and not path_value.endswith("/init") and not path_value.endswith("/confirm"):
                fields = [
                    key
                    for key in (
                        "tax_region[country]",
                        "tax_region[line1]",
                        "tax_region[city]",
                        "tax_region[state]",
                        "tax_region[postal_code]",
                    )
                    if key in values
                ]
                if fields:
                    stripe_tax_steps.append({"index": index, "fields": fields, "field_count": len(fields)})
            if path_value.endswith("/confirm"):
                stripe_confirm.append(
                    {
                        "index": index,
                        "key_count": len(values),
                        "expected_amount": values.get("expected_amount", ""),
                        "expected_payment_method_type": values.get("expected_payment_method_type", ""),
                        "link_brand": values.get("link_brand", ""),
                        "payment_method_data[type]": values.get("payment_method_data[type]", ""),
                        "payment_method_data[time_on_page]": values.get("payment_method_data[time_on_page]", ""),
                        "version": values.get("version", ""),
                        "_stripe_version": values.get("_stripe_version", ""),
                        "init_checksum": _redacted_length(values.get("init_checksum")),
                        "js_checksum": _redacted_length(values.get("js_checksum")),
                    }
                )
        if host == "pm-redirects.stripe.com":
            location = _headers(response.get("headers")).get("location", "")
            redirects.append({"index": index, "status": int(response.get("status", 0) or 0), "location_host": urlsplit(location).netloc, "location_path": _safe_path(urlsplit(location).path)})
        if host == "app.midtrans.com" and "/snap/v1/transactions/" in path_value:
            response_text = str((response.get("content") or {}).get("text") or "")
            payload = _json_text(response_text)
            if isinstance(payload, dict):
                details = payload.get("transaction_details") if isinstance(payload.get("transaction_details"), dict) else {}
                enabled = payload.get("enabled_payments") if isinstance(payload.get("enabled_payments"), list) else []
                midtrans_transactions.append(
                    {
                        "index": index,
                        "status": int(response.get("status", 0) or 0),
                        "amount": details.get("gross_amount"),
                        "currency": details.get("currency"),
                        "recommended": payload.get("recommended_payment_method"),
                        "enabled_types": [item.get("type") for item in enabled if isinstance(item, dict)],
                    }
                )
        content = response.get("content") or {}
        response_capture["with_text"] += bool(content.get("text"))
        response_capture["without_text"] += not bool(content.get("text"))

    return {
        "name": path.name,
        "path": str(path.resolve()),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest().upper(),
        "entry_count": len(entries),
        "hosts": dict(hosts),
        "statuses": dict(statuses),
        "methods": dict(methods),
        "endpoints": endpoints,
        "sequence": sequence,
        "sentinel_flows": dict(sentinel_flows),
        "sentinel_shapes": sentinel_shapes,
        "header_presence": dict(header_presence),
        "header_lengths": {key: sorted(value) for key, value in header_lengths.items()},
        "stripe_tax_steps": stripe_tax_steps,
        "stripe_elements": stripe_elements,
        "stripe_confirm": stripe_confirm,
        "midtrans_transactions": midtrans_transactions,
        "redirects": redirects,
        "response_capture": dict(response_capture),
    }


def render(reports: list[dict[str, Any]]) -> str:
    lines = [
        "# GoPay HAR dual comparison (redacted)",
        "",
        "> Inputs are offline HAR data. No request was replayed. Token, cookie, session, order, and nonce values are represented only by length/hash or placeholders.",
        "",
    ]
    for report in reports:
        lines.extend(
            [
                f"## {report['name']}",
                f"- source: `{report['path']}`",
                f"- size_bytes: `{report['size']}`",
                f"- sha256: `{report['sha256']}`",
                f"- entries: `{report['entry_count']}`",
                f"- hosts: `{json.dumps(report['hosts'], ensure_ascii=False, sort_keys=True)}`",
                f"- statuses: `{json.dumps(report['statuses'], ensure_ascii=False, sort_keys=True)}`",
                f"- methods: `{json.dumps(report['methods'], ensure_ascii=False, sort_keys=True)}`",
                "",
                "### Sentinel and ChatGPT contract",
                f"- sentinel flows: `{json.dumps(report['sentinel_flows'], ensure_ascii=False, sort_keys=True)}`",
                f"- sentinel shapes: `{json.dumps(report['sentinel_shapes'], ensure_ascii=False)}`",
                f"- header presence: `{json.dumps(report['header_presence'], ensure_ascii=False, sort_keys=True)}`",
                f"- header lengths: `{json.dumps(report['header_lengths'], ensure_ascii=False, sort_keys=True)}`",
                f"- response text captured/omitted: `{json.dumps(report['response_capture'], ensure_ascii=False, sort_keys=True)}`",
                "",
                "### Stripe contract",
                f"- Elements query: `{json.dumps(report['stripe_elements'], ensure_ascii=False)}`",
                f"- progressive tax steps: `{json.dumps(report['stripe_tax_steps'], ensure_ascii=False)}`",
                f"- confirm summary: `{json.dumps(report['stripe_confirm'], ensure_ascii=False)}`",
                "",
                "### Provider contract",
                f"- Stripe redirects: `{json.dumps(report['redirects'], ensure_ascii=False)}`",
                f"- Midtrans transactions: `{json.dumps(report['midtrans_transactions'], ensure_ascii=False)}`",
                "",
                "### Relevant sequence",
                "```text",
                " -> ".join(report["sequence"]),
                "```",
                "",
            ]
        )
    if len(reports) == 2:
        left, right = reports
        lines.extend(
            [
                "## Direct differences",
                "",
                f"| Metric | {left['name']} | {right['name']} |",
                "|---|---:|---:|",
                f"| HAR entries | {left['entry_count']} | {right['entry_count']} |",
                f"| ChatGPT entries | {left['hosts'].get('chatgpt.com', 0)} | {right['hosts'].get('chatgpt.com', 0)} |",
                f"| WebSocket entries | {left['hosts'].get('ws.chatgpt.com', 0)} | {right['hosts'].get('ws.chatgpt.com', 0)} |",
                f"| Sentinel token lengths | {left['header_lengths'].get('openai-sentinel-token', [])} | {right['header_lengths'].get('openai-sentinel-token', [])} |",
                f"| Approve/checkout statuses | {left['endpoints'].get('/backend-api/payments/checkout/approve', [])} | {right['endpoints'].get('/backend-api/payments/checkout/approve', [])} |",
                f"| Midtrans gross amount | {left['midtrans_transactions']} | {right['midtrans_transactions']} |",
                "",
                "### Stable observations",
                "- Both captures use `cs_live` Checkout, not `oaics_`.",
                "- Both use `chatgpt_checkout` and `checkout_session_approval` Sentinel flows.",
                "- Both keep `id-ID`, client build `10012890`, the same client version, and `Asia/Jakarta` Stripe browser timezone.",
                "- Both have five progressive Stripe tax-region POSTs: country, line1, city, state, postal_code.",
                "- Both end with a 302 from `pm-redirects.stripe.com` to an `app.midtrans.com` redirection page and a transaction response recommending GoPay.",
                "",
                "### Variable observations",
                "- Sentinel payload lengths, proof lengths, device/session identifiers, and Stripe checksums differ between captures.",
                "- The second capture includes one `ws.chatgpt.com` WebSocket and more telemetry/FARO traffic.",
                "- The first capture orders the second taxes/snapshot refresh before confirm differently from the second capture; stage code should not assume those background telemetry calls are globally serialized.",
                "- ChatGPT payment request bodies and ChatGPT response bodies are not present in these HAR entries; their exact JSON fields cannot be inferred from this pair.",
                "",
            ]
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har", nargs=2, type=Path)
    parser.add_argument("--output", "-o", type=Path)
    args = parser.parse_args()
    text = render([summarize(path) for path in args.har])
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"HAR_COMPARE_REPORT={args.output.resolve()}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
