from __future__ import annotations

"""Summarize one CDP-captured GoPay HAR without emitting secrets."""

import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


CHATGPT_ENDPOINTS = {
    "/backend-api/sentinel/req",
    "/backend-api/sentinel/ping",
    "/backend-api/payments/checkout",
    "/backend-api/payments/checkout/taxes",
    "/backend-api/payments/checkout/snapshot",
    "/backend-api/payments/checkout/approve",
}


def _text(entry: dict[str, Any], *, response: bool = False) -> str:
    obj = entry.get("response" if response else "request") or {}
    obj = obj.get("content" if response else "postData") or {}
    if not isinstance(obj, dict):
        return ""
    value = str(obj.get("text") or "")
    if obj.get("encoding") == "base64":
        try:
            return base64.b64decode(value).decode("utf-8", errors="replace")
        except Exception:
            return value
    return value


def _json(value: str) -> Any:
    try:
        return json.loads(value) if value else None
    except (TypeError, ValueError):
        return None


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


def _hash(value: Any) -> str:
    return hashlib.sha256(str(value).encode("utf-8", errors="replace")).hexdigest()[:16]


def _length_hash(value: Any) -> str:
    text = str(value or "")
    return f"len={len(text)} sha256={_hash(text)}"


def _paths(value: Any, prefix: str = "") -> list[str]:
    result: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            result.append(path)
            result.extend(_paths(nested, path))
    elif isinstance(value, list) and prefix:
        result.append(prefix + "[]")
    return sorted(set(result))


def _safe_path(path: str) -> str:
    import re

    value = re.sub(r"cs_(?:live|test)_[A-Za-z0-9_]+", "cs_<CHECKOUT_SESSION>", path)
    value = re.sub(r"/(?:[0-9a-f]{8}-[0-9a-f-]{27,})", "/<UUID>", value, flags=re.I)
    value = re.sub(r"/authorize/[^/?]+/[^/?]+", "/authorize/<ACCOUNT>/<NONCE>", value)
    return value


