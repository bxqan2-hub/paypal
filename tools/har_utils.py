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
    r"requestdata|^p$|qr[_-]?code|session[_-]?token)",
    re.IGNORECASE,
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
    if redact and SENSITIVE_NAME_RE.search(str(name)):
        return f"<redacted len={len(text)} sha256={_short_hash(text)}>"
    if len(text) > MAX_INLINE_TEXT:
        return f"<truncated len={len(text)} sha256={_short_hash(text)} preview={text[:120]!r}>"
    return value


def redact_url(url: str, *, redact: bool = True) -> str:
    if not redact:
        return url
    parts = urlsplit(url)
    query = []
    for name, value in parse_qsl(parts.query, keep_blank_values=True):
        query.append((name, redact_value(name, value, redact=True)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


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
    return {
        "index": index,
        "startedDateTime": entry.get("startedDateTime"),
        "time_ms": round(float(entry.get("time", 0) or 0), 2),
        "method": str(request.get("method") or "GET").upper(),
        "url": redact_url(url, redact=redact),
        "host": parts.netloc,
        "path": parts.path or "/",
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
        "redirect_url": redact_url(str(response.get("redirectURL") or ""), redact=redact),
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
    notable: list[dict[str, Any]] = []
    short_url_re = re.compile(r"https://m\.gcash/s/[A-Za-z0-9_-]+")
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
        ):
            if marker.lower() in (summary["url"] + " " + request_body).lower():
                operation = marker
                break
        if operation:
            notable.append({"index": index, "operation": operation, "status": summary["status"], "url": summary["url"]})

    return {
        "schema": "opll.har-analysis.v1",
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
            "notable_operations": notable,
        },
        "entries": summaries,
    }


def markdown_report(report: dict[str, Any]) -> str:
    counts = report.get("counts", {})
    observations = report.get("observations", {})
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
        "",
        "## Notable operations",
        "",
        "| Index | Operation | Status | URL |",
        "|---:|---|---:|---|",
    ]
    for item in observations.get("notable_operations", []):
        lines.append(f"| {item.get('index')} | `{item.get('operation')}` | {item.get('status')} | `{item.get('url')}` |")
    lines.extend(["", "## Entries", "", "| Index | Method | Status | Host | Path | Time (ms) |", "|---:|---|---:|---|---|---:|"])
    for item in report.get("entries", []):
        lines.append(
            f"| {item.get('index')} | {item.get('method')} | {item.get('status')} | "
            f"`{item.get('host')}` | `{item.get('path')}` | {item.get('time_ms')} |"
        )
    return "\n".join(lines) + "\n"
