from __future__ import annotations

import base64
import hashlib
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


SENSITIVE_NAME_RE = re.compile(
    r"(?:authorization|cookie|set-cookie|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|client[_-]?secret|password|passwd|secret|signature|redirectdata|"
    r"requestdata|^p$|qr[_-]?code|session[_-]?token|"
    r"(?:checkout|account|customer|payment|setup|confirmation|client|session)[_-]?id)",
    re.IGNORECASE,
)
SENSITIVE_URL_NAME_RE = re.compile(
    r"^(?:t|s|sid|session(?:id|_id)?|nonce|token|signature|redirect|return_url)$",
    re.IGNORECASE,
)
SENSITIVE_PATH_RE = re.compile(
    r"(?i)(?:oaics|seti|pi|cus|acct|ctoken|sa_nonce|authsess)[_-][A-Za-z0-9_-]+"
)
UUID_PATH_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9])[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}(?![A-Za-z0-9])"
)
MAX_INLINE_TEXT = 600


def load_har(path: str | Path) -> tuple[dict[str, Any], str]:
    source = Path(path)
    raw = source.read_bytes()
    try:
        value = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid HAR JSON: {source}") from exc
    if not isinstance(value, dict) or not isinstance(value.get("log"), dict):
        raise ValueError("HAR must contain a top-level log object")
    entries = value["log"].get("entries")
    if not isinstance(entries, list):
        raise ValueError("HAR log.entries must be an array")
    return value, hashlib.sha256(raw).hexdigest().upper()


def iter_entries(har: dict[str, Any]) -> Iterable[tuple[int, dict[str, Any]]]:
    entries = har.get("log", {}).get("entries", [])
    for index, entry in enumerate(entries):
        if isinstance(entry, dict):
            yield index, entry


def header_dict(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    if isinstance(headers, dict):
        return {str(k).lower(): str(v) for k, v in headers.items()}
    if isinstance(headers, list):
        for item in headers:
            if isinstance(item, dict) and item.get("name") is not None:
                result[str(item["name"]).lower()] = str(item.get("value", ""))
    return result


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:16]


def redact_value(name: str, value: Any, *, redact: bool = True) -> Any:
    text = str(value or "")
    if redact and (
        SENSITIVE_NAME_RE.search(str(name))
        or SENSITIVE_URL_NAME_RE.fullmatch(str(name).strip())
    ):
        return f"<redacted len={len(text)} sha256={_short_hash(text)}>"
    if len(text) > MAX_INLINE_TEXT:
        return f"<truncated len={len(text)} sha256={_short_hash(text)} preview={text[:120]!r}>"
    return value


def redact_path(path: str, *, redact: bool = True) -> str:
    if not redact:
        return path
    value = str(path or "")
    value = SENSITIVE_PATH_RE.sub("<redacted-id>", value)
    value = UUID_PATH_RE.sub("<redacted-uuid>", value)
    # Stripe authorize paths contain opaque account/nonce segments without a
    # stable prefix; retain only the endpoint shape.
    value = re.sub(r"(?i)(/authorize/)[^/?#]+", r"\1<redacted>", value)
    # Browser telemetry endpoints can put opaque base64 blobs directly in a
    # path. Keep the route shape but never publish those blobs.
    segments = value.split("/")
    value = "/".join(
        "<redacted-path>" if len(segment) > 160 else segment
        for segment in segments
    )
    return value


def redact_url(url: str, *, redact: bool = True) -> str:
    if not redact:
        return url
    parts = urlsplit(url)
    query = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((name, redact_value(name, value, redact=True)))
    host = parts.hostname or ""
    try:
        port = parts.port
    except ValueError:
        port = None
    if port:
        host = f"{host}:{port}"
    return urlunsplit(
        (
            parts.scheme,
            host,
            redact_path(parts.path, redact=True),
            urlencode(query),
            "",
        )
    )


