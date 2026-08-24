from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import re
import select
import secrets
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen


_COUNTRY_PROFILES: dict[str, dict[str, str]] = {
    "PH": {"lang": "en-US", "accept_lang": "en-US,en", "timezone": "Asia/Manila"},
    "US": {"lang": "en-US", "accept_lang": "en-US,en", "timezone": "America/New_York"},
    "GB": {"lang": "en-GB", "accept_lang": "en-GB,en", "timezone": "Europe/London"},
    "CA": {"lang": "en-CA", "accept_lang": "en-CA,en", "timezone": "America/Toronto"},
    "AU": {"lang": "en-AU", "accept_lang": "en-AU,en", "timezone": "Australia/Sydney"},
    "SG": {"lang": "en-SG", "accept_lang": "en-SG,en", "timezone": "Asia/Singapore"},
    "MY": {"lang": "en-MY", "accept_lang": "en-MY,en", "timezone": "Asia/Kuala_Lumpur"},
    "IN": {"lang": "en-IN", "accept_lang": "en-IN,en", "timezone": "Asia/Kolkata"},
    "JP": {"lang": "ja-JP", "accept_lang": "ja-JP,ja,en", "timezone": "Asia/Tokyo"},
    "KR": {"lang": "ko-KR", "accept_lang": "ko-KR,ko,en", "timezone": "Asia/Seoul"},
    "CN": {"lang": "zh-CN", "accept_lang": "zh-CN,zh,en", "timezone": "Asia/Shanghai"},
    "HK": {"lang": "en-HK", "accept_lang": "en-HK,en,zh-HK", "timezone": "Asia/Hong_Kong"},
    "TW": {"lang": "zh-TW", "accept_lang": "zh-TW,zh,en", "timezone": "Asia/Taipei"},
}


def infer_proxy_country(value: str) -> str:
    """Extract a two-letter region marker without exposing proxy credentials."""
    markers = re.findall(r"(?:region|country)[-_:=]([a-z]{2})(?:[-_:.]|$)", value, flags=re.IGNORECASE)
    return markers[-1].upper() if markers else "UN"


def locale_profile_for_proxy(value: str) -> tuple[str, dict[str, str]]:
    country = infer_proxy_country(value)
    return country, dict(_COUNTRY_PROFILES.get(country, _COUNTRY_PROFILES["US"]))