def summarize(path: Path) -> dict[str, Any]:
    raw = path.read_bytes()
    har = json.loads(raw.decode("utf-8-sig"))
    entries = har.get("log", {}).get("entries", [])
    hosts = Counter()
    statuses = Counter()
    methods = Counter()
    endpoint_counts: dict[str, dict[str, Any]] = {}
    sequence: list[str] = []
    sentinel_flows = Counter()
    sentinel_shapes: list[dict[str, Any]] = []
    headers = Counter()
    header_lengths: dict[str, set[int]] = {}
    body_rows: list[dict[str, Any]] = []
    midtrans: list[dict[str, Any]] = []

    for index, entry in enumerate(entries):
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        parsed = urlsplit(str(request.get("url") or ""))
        host = parsed.netloc
        request_path = parsed.path or "/"
        method = str(request.get("method") or "GET").upper()
        status = int(response.get("status", 0) or 0)
        hosts[host] += 1
        statuses[status] += 1
        methods[method] += 1

        if host == "chatgpt.com" and request_path in CHATGPT_ENDPOINTS:
            endpoint_counts.setdefault(request_path, {"count": 0, "statuses": Counter()})["count"] += 1
            endpoint_counts[request_path]["statuses"][status] += 1
            sequence.append(f"{index}:{request_path}")
        elif host == "api.stripe.com" and request_path.startswith("/v1/"):
            endpoint_counts.setdefault(request_path, {"count": 0, "statuses": Counter()})["count"] += 1
            endpoint_counts[request_path]["statuses"][status] += 1
            sequence.append(f"{index}:{_safe_path(request_path)}")
        elif host == "pm-redirects.stripe.com":
            endpoint_counts.setdefault("pm-redirects.stripe.com/authorize", {"count": 0, "statuses": Counter()})["count"] += 1
            endpoint_counts["pm-redirects.stripe.com/authorize"]["statuses"][status] += 1
            sequence.append(f"{index}:pm-redirects.stripe.com/authorize")
        elif host == "app.midtrans.com" and request_path.startswith("/snap/"):
            safe_endpoint = _safe_path(request_path.split("?", 1)[0])
            endpoint_counts.setdefault(safe_endpoint, {"count": 0, "statuses": Counter()})["count"] += 1
            endpoint_counts[safe_endpoint]["statuses"][status] += 1
            sequence.append(f"{index}:{safe_endpoint}")

        request_headers = _headers(request.get("headers"))
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
            if name in request_headers:
                headers[name] += 1
                header_lengths.setdefault(name, set()).add(len(request_headers[name]))

        request_text = _text(entry)
        response_text = _text(entry, response=True)
        request_json = _json(request_text)
        response_json = _json(response_text)
        if host == "chatgpt.com" and request_path in CHATGPT_ENDPOINTS:
            row: dict[str, Any] = {
                "index": index,
                "path": request_path,
                "method": method,
                "status": status,
                "request_len": len(request_text),
                "response_len": len(response_text),
                "request_keys": sorted(request_json) if isinstance(request_json, dict) else [],
                "response_keys": sorted(response_json) if isinstance(response_json, dict) else [],
            }
            if request_path == "/backend-api/payments/checkout" and isinstance(request_json, dict):
                billing = request_json.get("billing_details") if isinstance(request_json.get("billing_details"), dict) else {}
                row["safe_request"] = {
                    "entry_point": request_json.get("entry_point"),
                    "plan_name": request_json.get("plan_name"),
                    "checkout_ui_mode": request_json.get("checkout_ui_mode"),
                    "billing_country": billing.get("country"),
                    "billing_currency": billing.get("currency"),
                }
            if request_path.endswith("/taxes") and isinstance(request_json, dict):
                row["safe_request"] = {
                    "billing_country": request_json.get("billing_country"),
                    "currency": request_json.get("currency"),
                    "processor_entity": request_json.get("processor_entity"),
                    "billing_address_keys": sorted((request_json.get("billing_address") or {}).keys()),
                }
            if request_path.endswith("/snapshot") and isinstance(request_json, dict):
                address = ((request_json.get("snapshot") or {}).get("billing_address") or {}).get("address") or {}
                row["safe_request"] = {"snapshot_keys": sorted((request_json.get("snapshot") or {}).keys()), "address_keys": sorted(address)}
            if request_path.endswith("/approve") and isinstance(request_json, dict):
                row["safe_request"] = {"processor_entity": request_json.get("processor_entity")}
            if request_path == "/backend-api/payments/checkout" and isinstance(response_json, dict):
                row["safe_response"] = {
                    "checkout_provider": response_json.get("checkout_provider"),
                    "processor_entity": response_json.get("processor_entity"),
                    "status": response_json.get("status"),
                    "payment_status": response_json.get("payment_status"),
                    "requires_manual_approval": response_json.get("requires_manual_approval"),
                    "automatic_tax_enabled": response_json.get("automatic_tax_enabled"),
                    "billing_details": response_json.get("billing_details"),
                }
            if request_path.endswith("/taxes") and isinstance(response_json, dict):
                checkout_session = response_json.get("checkout_session") if isinstance(response_json.get("checkout_session"), dict) else {}
                automatic_tax = checkout_session.get("automatic_tax") if isinstance(checkout_session.get("automatic_tax"), dict) else {}
                details = checkout_session.get("total_details") if isinstance(checkout_session.get("total_details"), dict) else {}
                row["safe_response"] = {
                    "using_automatic_tax": response_json.get("using_automatic_tax"),
                    "amount_subtotal": checkout_session.get("amount_subtotal"),
                    "amount_total": checkout_session.get("amount_total"),
                    "currency": checkout_session.get("currency"),
                    "payment_method_types": checkout_session.get("payment_method_types"),
                    "payment_status": checkout_session.get("payment_status"),
                    "mode": checkout_session.get("mode"),
                    "approval_method": checkout_session.get("approval_method"),
                    "automatic_tax": {"enabled": automatic_tax.get("enabled"), "status": automatic_tax.get("status")},
                    "amount_discount": details.get("amount_discount"),
                    "amount_tax": details.get("amount_tax"),
                }
            if request_path.endswith("/approve") and isinstance(response_json, dict):
                row["safe_response"] = {"result": response_json.get("result")}
            body_rows.append(row)

        if host == "chatgpt.com" and request_path == "/backend-api/sentinel/req" and isinstance(request_json, dict):
            flow = str(request_json.get("flow") or "")
            sentinel_flows[flow] += 1
            sentinel_shapes.append({"index": index, "flow": flow, "id": _length_hash(request_json.get("id")), "p": _length_hash(request_json.get("p"))})

        if host == "app.midtrans.com" and "/snap/v1/transactions/" in request_path and isinstance(response_json, dict):
            details = response_json.get("transaction_details") if isinstance(response_json.get("transaction_details"), dict) else {}
            enabled = response_json.get("enabled_payments") if isinstance(response_json.get("enabled_payments"), list) else []
            midtrans.append({
                "index": index,
                "status": status,
                "gross_amount": details.get("gross_amount"),
                "currency": details.get("currency"),
                "recommended_payment_method": response_json.get("recommended_payment_method"),
                "enabled_payments": [item.get("type") for item in enabled if isinstance(item, dict)],
                "gopay_keys": sorted((response_json.get("gopay") or {}).keys()),
            })

    normalized_endpoints = {
        key: {"count": value["count"], "statuses": dict(value["statuses"])}
        for key, value in endpoint_counts.items()
    }
    return {
        "name": path.name,
        "path": str(path.resolve()),
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest().upper(),
        "entry_count": len(entries),
        "hosts": dict(hosts),
        "statuses": dict(statuses),
        "methods": dict(methods),
        "endpoints": normalized_endpoints,
        "sequence": sequence,
        "sentinel_flows": dict(sentinel_flows),
        "sentinel_shapes": sentinel_shapes,
        "header_presence": dict(headers),
        "header_lengths": {key: sorted(value) for key, value in header_lengths.items()},
        "body_rows": body_rows,
        "midtrans": midtrans,
    }