def summarize_payload(text: Any, *, content_type: str = "", redact: bool = True) -> Any:
    if text in (None, ""):
        return ""
    raw = str(text)
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if payload is not None:
        return _summarize_json(payload, redact=redact)
    if "x-www-form-urlencoded" in content_type.lower() or "form-urlencoded" in content_type.lower():
        result = {}
        for name, value in parse_qsl(raw, keep_blank_values=True):
            result[name] = redact_value(name, value, redact=redact)
        return result
    if redact:
        return {"type": "text", "length": len(raw), "sha256": _short_hash(raw)}
    return raw if len(raw) <= MAX_INLINE_TEXT else raw[:MAX_INLINE_TEXT] + "..."


def _summarize_json(value: Any, *, redact: bool, name: str = "") -> Any:
    if isinstance(value, dict):
        return {str(k): _summarize_json(v, redact=redact, name=str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_summarize_json(item, redact=redact, name=name) for item in value[:50]]
    return redact_value(name, value, redact=redact)


def _header_summary(headers: Any, *, redact: bool) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in header_dict(headers).items():
        result[name] = redact_value(name, value, redact=redact)
    return result


def _body_size(content: dict[str, Any]) -> int:
    for key in ("size", "bodySize", "encodedDataLength"):
        value = content.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    text = content.get("text")
    return len(str(text).encode("utf-8")) if text else 0


def entry_summary(index: int, entry: dict[str, Any], *, redact: bool = True) -> dict[str, Any]:
    request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
    response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
    request_headers = header_dict(request.get("headers"))
    response_headers = header_dict(response.get("headers"))
    url = str(request.get("url") or "")
    parts = urlsplit(url)
    post_data = request.get("postData") if isinstance(request.get("postData"), dict) else {}
    response_content = response.get("content") if isinstance(response.get("content"), dict) else {}
    response_text = response_content.get("text", "")
    if redact:
        entry_host = parts.hostname or ""
        try:
            entry_port = parts.port
        except ValueError:
            entry_port = None
        if entry_port:
            entry_host = f"{entry_host}:{entry_port}"
    else:
        entry_host = parts.netloc
    return {
        "index": index,
        "startedDateTime": entry.get("startedDateTime"),
        "time_ms": round(float(entry.get("time", 0) or 0), 2),
        "method": str(request.get("method") or "GET").upper(),
        "url": redact_url(url, redact=redact),
        "host": entry_host,
        "path": redact_path(parts.path, redact=redact) or "/",
        "status": int(response.get("status", 0) or 0),
        "statusText": response.get("statusText", ""),
        "mimeType": response_content.get("mimeType") or response_headers.get("content-type", ""),
        "request_headers": _header_summary(request_headers, redact=redact),
        "response_headers": _header_summary(response_headers, redact=redact),
        "request_body": summarize_payload(
            post_data.get("text", ""),
            content_type=request_headers.get("content-type", ""),
            redact=redact,
        ),
        "response_body": summarize_payload(
            response_text,
            content_type=response_headers.get("content-type", ""),
            redact=redact,
        ),
        "request_size": int(request.get("bodySize", 0) or 0),
        "response_size": _body_size(response_content),
        "redirect_url": redact_url(
            str(response.get("redirectURL") or response_headers.get("location", "") or ""),
            redact=redact,
        ),
        "resource_type": entry.get("_resourceType") or entry.get("resourceType", ""),
    }


def _find_strings(value: Any, pattern: re.Pattern[str], results: list[str]) -> None:
    if isinstance(value, str):
        results.extend(pattern.findall(value))
    elif isinstance(value, dict):
        for nested in value.values():
            _find_strings(nested, pattern, results)
    elif isinstance(value, list):
        for nested in value:
            _find_strings(nested, pattern, results)


def analyze_har(
    path: str | Path,
    *,
    host: str = "",
    path_contains: str = "",
    method: str = "",
    status: int | None = None,
    contains: str = "",
    limit: int = 0,
    redact: bool = True,
) -> dict[str, Any]:
    har, sha256 = load_har(path)
    all_entries = list(iter_entries(har))
    channel = "momo" if any(
        "payment.momo.vn" in str((entry.get("request") or {}).get("url") or "").lower()
        or "/promo_campaign/check_coupon" in str((entry.get("request") or {}).get("url") or "").lower()
        for _, entry in all_entries
    ) else "gopay"
    selected: list[tuple[int, dict[str, Any]]] = []
    host_text = host.lower().strip()
    path_text = path_contains.lower().strip()
    method_text = method.upper().strip()
    contains_text = contains.lower().strip()
    for index, entry in all_entries:
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        url = str(request.get("url") or "")
        parts = urlsplit(url)
        entry_method = str(request.get("method") or "GET").upper()
        entry_status = int((entry.get("response") or {}).get("status", 0) or 0)
        haystack = json.dumps(entry, ensure_ascii=False).lower()
        if host_text and host_text not in parts.netloc.lower():
            continue
        if path_text and path_text not in parts.path.lower():
            continue
        if method_text and entry_method != method_text:
            continue
        if status is not None and entry_status != status:
            continue
        if contains_text and contains_text not in haystack:
            continue
        selected.append((index, entry))
    if limit > 0:
        selected = selected[:limit]

    summaries = [entry_summary(index, entry, redact=redact) for index, entry in selected]
    hosts = Counter()
    statuses = Counter()
    methods = Counter()
    client_builds: set[str] = set()
    client_versions: set[str] = set()
    user_agents: set[str] = set()
    sentinel_lengths: list[int] = []
    short_urls: list[str] = []
    gopay_redirects: list[dict[str, Any]] = []
    gopay_payment_methods: set[str] = set()
    gopay_amounts: set[str] = set()
    momo_gateway_statuses: Counter[str] = Counter()
    momo_gateway_amounts: set[str] = set()
    momo_gateway_redirects = 0
    notable: list[dict[str, Any]] = []
    stripe_init_entries: list[dict[str, Any]] = []
    short_url_re = re.compile(r"https://m\.gcash/s/[A-Za-z0-9_-]+")
    gopay_redirect_re = re.compile(
        r"https://pm-redirects\.stripe\.com/authorize/[^\s\"'<]+",
        re.IGNORECASE,
    )
    for index, entry in selected:
        summary = entry_summary(index, entry, redact=redact)
        hosts[summary["host"]] += 1
        statuses[str(summary["status"])] += 1
        methods[summary["method"]] += 1
        headers = summary["request_headers"]
        if "oai-client-build-number" in headers:
            client_builds.add(str(headers["oai-client-build-number"]))
        if "oai-client-version" in headers:
            client_versions.add(str(headers["oai-client-version"]))
        if "user-agent" in headers:
            user_agents.add(str(headers["user-agent"]))
        sentinel = header_dict((entry.get("request") or {}).get("headers"))
        token = sentinel.get("openai-sentinel-token") or sentinel.get("openai-sentinel-so-token")
        if token:
            sentinel_lengths.append(len(token))
        body_text = json.dumps(summary.get("response_body"), ensure_ascii=False)
        short_urls.extend(short_url_re.findall(body_text))
        # GoPay observations: authorize redirect (request url / redirect header /
        # response body), payment method types, amount.
        response_redirect = str(summary.get("redirect_url") or "")
        raw_request_url = str((entry.get("request") or {}).get("url") or "")
        raw_response_headers = header_dict((entry.get("response") or {}).get("headers"))
        raw_redirect = str(
            (entry.get("response") or {}).get("redirectURL")
            or raw_response_headers.get("location")
            or ""
        )
        raw_response_body = str(
            ((entry.get("response") or {}).get("content") or {}).get("text") or ""
        )
        redirect_haystack = (
            raw_request_url + "\n" + raw_redirect + "\n" + raw_response_body
        )
        gopay_redirects.extend(
            {
                "index": index,
                "sha256": hashlib.sha256(match.group(0).encode("utf-8")).hexdigest()[:32],
                "host": "pm-redirects.stripe.com",
                "path_prefix": "/authorize/",
                "url_redacted": redact_url(match.group(0), redact=redact),
            }
            for match in gopay_redirect_re.finditer(redirect_haystack)
        )
        if summary["host"] == "api.stripe.com":
            method_groups = re.findall(r'"payment_method_types"\s*:\s*\[([^\]]*)\]', body_text)
            gopay_payment_methods.update(
                str(item).strip().lower()
                for group in method_groups
                for item in re.findall(r'"(gopay|card|link)"', group)
            )
            gopay_amounts.update(
                str(value)
                for value in re.findall(
                    r'"(?:amount_total|checkout_amount|expected_amount|amount)":\s*"?(\d+)"?',
                    body_text,
                )
            )
        if summary["host"].lower() == "payment.momo.vn":
            raw_response = str(
                ((entry.get("response") or {}).get("content") or {}).get("text") or ""
            )
            try:
                momo_payload = json.loads(raw_response) if raw_response else {}
            except (TypeError, json.JSONDecodeError):
                momo_payload = {}
            if isinstance(momo_payload, dict):
                status_value = momo_payload.get("status_code")
                if status_value not in (None, ""):
                    momo_gateway_statuses[str(status_value)] += 1
                for key in ("amount", "amount_total", "amountTotal"):
                    value = momo_payload.get(key)
                    if value not in (None, ""):
                        momo_gateway_amounts.add(str(value))
                return_url = str(momo_payload.get("return_url") or "")
                if return_url:
                    for value in dict(parse_qsl(urlsplit(return_url).query)).get(
                        "amount", ""
                    ).split(","):
                        if value != "":
                            momo_gateway_amounts.add(str(value))
                if bool(momo_payload.get("redirect")):
                    momo_gateway_redirects += 1
        operation = ""
        request_body = str((entry.get("request") or {}).get("postData", {}).get("text", ""))
        for marker in (
            "sentinel/req",
            "checkout/custom_payment_method/start",
            "checkout/confirm",
            "key-agreement",
            "authorisation.stateless.consult",
            "short.dynamic.link",
            "query.result",
            "querySession",
            "payments/checkout/approve",
            "payments/checkout/snapshot",
            "payment_pages/",
        ):
            if marker.lower() in (summary["url"] + " " + request_body).lower():
                operation = marker
                break
        if operation:
            notable.append({"index": index, "operation": operation, "status": summary["status"], "url": summary["url"]})
        if summary["host"] == "api.stripe.com" and summary["path"].endswith("/init"):
            stripe_init_entries.append(
                {
                    "index": index,
                    "checkpoint": "gopay_stripe_init",
                    "status": summary["status"],
                    "path": summary["path"],
                }
            )

    return {
        "schema": "opll.har-analysis.v1",
        "channel": channel,
        "source": str(Path(path).resolve()),
        "sha256": sha256,
        "har_version": har.get("log", {}).get("version", ""),
        "entry_count": len(all_entries),
        "selected_count": len(selected),
        "filters": {
            "host": host,
            "path": path_contains,
            "method": method,
            "status": status,
            "contains": contains,
            "limit": limit,
            "redact": redact,
        },
        "counts": {
            "hosts": dict(hosts),
            "statuses": dict(statuses),
            "methods": dict(methods),
        },
        "observations": {
            "oai_client_build_numbers": sorted(client_builds),
            "oai_client_versions": sorted(client_versions),
            "user_agents": sorted(user_agents),
            "sentinel_header_lengths": sorted(set(sentinel_lengths)),
            "short_urls": sorted(set(short_urls)),
            "gopay_redirects": gopay_redirects,
            "gopay_payment_methods": sorted(gopay_payment_methods),
            "gopay_amounts": sorted(gopay_amounts),
            "momo_gateway_statuses": dict(momo_gateway_statuses),
            "momo_gateway_amounts": sorted(momo_gateway_amounts),
            "momo_gateway_redirects": momo_gateway_redirects,
            "gopay_stripe_init": stripe_init_entries,
            "notable_operations": notable,
        },
        "entries": summaries,
    }


def markdown_report(report: dict[str, Any]) -> str:
    counts = report.get("counts", {})
    observations = report.get("observations", {})
    channel = str(report.get("channel") or "").lower()
    if channel == "momo":
        channel_lines = [
            f"- MoMo gateway redirects (#): `{observations.get('momo_gateway_redirects', 0)}`",
            f"- MoMo gateway statuses: `{json.dumps(observations.get('momo_gateway_statuses', {}), ensure_ascii=False)}`",
            f"- MoMo gateway amounts: `{', '.join(observations.get('momo_gateway_amounts', [])) or '-'}`",
        ]
    else:
        channel_lines = [
            f"- GoPay redirects (#): `{len(observations.get('gopay_redirects', []))}`",
            f"- GoPay payment methods: `{', '.join(observations.get('gopay_payment_methods', [])) or '-'}`",
            f"- GoPay amounts: `{', '.join(observations.get('gopay_amounts', [])) or '-'}`",
        ]
    lines = [
        "# HAR analysis",
        "",
        f"- Source: `{report.get('source', '')}`",
        f"- SHA-256: `{report.get('sha256', '')}`",
        f"- Entries: `{report.get('entry_count', 0)}` (selected `{report.get('selected_count', 0)}`)",
        "",
        "## Counts",
        "",
        f"- Hosts: `{json.dumps(counts.get('hosts', {}), ensure_ascii=False)}`",
        f"- Statuses: `{json.dumps(counts.get('statuses', {}), ensure_ascii=False)}`",
        f"- Methods: `{json.dumps(counts.get('methods', {}), ensure_ascii=False)}`",
        "",
        "## Observations",
        "",
        f"- Client builds: `{', '.join(observations.get('oai_client_build_numbers', [])) or '-'}`",
        f"- Client versions: `{', '.join(observations.get('oai_client_versions', [])) or '-'}`",
        f"- Sentinel header lengths: `{', '.join(map(str, observations.get('sentinel_header_lengths', []))) or '-'}`",
        f"- HAR-observed short URLs: `{', '.join(observations.get('short_urls', [])) or '-'}`",
        *channel_lines,
        "",
        "## Notable operations",
        "",
        "| Index | Operation | Status | URL |",
        "|---:|---|---:|---|",
    ]
    for item in observations.get("notable_operations", []):
        lines.append(f"| {item.get('index')} | `{item.get('operation')}` | {item.get('status')} | `{item.get('url')}` |")
    gopay_redirects = observations.get("gopay_redirects", [])
    if gopay_redirects and channel != "momo":
        lines.extend(
            [
                "",
                "## GoPay authorize redirects",
                "",
                "| Index | sha256 | Host | Path | Redacted URL |",
                "|---:|---|---:|---|---|",
            ]
        )
        for item in gopay_redirects:
            lines.append(
                f"| {item.get('index')} | `{item.get('sha256')}` | `{item.get('host')}` | "
                f"`{item.get('path_prefix')}` | `{item.get('url_redacted')}` |"
            )
    lines.extend(["", "## Entries", "", "| Index | Method | Status | Host | Path | Time (ms) |", "|---:|---|---:|---|---|---:|"])
    for item in report.get("entries", []):
        lines.append(
            f"| {item.get('index')} | {item.get('method')} | {item.get('status')} | "
            f"`{item.get('host')}` | `{item.get('path')}` | {item.get('time_ms')} |"
        )
    return "\n".join(lines) + "\n"