class CDPWebSocket:
    def __init__(self, url: str, *, timeout: float = 10.0) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or not parsed.hostname or not parsed.port:
            raise ValueError(f"unsupported CDP websocket URL: {url}")
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=timeout)
        self.sock.settimeout(timeout)
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = self._read_http_headers()
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            self.sock.close()
            raise RuntimeError(f"CDP websocket handshake failed: {response[:200]!r}")
        self.next_id = 0

    def _read_http_headers(self) -> bytes:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 65536:
                raise RuntimeError("CDP handshake headers are too large")
        return bytes(data)

    def _recv_exact(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise EOFError("CDP websocket closed")
            data.extend(chunk)
        return bytes(data)

    def recv(self) -> tuple[int, bytes]:
        first, second = self._recv_exact(2)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        size = second & 0x7F
        if size == 126:
            size = int.from_bytes(self._recv_exact(2), "big")
        elif size == 127:
            size = int.from_bytes(self._recv_exact(8), "big")
        mask = self._recv_exact(4) if masked else b""
        payload = bytearray(self._recv_exact(size))
        if masked:
            for index in range(size):
                payload[index] ^= mask[index % 4]
        return opcode, bytes(payload)

    def send(self, payload: bytes, *, opcode: int = 1) -> None:
        size = len(payload)
        header = bytearray([0x80 | opcode])
        if size < 126:
            header.append(0x80 | size)
        elif size < 65536:
            header.append(0x80 | 126)
            header.extend(size.to_bytes(2, "big"))
        else:
            header.append(0x80 | 127)
            header.extend(size.to_bytes(8, "big"))
        mask = secrets.token_bytes(4)
        header.extend(mask)
        self.sock.sendall(bytes(header) + bytes(value ^ mask[index % 4] for index, value in enumerate(payload)))

    def send_json(self, value: dict[str, Any]) -> None:
        self.send(json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def recv_json(self) -> dict[str, Any]:
        fragments: list[bytes] = []
        while True:
            opcode, payload = self.recv()
            if opcode == 8:
                raise EOFError("CDP websocket close frame")
            if opcode == 9:
                self.send(payload, opcode=10)
                continue
            if opcode in (1, 0):
                fragments.append(payload)
                if opcode == 1 or fragments:
                    try:
                        value = json.loads(b"".join(fragments).decode("utf-8"))
                        fragments.clear()
                        return value
                    except json.JSONDecodeError:
                        continue

    def close(self) -> None:
        try:
            self.send(b"", opcode=8)
        except Exception:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _iso_from_wall_time(value: Any) -> str:
    try:
        stamp = float(value)
    except (TypeError, ValueError):
        stamp = time.time()
    return dt.datetime.fromtimestamp(stamp, tz=dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _header_list(value: Any) -> list[dict[str, str]]:
    if isinstance(value, dict):
        return [{"name": str(name), "value": str(item)} for name, item in value.items()]
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _query_list(url: str) -> list[dict[str, str]]:
    from urllib.parse import parse_qsl

    return [{"name": name, "value": value} for name, value in parse_qsl(urlsplit(url).query, keep_blank_values=True)]


class HARRecorder:
    def __init__(
        self,
        cdp: CDPWebSocket,
        *,
        max_body_bytes: int = 8 * 1024 * 1024,
        response_body_retries: int = 3,
        stream_responses: bool = True,
    ) -> None:
        self.cdp = cdp
        self.max_body_bytes = max_body_bytes
        self.response_body_retries = max(1, response_body_retries)
        self.stream_responses = stream_responses
        self.entries: list[dict[str, Any]] = []
        self.states: dict[str, dict[str, Any]] = {}
        self._commands: dict[int, dict[str, Any]] = {}
        self._fetch_bodies: dict[str, dict[str, Any]] = {}

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.cdp.next_id += 1
        command_id = self.cdp.next_id
        self.cdp.send_json({"id": command_id, "method": method, "params": params or {}})
        sock = getattr(self.cdp, "sock", None)
        previous_timeout = sock.gettimeout() if sock is not None and hasattr(sock, "gettimeout") else None
        if sock is not None and previous_timeout is not None and previous_timeout < 10:
            sock.settimeout(10)
        try:
            while True:
                message = self._commands.pop(command_id, None) or self.cdp.recv_json()
                response_id = message.get("id")
                if response_id is not None:
                    if response_id != command_id:
                        # A command can be issued while handling an event received by an
                        # outer command. Preserve the outer response instead of dropping it.
                        self._commands[int(response_id)] = message
                        continue
                    if "error" in message:
                        raise RuntimeError(f"CDP {method} failed: {message['error']}")
                    return message.get("result", {})
                self.handle(message)
        finally:
            if sock is not None and previous_timeout is not None:
                sock.settimeout(previous_timeout)

    def handle(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "Fetch.requestPaused":
            self._capture_paused_response(params)
        elif method == "Network.requestWillBeSent":
            request_id = str(params.get("requestId") or "")
            if not request_id:
                return
            if params.get("redirectResponse") and request_id in self.states:
                previous = self.states.pop(request_id)
                if not previous.get("body"):
                    stream_body, stream_encoded = self._stream_fallback(previous)
                    previous["body"] = stream_body
                    previous["base64Encoded"] = stream_encoded
                    previous["body_source"] = "Network.streamResourceContent" if stream_body else ""
                self._finish(previous, response=params.get("redirectResponse"), timestamp=params.get("timestamp"))
            request = params.get("request") if isinstance(params.get("request"), dict) else {}
            self.states[request_id] = {
                "request_id": request_id,
                "request_event": params,
                "request": request,
                "response": None,
                "finish": None,
                "error": "",
                "stream_body": bytearray(),
                "stream_tail": bytearray(),
                "stream_starting": False,
                "stream_truncated": False,
                "stream_error": "",
                "fetch_body": self._fetch_bodies.pop(request_id, None),
            }
        elif method == "Network.responseReceived":
            request_id = str(params.get("requestId") or "")
            state = self.states.get(request_id)
            if state:
                state["response"] = params.get("response") if isinstance(params.get("response"), dict) else {}
                state["response_event"] = params
                self._begin_response_stream(state)
        elif method == "Network.dataReceived":
            request_id = str(params.get("requestId") or "")
            state = self.states.get(request_id)
            data = params.get("data")
            if state and isinstance(data, str) and data:
                try:
                    chunk = base64.b64decode(data, validate=True)
                except Exception:
                    chunk = b""
                self._append_stream_bytes(state, chunk, tail=bool(state.get("stream_starting")))
        elif method == "Network.loadingFailed":
            request_id = str(params.get("requestId") or "")
            state = self.states.get(request_id)
            if state:
                state["error"] = str(params.get("errorText") or params.get("blockedReason") or "loading failed")
                self._finish(state, timestamp=params.get("timestamp"))
                self.states.pop(request_id, None)
        elif method == "Network.loadingFinished":
            request_id = str(params.get("requestId") or "")
            current = self.states.get(request_id)
            if current and current.get("stream_starting"):
                current["deferred_finish"] = params
                return
            state = self.states.pop(request_id, None)
            if state:
                state["finish"] = params
                self._fill_missing_request_body(state)
                self._fill_response_body(state)
                self._finish(state, timestamp=params.get("timestamp"))

    def _capture_paused_response(self, params: dict[str, Any]) -> None:
        fetch_id = str(params.get("requestId") or "")
        network_id = str(params.get("networkId") or "")
        if not fetch_id:
            return
        captured: dict[str, Any] = {"body": "", "base64Encoded": False, "errors": []}
        try:
            if params.get("responseStatusCode") is not None or params.get("responseErrorReason") is not None:
                for attempt in range(self.response_body_retries):
                    try:
                        result = self.command("Fetch.getResponseBody", {"requestId": fetch_id})
                        captured["body"] = str(result.get("body") or "")
                        captured["base64Encoded"] = bool(result.get("base64Encoded"))
                        if captured["body"]:
                            break
                    except Exception as exc:
                        captured["errors"].append(str(exc))
                    if attempt + 1 < self.response_body_retries:
                        time.sleep(0.02 * (attempt + 1))
        finally:
            try:
                self.command("Fetch.continueRequest", {"requestId": fetch_id})
            except Exception as exc:
                captured["errors"].append(str(exc))
        if network_id:
            state = self.states.get(network_id)
            if state is not None:
                state["fetch_body"] = captured
            else:
                self._fetch_bodies[network_id] = captured

    def _append_stream_bytes(self, state: dict[str, Any], chunk: bytes, *, tail: bool = False) -> None:
        if not chunk or self.max_body_bytes <= 0:
            return
        key = "stream_tail" if tail else "stream_body"
        target = state.get(key)
        if not isinstance(target, bytearray):
            target = bytearray()
            state[key] = target
        current = state.get("stream_body")
        other = state.get("stream_tail")
        current_size = len(current) if isinstance(current, bytearray) else 0
        other_size = len(other) if isinstance(other, bytearray) else 0
        available = max(0, self.max_body_bytes - current_size - other_size)
        target.extend(chunk[:available])
        if len(chunk) > available:
            state["stream_truncated"] = True

    def _begin_response_stream(self, state: dict[str, Any]) -> None:
        """Stream response bytes as a fallback when CDP later evicts the body."""
        if not self.stream_responses or self.max_body_bytes <= 0:
            return
        request_id = str(state.get("request_id") or "")
        if not request_id:
            return
        state["stream_starting"] = True
        try:
            result = self.command("Network.streamResourceContent", {"requestId": request_id})
            buffered = str(result.get("bufferedData") or "") if isinstance(result, dict) else ""
            prefix = base64.b64decode(buffered, validate=True) if buffered else b""
            tail = state.get("stream_tail")
            state["stream_tail"] = bytearray()
            self._append_stream_bytes(state, prefix)
            if isinstance(tail, bytearray):
                self._append_stream_bytes(state, bytes(tail))
        except Exception as exc:
            state["stream_error"] = str(exc)
        finally:
            state["stream_starting"] = False
        deferred = state.pop("deferred_finish", None)
        request_id = str(state.get("request_id") or "")
        if isinstance(deferred, dict) and request_id in self.states:
            self.handle({"method": "Network.loadingFinished", "params": deferred})

    def _stream_fallback(self, state: dict[str, Any]) -> tuple[str, bool]:
        stream_body = state.get("stream_body")
        stream_tail = state.get("stream_tail")
        raw = bytes(stream_body) if isinstance(stream_body, bytearray) else b""
        if isinstance(stream_tail, bytearray) and stream_tail:
            raw += bytes(stream_tail)
        if not raw:
            return "", False
        return base64.b64encode(raw).decode("ascii"), True

    def _fill_response_body(self, state: dict[str, Any]) -> None:
        request_id = str(state.get("request_id") or "")
        fetch_body = state.get("fetch_body") if isinstance(state.get("fetch_body"), dict) else {}
        body = str(fetch_body.get("body") or "")
        encoded = bool(fetch_body.get("base64Encoded"))
        errors: list[str] = []
        errors.extend(str(item) for item in fetch_body.get("errors", []) if item)
        attempts = 0
        source = "Fetch.getResponseBody" if body else ""
        if not body:
            for attempt in range(self.response_body_retries):
                attempts = attempt + 1
                try:
                    result = self.command("Network.getResponseBody", {"requestId": request_id})
                    body = str(result.get("body") or "")
                    encoded = bool(result.get("base64Encoded"))
                    if body:
                        break
                except Exception as exc:
                    errors.append(str(exc))
                if attempt + 1 < self.response_body_retries:
                    time.sleep(0.02 * (attempt + 1))
            if body:
                source = "Network.getResponseBody"
        if not body:
            body, encoded = self._stream_fallback(state)
            if body:
                source = "Network.streamResourceContent"
        state["body"] = body
        state["base64Encoded"] = encoded
        state["body_source"] = source
        state["body_attempts"] = attempts
        if errors:
            state["body_errors"] = errors

    def _fill_missing_request_body(self, state: dict[str, Any]) -> None:
        """Ask CDP for POST data omitted from requestWillBeSent (seen in RoxyChrome)."""
        request = state.get("request") if isinstance(state.get("request"), dict) else {}
        method = str(request.get("method") or "").upper()
        if method not in {"POST", "PUT", "PATCH"} or request.get("postData"):
            return
        request_id = str(state.get("request_id") or "")
        if not request_id:
            return
        try:
            result = self.command("Network.getRequestPostData", {"requestId": request_id})
            post_data = result.get("postData") if isinstance(result, dict) else None
            if post_data:
                request["postData"] = str(post_data)
        except Exception:
            # Chrome legitimately returns an error for bodyless POSTs or after a redirect.
            return

    def flush_pending(self) -> None:
        """Materialize requests still in flight when capture stops."""
        pending = list(self.states.values())
        self.states.clear()
        for state in pending:
            self._fill_missing_request_body(state)
            if state.get("response"):
                try:
                    self._fill_response_body(state)
                except Exception:
                    pass
            self._finish(state)

    @staticmethod
    def _expects_response_body(request: dict[str, Any], response: dict[str, Any]) -> bool:
        method = str(request.get("method") or "").upper()
        status = int(response.get("status", 0) or 0)
        if method == "HEAD" or status in {0, 101, 204, 205, 304}:
            return False
        headers = {
            str(item.get("name", "")).lower(): str(item.get("value", ""))
            for item in _header_list(response.get("headers"))
        }
        content_length = headers.get("content-length", "").strip()
        if content_length == "0":
            return False
        try:
            if int(content_length) > 0:
                return True
        except ValueError:
            pass
        mime_type = str(response.get("mimeType") or headers.get("content-type", "")).lower()
        return status >= 200 and status < 300 and any(
            marker in mime_type for marker in ("json", "text/", "javascript", "xml", "html")
        )

    def _finish(self, state: dict[str, Any], *, response: dict[str, Any] | None = None, timestamp: Any = None) -> None:
        request = state.get("request") if isinstance(state.get("request"), dict) else {}
        response = response or (state.get("response") if isinstance(state.get("response"), dict) else {})
        request_event = state.get("request_event") if isinstance(state.get("request_event"), dict) else {}
        start_timestamp = request_event.get("timestamp")
        try:
            duration = max(0.0, (float(timestamp) - float(start_timestamp)) * 1000.0)
        except (TypeError, ValueError):
            duration = 0.0
        body = str(state.get("body") or "")
        original_body = body
        truncated = False
        is_base64 = bool(state.get("base64Encoded"))
        if is_base64:
            try:
                raw_body = base64.b64decode(original_body, validate=True)
            except Exception:
                is_base64 = False
                raw_body = original_body.encode("utf-8", errors="replace")
            if len(raw_body) > self.max_body_bytes:
                raw_body = raw_body[: self.max_body_bytes]
                truncated = True
            body = base64.b64encode(raw_body).decode("ascii") if is_base64 else raw_body.decode("utf-8", errors="replace")
        elif len(body.encode("utf-8", errors="replace")) > self.max_body_bytes:
            body = body.encode("utf-8", errors="replace")[: self.max_body_bytes].decode("utf-8", errors="replace")
            truncated = True
        request_body = request.get("postData")
        if isinstance(request_body, dict):
            request_body = request_body.get("text", "")
        request_body = str(request_body or "")
        request_headers = _header_list(request.get("headers"))
        request_content_type = next(
            (item.get("value", "") for item in request_headers if str(item.get("name", "")).lower() == "content-type"),
            "",
        )
        content_size = len(body.encode("utf-8", errors="replace"))
        if is_base64:
            content_size = len(base64.b64decode(body, validate=True)) if body else 0
        content: dict[str, Any] = {
            "size": content_size,
            "mimeType": str(response.get("mimeType") or "application/octet-stream"),
        }
        if body:
            content["text"] = body
            if is_base64:
                content["encoding"] = "base64"
        if truncated:
            content["_captureTruncated"] = True
        response_headers = _header_list(response.get("headers"))
        location = next(
            (item.get("value", "") for item in response_headers if str(item.get("name", "")).lower() == "location"),
            "",
        )
        entry = {
            "startedDateTime": _iso_from_wall_time(request_event.get("wallTime")),
            "time": round(duration, 2),
            "request": {
                "method": str(request.get("method") or "GET"),
                "url": str(request.get("url") or ""),
                "httpVersion": "HTTP/1.1",
                "headers": request_headers,
                "queryString": _query_list(str(request.get("url") or "")),
                "cookies": [],
                "headersSize": -1,
                "bodySize": len(request_body.encode("utf-8")),
            },
            "response": {
                "status": int(response.get("status", 0) or 0),
                "statusText": str(response.get("statusText") or ""),
                "httpVersion": "HTTP/1.1",
                "headers": response_headers,
                "cookies": [],
                "content": content,
                "redirectURL": str(location) if int(response.get("status", 0) or 0) in {301, 302, 303, 307, 308} else "",
                "headersSize": -1,
                "bodySize": max(0, int(response.get("encodedDataLength", content["size"]) or content["size"])),
            },
            "cache": {},
            "timings": {"send": 0, "wait": round(duration, 2), "receive": 0},
        }
        if request_body:
            entry["request"]["postData"] = {
                "mimeType": request_content_type,
                "text": request_body,
            }
        if state.get("error"):
            entry["_error"] = state["error"]
        capture_detail: dict[str, Any] = {
            "responseBodySource": str(state.get("body_source") or "none"),
            "responseBodyAttempts": int(state.get("body_attempts") or 0),
        }
        if state.get("stream_truncated"):
            capture_detail["streamTruncated"] = True
        if state.get("body_errors"):
            capture_detail["responseBodyErrors"] = list(state["body_errors"])
        if state.get("stream_error") and not body:
            capture_detail["streamError"] = str(state["stream_error"])
        if not body and self._expects_response_body(request, response):
            capture_detail["responseBodyMissing"] = True
        entry["_capture"] = capture_detail
        self.entries.append(entry)

    def as_har(self, *, title: str) -> dict[str, Any]:
        entries = sorted(self.entries, key=lambda item: str(item.get("startedDateTime", "")))
        return {
            "log": {
                "version": "1.2",
                "creator": {"name": "opll-har-capture", "version": "1.0"},
                "pages": [],
                "entries": entries,
                "_capture": {"title": title, "entryCount": len(entries)},
            }
        }


def audit_har_completeness(har: dict[str, Any]) -> dict[str, Any]:
    """Audit a GCash flow without exposing request or response payload values."""
    log = har.get("log") if isinstance(har.get("log"), dict) else {}
    entries = log.get("entries") if isinstance(log.get("entries"), list) else []

    def request_text(entry: dict[str, Any]) -> str:
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        post_data = request.get("postData") if isinstance(request.get("postData"), dict) else {}
        return str(post_data.get("text") or "")

    def response_text(entry: dict[str, Any]) -> str:
        response = entry.get("response") if isinstance(entry.get("response"), dict) else {}
        content = response.get("content") if isinstance(response.get("content"), dict) else {}
        return str(content.get("text") or "")

    def searchable(entry: dict[str, Any]) -> str:
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        return (str(request.get("url") or "") + "\n" + request_text(entry)).lower()

    critical = [
        ("checkout_taxes", "/backend-api/payments/checkout/taxes"),
        ("checkout_confirm", "/backend-api/payments/checkout/confirm"),
        ("custom_payment_method_start", "/backend-api/payments/checkout/custom_payment_method/start"),
        ("sentinel_req", "/backend-api/sentinel/req"),
        ("key_agreement", "/c4/v3/key-agreement/handshake"),
        ("authorisation_consult", "ap.mobilewallet.gka.authorisation.stateless.consult"),
        ("short_dynamic_link", "ap.mobilewallet.short.dynamic.link"),
        ("query_result", "ap.mobilewallet.gka.query.result"),
    ]
    critical_results: list[dict[str, Any]] = []
    issues: list[str] = []
    for name, marker in critical:
        matches = [entry for entry in entries if isinstance(entry, dict) and marker in searchable(entry)]
        request_body = any(bool(request_text(entry)) for entry in matches)
        response_body = any(bool(response_text(entry)) for entry in matches)
        result = {
            "name": name,
            "count": len(matches),
            "requestBody": request_body,
            "responseBody": response_body,
        }
        critical_results.append(result)
        if not matches:
            issues.append(f"{name}:entry_missing")
        elif not request_body:
            issues.append(f"{name}:request_body_missing")
        elif not response_body:
            issues.append(f"{name}:response_body_missing")

    missing_responses: list[dict[str, Any]] = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            continue
        detail = entry.get("_capture") if isinstance(entry.get("_capture"), dict) else {}
        if not detail.get("responseBodyMissing"):
            continue
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        parsed = urlsplit(str(request.get("url") or ""))
        missing_responses.append(
            {
                "index": index,
                "method": str(request.get("method") or ""),
                "host": str(parsed.hostname or ""),
                "path": str(parsed.path or ""),
            }
        )

    audit = {
        "complete": not issues and not missing_responses,
        "criticalComplete": not issues,
        "entryCount": len(entries),
        "critical": critical_results,
        "issues": issues,
        "missingExpectedResponses": missing_responses,
    }
    capture = log.get("_capture") if isinstance(log.get("_capture"), dict) else None
    if capture is not None:
        capture["completenessAudit"] = audit
    return audit


def network_enable_params(max_body_bytes: int) -> dict[str, Any]:
    per_resource = max(max_body_bytes, 1024 * 1024)
    return {
        "maxTotalBufferSize": max(per_resource * 16, 16 * 1024 * 1024),
        "maxResourceBufferSize": per_resource,
        "maxPostDataSize": per_resource,
        "enableDurableMessages": True,
    }


def _find_browser(explicit: str = "") -> str:
    candidates = [explicit] if explicit else []
    candidates.extend(
        [
            os.getenv("OPLL_CHROME_BIN", ""),
            os.getenv("CHROME_BIN", ""),
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
    )
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    raise FileNotFoundError("Chrome/Edge not found; pass --browser C:\\path\\to\\chrome.exe")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _read_until_headers(sock: socket.socket, *, limit: int = 65536) -> bytes:
    data = bytearray()
    while b"\r\n\r\n" not in data:
        part = sock.recv(4096)
        if not part:
            break
        data.extend(part)
        if len(data) > limit:
            raise RuntimeError("proxy headers are too large")
    return bytes(data)


def _recv_exact_socket(sock: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        part = sock.recv(size - len(data))
        if not part:
            raise RuntimeError("SOCKS5 connection closed during handshake")
        data.extend(part)
    return bytes(data)


def _socks5_connect(sock: socket.socket, host: str, port: int, username: str, password: str) -> None:
    if username:
        sock.sendall(b"\x05\x01\x02")
        if _recv_exact_socket(sock, 2) != b"\x05\x02":
            raise RuntimeError("SOCKS5 username/password authentication was rejected")
        user = username.encode("utf-8")
        secret = password.encode("utf-8")
        if len(user) > 255 or len(secret) > 255:
            raise ValueError("SOCKS5 credentials are too long")
        sock.sendall(b"\x01" + bytes([len(user)]) + user + bytes([len(secret)]) + secret)
        if _recv_exact_socket(sock, 2) != b"\x01\x00":
            raise RuntimeError("SOCKS5 authentication failed")
    else:
        sock.sendall(b"\x05\x01\x00")
        if _recv_exact_socket(sock, 2) != b"\x05\x00":
            raise RuntimeError("SOCKS5 no-authentication mode was rejected")
    encoded_host = host.encode("idna")
    if len(encoded_host) > 255:
        raise ValueError("destination hostname is too long")
    sock.sendall(b"\x05\x01\x00\x03" + bytes([len(encoded_host)]) + encoded_host + struct.pack("!H", port))
    reply = _recv_exact_socket(sock, 4)
    if len(reply) != 4 or reply[1] != 0:
        code = reply[1] if len(reply) > 1 else -1
        raise RuntimeError(f"SOCKS5 destination connection failed ({code})")
    if reply[3] == 1:
        address_size = 4
    elif reply[3] == 4:
        address_size = 16
    elif reply[3] == 3:
        length = _recv_exact_socket(sock, 1)
        address_size = length[0]
    else:
        raise RuntimeError("SOCKS5 reply has an unknown address type")
    remaining = address_size + 2
    while remaining:
        part = sock.recv(remaining)
        if not part:
            raise RuntimeError("SOCKS5 reply ended before the bound address")
        remaining -= len(part)


class _Socks5BridgeHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server = self.server
        assert isinstance(server, _Socks5BridgeServer)
        client = self.request
        client.settimeout(20)
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        client.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        upstream: socket.socket | None = None
        try:
            header = _read_until_headers(client)
            first_line = header.decode("latin-1", errors="replace").split("\r\n", 1)[0]
            parts = first_line.split(" ", 2)
            if len(parts) < 2 or parts[0].upper() != "CONNECT":
                client.sendall(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                return
            host, separator, port_text = parts[1].rpartition(":")
            if not separator:
                host, port_text = parts[1], "443"
            upstream = socket.create_connection((server.proxy_host, server.proxy_port), timeout=20)
            upstream.settimeout(20)
            upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            upstream.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            _socks5_connect(upstream, host.strip("[]"), int(port_text), server.proxy_user, server.proxy_password)
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            client.settimeout(None)
            upstream.settimeout(None)
            while True:
                readable, _, _ = select.select([client, upstream], [], [], 60)
                if not readable:
                    continue
                for source in readable:
                    payload = source.recv(65536)
                    if not payload:
                        return
                    (upstream if source is client else client).sendall(payload)
        except Exception:
            try:
                client.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
            except Exception:
                pass
        finally:
            if upstream:
                upstream.close()


class _Socks5BridgeServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True
    block_on_close = False
    request_queue_size = 128

    def __init__(self, address: tuple[str, int], proxy_host: str, proxy_port: int, proxy_user: str, proxy_password: str) -> None:
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port
        self.proxy_user = proxy_user
        self.proxy_password = proxy_password
        super().__init__(address, _Socks5BridgeHandler)


class Socks5HttpBridge:
    """Expose an authenticated SOCKS5 proxy as a local unauthenticated HTTP CONNECT proxy."""

    def __init__(self, value: str) -> None:
        text = value.strip()
        scheme = "socks5"
        host = ""
        port = 0
        username = ""
        password = ""
        if "://" not in text:
            parts = text.split(":", 3)
            if len(parts) == 4:
                host, port_text, username, password = parts
                port = int(port_text)
            else:
                text = "socks5://" + text
        if not host:
            parsed = urlsplit(text)
            scheme = parsed.scheme.lower()
            host = parsed.hostname or ""
            username = parsed.username or ""
            password = parsed.password or ""
            try:
                port = parsed.port or 0
            except ValueError:
                # Also accept socks5://HOST:PORT:USER:PASSWORD, which is the
                # common four-field export format used by proxy dashboards.
                raw = text.split("://", 1)[-1].split(":", 3)
                if len(raw) == 4:
                    host, port_text, username, password = raw
                    port = int(port_text)
        if scheme not in {"socks5", "socks5h"} or not host or not port:
            raise ValueError("--socks5-proxy must be socks5://host:port:user:password or a host:port:user:password entry")
        self.server = _Socks5BridgeServer(
            ("127.0.0.1", 0),
            host,
            port,
            username,
            password,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, name="har-socks5-bridge", daemon=True)
        self.thread.start()

    @property
    def proxy_server(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def check_socks5_proxy(value: str, url: str = "https://example.com/", *, timeout: float = 15) -> tuple[bool, str, int]:
    """Validate an authenticated SOCKS5 entry through the same bridge Chrome uses."""
    bridge = Socks5HttpBridge(value)
    started = time.perf_counter()
    try:
        opener = build_opener(ProxyHandler({"http": bridge.proxy_server, "https": bridge.proxy_server}))
        request = Request(url, headers={"User-Agent": "opll-har-proxy-check/1.0"})
        with opener.open(request, timeout=timeout) as response:
            response.read(64)
            status = getattr(response, "status", None)
            if status is None:
                status = response.getcode()
            latency_ms = round((time.perf_counter() - started) * 1000)
            status_code = int(status)
            return status_code < 400, str(status_code), latency_ms
    finally:
        bridge.close()


def _wait_json(port: int, path: str, timeout: float) -> Any:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
                return json.loads(response.read().decode("utf-8"))
        except Exception:
            time.sleep(0.2)
    raise TimeoutError(f"Chrome DevTools endpoint did not start: {path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture a manual Chrome/Edge session as HAR 1.2 through CDP.")
    parser.add_argument("--url", default="https://chatgpt.com/", help="first page opened in the capture browser")
    parser.add_argument("--output", "-o", type=Path, help="HAR output path")
    parser.add_argument("--browser", default="", help="Chrome or Edge executable")
    parser.add_argument("--user-data-dir", type=Path, default=Path("data/har-capture-profile"), help="persistent browser profile")
    parser.add_argument("--proxy-server", default="", help="Chrome proxy-server value, e.g. http://127.0.0.1:8080")
    parser.add_argument("--socks5-proxy", default="", help="authenticated SOCKS5 entry; the tool creates a local HTTP bridge for Chrome")
    parser.add_argument("--socks5-proxy-env", default="", help="read the authenticated SOCKS5 entry from this environment variable")
    parser.add_argument("--duration", type=float, default=0, help="capture seconds; 0 means wait for Ctrl+C")
    parser.add_argument("--max-body-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--no-fetch-responses", action="store_true", help="disable Fetch-domain response-body interception")
    parser.add_argument("--headless", action="store_true", help="run headless; manual entry normally uses the visible window")
    parser.add_argument("--ignore-certificate-errors", action="store_true")
    parser.add_argument("--check-proxy", action="store_true", help="validate the SOCKS5 proxy and exit without opening Chrome")
    parser.add_argument("--proxy-check-url", default="https://example.com/", help="URL used with --check-proxy")
    parser.add_argument("--proxy-check-timeout", type=float, default=15, help="proxy check timeout in seconds")
    parser.add_argument("--proxy-max-latency-ms", type=int, default=0, help="fail proxy check above this latency; 0 disables the threshold")
    parser.add_argument("--proxy-check-attempts", type=int, default=1, help="number of consecutive proxy checks")
    parser.add_argument("--lang", default="", help="Chrome UI language override; inferred from proxy region when omitted")
    parser.add_argument("--accept-lang", default="", help="Accept-Language override; inferred from proxy region when omitted")
    parser.add_argument("--timezone-id", default="", help="Chrome timezone override; inferred from proxy region when omitted")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    socks5_value = args.socks5_proxy or (os.getenv(args.socks5_proxy_env, "") if args.socks5_proxy_env else "")
    if args.check_proxy:
        if not socks5_value:
            print("PROXY_CHECK_ERROR=SOCKS5 proxy is required", file=sys.stderr)
            return 2
        attempts = max(args.proxy_check_attempts, 1)
        for attempt in range(1, attempts + 1):
            try:
                status_ok, status, latency_ms = check_socks5_proxy(
                    socks5_value,
                    args.proxy_check_url,
                    timeout=max(args.proxy_check_timeout, 1),
                )
                print(f"PROXY_CHECK_ATTEMPT={attempt} status={status} latency_ms={latency_ms}")
                if not status_ok:
                    return 2
                if args.proxy_max_latency_ms > 0 and latency_ms > args.proxy_max_latency_ms:
                    print(
                        f"PROXY_CHECK_ERROR=latency {latency_ms}ms exceeds {args.proxy_max_latency_ms}ms "
                        f"(status={status})",
                        file=sys.stderr,
                    )
                    return 2
            except (OSError, ValueError, RuntimeError, TimeoutError, URLError) as exc:
                print(f"PROXY_CHECK_ATTEMPT={attempt} error={exc}", file=sys.stderr)
                if attempt == attempts:
                    return 2
                continue
        print("PROXY_CHECK=ok")
        return 0
    if args.output is None:
        print("CAPTURE_ERROR=--output/-o is required unless --check-proxy is used", file=sys.stderr)
        return 2
    browser = _find_browser(args.browser)
    if args.proxy_server and socks5_value:
        print("CAPTURE_ERROR=use either --proxy-server or --socks5-proxy", file=sys.stderr)
        return 2
    locale_country, inferred_locale = locale_profile_for_proxy(socks5_value)
    capture_lang = args.lang or inferred_locale["lang"]
    accept_lang = args.accept_lang or inferred_locale["accept_lang"]
    timezone_id = args.timezone_id or inferred_locale["timezone"]
    bridge: Socks5HttpBridge | None = None
    if socks5_value:
        try:
            bridge = Socks5HttpBridge(socks5_value)
            args.proxy_server = bridge.proxy_server
        except (OSError, ValueError, RuntimeError) as exc:
            print(f"CAPTURE_ERROR={exc}", file=sys.stderr)
            return 2
    args.user_data_dir = args.user_data_dir.resolve()
    args.output = args.output.resolve()
    args.user_data_dir.mkdir(parents=True, exist_ok=True)
    port = _free_port()
    command = [
        browser,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={args.user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        f"--lang={capture_lang}",
        f"--accept-lang={accept_lang}",
        "about:blank",
    ]
    if args.proxy_server:
        command.insert(-1, f"--proxy-server={args.proxy_server}")
    if args.headless:
        command.insert(-1, "--headless=new")
    if args.ignore_certificate_errors:
        command.insert(-1, "--ignore-certificate-errors")
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    cdp: CDPWebSocket | None = None
    try:
        _wait_json(port, "/json/version", 30)
        pages = _wait_json(port, "/json/list", 30)
        page = next((item for item in pages if item.get("type") == "page" and item.get("webSocketDebuggerUrl")), None)
        if not page:
            raise RuntimeError("Chrome did not expose a debuggable page")
        cdp = CDPWebSocket(str(page["webSocketDebuggerUrl"]))
        recorder = HARRecorder(
            cdp,
            max_body_bytes=max(args.max_body_bytes, 0),
            stream_responses=args.no_fetch_responses,
        )
        recorder.command("Page.enable")
        recorder.command("Network.enable", network_enable_params(max(args.max_body_bytes, 0)))
        if not args.no_fetch_responses:
            recorder.command("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
        recorder.command("Emulation.setTimezoneOverride", {"timezoneId": timezone_id})
        recorder.command("Page.navigate", {"url": args.url})
        cdp.sock.settimeout(0.25)
        print(f"CAPTURE_BROWSER={browser}")
        print(f"CAPTURE_URL={args.url}")
        if socks5_value:
            print("CAPTURE_PROXY=local-http-bridge-for-authenticated-socks5")
        print(f"CAPTURE_PROXY_COUNTRY={locale_country}")
        print(f"CAPTURE_LANG={capture_lang}")
        print(f"CAPTURE_TIMEZONE={timezone_id}")
        print(f"CAPTURE_OUTPUT={args.output}")
        print("CAPTURE_ACTION=complete the manual flow, then press Ctrl+C in this terminal to save the HAR")
        deadline = time.time() + args.duration if args.duration > 0 else None
        while deadline is None or time.time() < deadline:
            try:
                message = cdp.recv_json()
            except socket.timeout:
                if deadline is not None and time.time() >= deadline:
                    break
                continue
            except KeyboardInterrupt:
                break
            recorder.handle(message)
        recorder.flush_pending()
        har = recorder.as_har(title=args.url)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"CAPTURE_SAVED={args.output}")
        print(f"CAPTURE_ENTRIES={len(har['log']['entries'])}")
        print(f"CAPTURE_SHA256={hashlib.sha256(args.output.read_bytes()).hexdigest().upper()}")
        return 0
    except KeyboardInterrupt:
        if cdp:
            recorder.flush_pending()  # type: ignore[name-defined]
            har = recorder.as_har(title=args.url)  # type: ignore[name-defined]
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"CAPTURE_SAVED={args.output}")
            return 0
        return 130
    finally:
        if cdp:
            if not args.no_fetch_responses:
                try:
                    recorder.command("Fetch.disable")  # type: ignore[name-defined]
                except Exception:
                    pass
            cdp.close()
        try:
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            process.kill()
        if bridge:
            bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