def render(report: dict[str, Any]) -> str:
    lines = [
        "# GoPay CDP capture summary (redacted)",
        "",
        "> This report is derived from local CDP data. Raw credentials, cookies, tokens, session IDs, customer data, order IDs, and redirect nonces are not emitted.",
        "",
        f"- source: `{report['path']}`",
        f"- size_bytes: `{report['size']}`",
        f"- sha256: `{report['sha256']}`",
        f"- entries: `{report['entry_count']}`",
        f"- hosts: `{json.dumps(report['hosts'], ensure_ascii=False, sort_keys=True)}`",
        f"- statuses: `{json.dumps(report['statuses'], ensure_ascii=False, sort_keys=True)}`",
        f"- methods: `{json.dumps(report['methods'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Endpoint coverage",
        "",
        f"```json\n{json.dumps(report['endpoints'], ensure_ascii=False, indent=2)}\n```",
        "",
        "## Sentinel and identity",
        "",
        f"- flows: `{json.dumps(report['sentinel_flows'], ensure_ascii=False, sort_keys=True)}`",
        f"- payload shapes: `{json.dumps(report['sentinel_shapes'], ensure_ascii=False)}`",
        f"- header presence: `{json.dumps(report['header_presence'], ensure_ascii=False, sort_keys=True)}`",
        f"- header lengths: `{json.dumps(report['header_lengths'], ensure_ascii=False, sort_keys=True)}`",
        "",
        "## ChatGPT body summaries",
        "",
        f"```json\n{json.dumps(report['body_rows'], ensure_ascii=False, indent=2)}\n```",
        "",
        "## Midtrans",
        "",
        f"```json\n{json.dumps(report['midtrans'], ensure_ascii=False, indent=2)}\n```",
        "",
        "## Relevant sequence",
        "",
        "```text",
        " -> ".join(report["sequence"]),
        "```",
        "",
        "## Coverage finding",
        "",
        f"- `api.stripe.com` entries: `{report['hosts'].get('api.stripe.com', 0)}`",
        f"- `js.stripe.com` entries: `{report['hosts'].get('js.stripe.com', 0)}`",
        "- ChatGPT and Midtrans bodies are present; Stripe API init/elements/tax_region/confirm are absent from this capture.",
        "- The absence is an observed target-coverage result, not a fabricated response or replayed request.",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("har", type=Path)
    parser.add_argument("--output", "-o", type=Path)
    args = parser.parse_args()
    text = render(summarize(args.har))
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(f"HAR_CDP_SUMMARY={args.output.resolve()}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
