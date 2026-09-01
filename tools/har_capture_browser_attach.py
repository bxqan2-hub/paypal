from __future__ import annotations

"""Capture all Chrome CDP targets from an already running browser.

Unlike the page-only attach helper, this recorder connects to the browser
WebSocket from ``/json/version`` and enables ``Target.setAutoAttach`` with
``flatten=true``. Stripe cross-origin iframes/OOPIFs therefore receive their
own Network/Fetch recorder and their API bodies are included in the saved HAR.
The browser is never navigated or reloaded by this tool.
"""

import argparse
from collections import Counter, defaultdict, deque
import hashlib
import json
import os
import socket
import signal
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    from .har_capture import CDPWebSocket, HARRecorder, network_enable_params
except ImportError:  # direct ``python tools/har_capture_browser_attach.py``
    from har_capture import CDPWebSocket, HARRecorder, network_enable_params


TARGET_TYPES = {"page", "iframe", "worker", "service_worker", "shared_worker"}


class BrowserConnection:
    """Route browser-level and flattened target-level CDP messages."""

    def __init__(self, websocket_url: str, *, timeout: float = 5.0) -> None:
        self.cdp = CDPWebSocket(websocket_url, timeout=timeout)
        self._queues: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self._global: deque[dict[str, Any]] = deque()
        self.next_id = 0

    @property
    def sock(self) -> Any:
        return self.cdp.sock

    def close(self) -> None:
        self.cdp.close()

    def _route(self, message: dict[str, Any]) -> None:
        session_id = str(message.get("sessionId") or "")
        if session_id:
            self._queues[session_id].append(message)
        else:
            self._global.append(message)

    def _read(self) -> None:
        self._route(self.cdp.recv_json())

    def command(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.next_id += 1
        command_id = self.next_id
        self.cdp.send_json({"id": command_id, "method": method, "params": params or {}})
        while True:
            message = None
            for index, queued in enumerate(self._global):
                if queued.get("id") == command_id:
                    message = queued
                    del self._global[index]
                    break
            if message is None:
                self._read()
                continue
            if "error" in message:
                raise RuntimeError(f"CDP {method} failed: {message['error']}")
            return message.get("result", {})

    def recv_global(self) -> dict[str, Any]:
        while not self._global:
            self._read()
        return self._global.popleft()

    def recv_session(self, session_id: str) -> dict[str, Any]:
        queue = self._queues[session_id]
        while not queue:
            self._read()
        return queue.popleft()

    def recv_any(self) -> tuple[str, dict[str, Any]]:
        """Return the next browser event with its flattened session id."""
        while True:
            if self._global:
                return "", self._global.popleft()
            for session_id, queue in self._queues.items():
                if queue:
                    return session_id, queue.popleft()
            self._read()


class ScopedCDP:
    """CDP adapter with the interface expected by ``HARRecorder``."""

    def __init__(self, browser: BrowserConnection, session_id: str) -> None:
        self.browser = browser
        self.session_id = session_id

    @property
    def next_id(self) -> int:
        return self.browser.next_id

    @next_id.setter
    def next_id(self, value: int) -> None:
        self.browser.next_id = value

    @property
    def sock(self) -> Any:
        return self.browser.sock

    def send_json(self, payload: dict[str, Any]) -> None:
        message = dict(payload)
        message["sessionId"] = self.session_id
        self.browser.cdp.send_json(message)

    def recv_json(self) -> dict[str, Any]:
        return self.browser.recv_session(self.session_id)


class TargetRecorder:
    def __init__(
        self,
        browser: BrowserConnection,
        session_id: str,
        target_info: dict[str, Any],
        *,
        max_body_bytes: int,
        fetch_responses: bool,
    ) -> None:
        self.browser = browser
        self.session_id = session_id
        self.target_info = target_info
        self.cdp = ScopedCDP(browser, session_id)
        self.recorder = HARRecorder(
            self.cdp,
            max_body_bytes=max_body_bytes,
            stream_responses=not fetch_responses,
        )
        self.fetch_responses = fetch_responses

    def enable(self) -> None:
        self.recorder.command("Network.enable", network_enable_params(self.recorder.max_body_bytes))
        try:
            self.recorder.command("Page.enable")
        except Exception:
            pass
        try:
            self.recorder.command("Runtime.enable")
        except Exception:
            pass
        # A page can create nested OOPIF/worker targets after initial attach.
        # Ask the target itself to discover and auto-attach children as a
        # second line of defense in addition to the browser-level setting.
        try:
            self.recorder.command("Target.setDiscoverTargets", {"discover": True})
            self.recorder.command(
                "Target.setAutoAttach",
                {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
            )
        except Exception:
            pass
        if self.fetch_responses:
            try:
                self.recorder.command(
                    "Fetch.enable",
                    {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]},
                )
            except Exception:
                self.fetch_responses = False

    def handle(self, message: dict[str, Any]) -> None:
        self.recorder.handle(message)

    def close(self) -> list[dict[str, Any]]:
        try:
            self.recorder.flush_pending()
        except BaseException:
            pass
        if self.fetch_responses:
            try:
                self.recorder.command("Fetch.disable")
            except BaseException:
                pass
        target_type = str(self.target_info.get("type") or "")
        target_url = str(self.target_info.get("url") or "")
        target_id = str(self.target_info.get("targetId") or "")
        for entry in self.recorder.entries:
            entry["_cdp_target"] = {
                "targetId": target_id,
                "sessionId": self.session_id,
                "type": target_type,
                "url": target_url,
            }
        return self.recorder.entries


def _json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _browser_websocket(port: int) -> str:
    version = _json(f"http://127.0.0.1:{port}/json/version")
    if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
        raise RuntimeError("CDP /json/version did not return browser WebSocket URL")
    return str(version["webSocketDebuggerUrl"])


def _capture_completeness_audit(har: dict[str, Any]) -> dict[str, Any]:
    """Check the GoPay checkpoints without depending on a mutable helper module.

    The browser recorder must remain usable with older ``har_capture.py``
    deployments, so the save-time audit is intentionally self-contained. It
    distinguishes a bodyless 204 snapshot from a response that should contain
    JSON, and reports API Stripe absence rather than fabricating it.
    """
    entries = har.get("log", {}).get("entries", []) if isinstance(har.get("log"), dict) else []
    entries = [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []

    def request(entry: dict[str, Any]) -> dict[str, Any]:
        value = entry.get("request")
        return value if isinstance(value, dict) else {}

    def response(entry: dict[str, Any]) -> dict[str, Any]:
        value = entry.get("response")
        return value if isinstance(value, dict) else {}

    def body(entry: dict[str, Any], side: str) -> str:
        if side == "request":
            value = request(entry).get("postData")
            return str(value.get("text") or "") if isinstance(value, dict) else str(value or "")
        content = response(entry).get("content")
        return str(content.get("text") or "") if isinstance(content, dict) else ""

    def matches(entry: dict[str, Any], host: str, path: str, method: str) -> bool:
        parsed = urlsplit(str(request(entry).get("url") or ""))
        return (
            parsed.netloc.lower() == host
            and parsed.path == path
            and str(request(entry).get("method") or "").upper() == method
        )

    def stripe_payment_page(entry: dict[str, Any], suffix: str, method: str) -> bool:
        parsed = urlsplit(str(request(entry).get("url") or ""))
        return (
            parsed.netloc.lower() == "api.stripe.com"
            and str(request(entry).get("method") or "").upper() == method
            and parsed.path.startswith("/v1/payment_pages/")
            and parsed.path.endswith(suffix)
        )

    def stripe_redirect(entry: dict[str, Any]) -> bool:
        candidates = [
            str(request(entry).get("url") or ""),
            str(response(entry).get("redirectURL") or ""),
        ]
        return any(
            (parsed := urlsplit(candidate)).netloc.lower() == "pm-redirects.stripe.com"
            and parsed.path.startswith("/authorize/")
            for candidate in candidates
        )

    specs: list[tuple[str, Any, bool]] = [
        (
            "gopay_checkout_create",
            lambda e: matches(e, "chatgpt.com", "/backend-api/payments/checkout", "POST"),
            True,
        ),
        (
            "gopay_checkout_taxes",
            lambda e: matches(e, "chatgpt.com", "/backend-api/payments/checkout/taxes", "POST"),
            True,
        ),
        (
            "gopay_checkout_snapshot",
            lambda e: matches(e, "chatgpt.com", "/backend-api/payments/checkout/snapshot", "POST"),
            True,
        ),
        (
            "gopay_approve",
            lambda e: matches(e, "chatgpt.com", "/backend-api/payments/checkout/approve", "POST"),
            True,
        ),
        ("gopay_stripe_init", lambda e: stripe_payment_page(e, "/init", "POST"), True),
        (
            "gopay_stripe_elements",
            lambda e: matches(e, "api.stripe.com", "/v1/elements/sessions", "GET"),
            False,
        ),
        ("gopay_stripe_confirm", lambda e: stripe_payment_page(e, "/confirm", "POST"), True),
        ("gopay_redirect", stripe_redirect, False),
    ]
    critical: list[dict[str, Any]] = []
    issues: list[str] = []
    for name, predicate, needs_request_body in specs:
        found = [entry for entry in entries if predicate(entry)]
        request_body = any(bool(body(entry, "request")) for entry in found)
        response_body = any(bool(body(entry, "response")) for entry in found)
        status_all_bodyless = bool(found) and all(
            int(response(entry).get("status", 0) or 0) in {101, 204, 205, 304}
            for entry in found
        )
        critical.append(
            {"name": name, "count": len(found), "requestBody": request_body, "responseBody": response_body}
        )
        if not found:
            issues.append(f"{name}:entry_missing")
        elif needs_request_body and not request_body:
            issues.append(f"{name}:request_body_missing")
        elif name != "gopay_redirect" and not response_body and not status_all_bodyless:
            issues.append(f"{name}:response_body_missing")
    sentinel_count = sum(
        1
        for entry in entries
        if "/backend-api/sentinel/req" in str(request(entry).get("url") or "")
    )
    channel_markers = {"gopay_approve", "gopay_stripe_init", "gopay_redirect"}
    channel = "gopay" if any(
        predicate(entry)
        for entry in entries
        for name, predicate, _ in specs
        if name in channel_markers
    ) else "unknown"
    return {
        "channel": channel,
        "complete": channel == "gopay" and not issues,
        "criticalComplete": channel == "gopay" and not issues,
        "entryCount": len(entries),
        "critical": critical,
        "sentinelReqEntries": sentinel_count,
        "issues": issues if channel == "gopay" else ["unknown:channel_unidentified"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--output", "-o", type=Path, required=True)
    parser.add_argument("--max-body-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument(
        "--fetch-responses",
        action="store_true",
        help="pause responses to fetch bodies explicitly; default uses non-blocking Network streaming",
    )
    parser.add_argument("--no-fetch-responses", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--heartbeat-seconds", type=float, default=5.0)
    parser.add_argument("--stop-file", type=Path)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    stop_file = args.stop_file.resolve() if args.stop_file else None
    if stop_file is not None:
        stop_file.unlink(missing_ok=True)
        def stop_watchdog() -> None:
            while not stop_file.exists():
                time.sleep(0.2)
            try:
                os.kill(os.getpid(), signal.SIGINT)
            except OSError:
                pass
        threading.Thread(target=stop_watchdog, name="capture-stop-watchdog", daemon=True).start()
    browser = BrowserConnection(_browser_websocket(args.cdp_port))
    recorders: dict[str, TargetRecorder] = {}
    target_sessions: dict[str, str] = {}
    all_entries: list[dict[str, Any]] = []
    target_types_seen: set[str] = set()
    target_ids_seen: set[str] = set()
    target_count_seen = 0
    page_url = "https://chatgpt.com/"

    def attach_target(session_id: str, target_info: dict[str, Any]) -> None:
        nonlocal target_count_seen
        target_type = str(target_info.get("type") or "")
        target_id = str(target_info.get("targetId") or "")
        if not session_id or target_type not in TARGET_TYPES:
            return
        if session_id in recorders or (target_id and target_id in target_sessions):
            return
        target = TargetRecorder(
            browser,
            session_id,
            target_info,
            max_body_bytes=max(args.max_body_bytes, 0),
            fetch_responses=bool(args.fetch_responses and not args.no_fetch_responses),
        )
        try:
            target.enable()
        except Exception:
            return
        recorders[session_id] = target
        if target_id:
            target_sessions[target_id] = session_id
        target_types_seen.add(target_type)
        target_ids_seen.add(target_id or session_id)
        target_count_seen = len(target_ids_seen)
        url = str(target_info.get("url") or "")
        print(
            f"CAPTURE_TARGET_ATTACHED={target_type}:{urlsplit(url).netloc}{urlsplit(url).path}",
            flush=True,
        )

    def manually_attach_target(target_info: dict[str, Any]) -> None:
        target_id = str(target_info.get("targetId") or "")
        if not target_id or target_id in target_sessions:
            return
        try:
            result = browser.command(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
        except Exception:
            return
        session_id = str(result.get("sessionId") or "") if isinstance(result, dict) else ""
        if session_id:
            attach_target(session_id, target_info)

    try:
        browser.command("Target.setDiscoverTargets", {"discover": True})
        browser.command(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        try:
            existing = browser.command("Target.getTargets").get("targetInfos", [])
            for target_info in existing if isinstance(existing, list) else []:
                if isinstance(target_info, dict) and str(target_info.get("type") or "") in TARGET_TYPES:
                    manually_attach_target(target_info)
        except Exception:
            pass
        browser.sock.settimeout(0.25)
        print(f"CAPTURE_CDP=127.0.0.1:{args.cdp_port}", flush=True)
        print("CAPTURE_TARGET_MODE=browser-auto-attach-flatten", flush=True)
        print(f"CAPTURE_OUTPUT={args.output.resolve()}", flush=True)
        print("CAPTURE_READY=1", flush=True)
        print("CAPTURE_ACTION=perform the complete GoPay flow; send the stop instruction when finished", flush=True)
        heartbeat_interval = max(float(args.heartbeat_seconds), 0.5)
        last_heartbeat = time.monotonic()
        while True:
            if stop_file is not None and stop_file.exists():
                break
            try:
                session_id, message = browser.recv_any()
            except socket.timeout:
                if time.monotonic() - last_heartbeat >= heartbeat_interval:
                    print(
                        f"CAPTURE_HEARTBEAT=targets:{len(recorders)} entries:{sum(len(item.recorder.entries) for item in recorders.values()) + len(all_entries)}",
                        flush=True,
                    )
                    last_heartbeat = time.monotonic()
                continue
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if session_id:
                target = recorders.get(session_id)
                if target is not None:
                    target.handle(message)
                if time.monotonic() - last_heartbeat >= heartbeat_interval:
                    print(
                        f"CAPTURE_HEARTBEAT=targets:{len(recorders)} entries:{sum(len(item.recorder.entries) for item in recorders.values()) + len(all_entries)}",
                        flush=True,
                    )
                    last_heartbeat = time.monotonic()
                continue
            if method == "Target.attachedToTarget":
                session_id = str(params.get("sessionId") or "")
                target_info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
                attach_target(session_id, target_info)
                url = str(target_info.get("url") or "")
                if str(target_info.get("type") or "") == "page" and "chatgpt.com" in url:
                    page_url = url
            elif method == "Target.targetCreated":
                target_info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
                manually_attach_target(target_info)
            elif method == "Target.targetInfoChanged":
                target_info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
                target_id = str(target_info.get("targetId") or "")
                session_for_target = target_sessions.get(target_id)
                if session_for_target and session_for_target in recorders:
                    recorders[session_for_target].target_info = target_info
            elif method == "Target.detachedFromTarget":
                session_id = str(params.get("sessionId") or "")
                target = recorders.pop(session_id, None)
                if target is not None:
                    all_entries.extend(target.close())
                for target_id, mapped_session in list(target_sessions.items()):
                    if mapped_session == session_id:
                        target_sessions.pop(target_id, None)
    except (KeyboardInterrupt, EOFError, ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
        pass
    finally:
        for session_id, target in list(recorders.items()):
            all_entries.extend(target.close())
            recorders.pop(session_id, None)
        all_entries.sort(key=lambda entry: str(entry.get("startedDateTime") or ""))
        har = {
            "log": {
                "version": "1.2",
                "creator": {"name": "opll-har-capture-browser-attach", "version": "1.0"},
                "pages": [],
                "entries": all_entries,
                "_capture": {
                    "title": page_url,
                    "entryCount": len(all_entries),
                    "targetMode": "browser-auto-attach-flatten",
                    "targetCount": target_count_seen,
                    "targetTypes": sorted(target_types_seen),
                    "fetchResponses": bool(args.fetch_responses and not args.no_fetch_responses),
                },
            }
        }
        try:
            completeness = _capture_completeness_audit(har)
        except Exception as exc:
            completeness = {"complete": False, "criticalComplete": False, "issues": [f"audit_error:{exc}"]}
        har["log"]["_capture"]["completenessAudit"] = completeness
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"CAPTURE_SAVED={args.output.resolve()}", flush=True)
        print(f"CAPTURE_ENTRIES={len(all_entries)}", flush=True)
        print(f"CAPTURE_SHA256={hashlib.sha256(args.output.read_bytes()).hexdigest().upper()}", flush=True)
        host_counts = Counter(
            urlsplit(str(entry.get("request", {}).get("url") or "")).netloc.lower()
            for entry in all_entries
            if isinstance(entry, dict) and isinstance(entry.get("request"), dict)
        )
        print(f"CAPTURE_HOST_COUNTS={json.dumps(dict(sorted(host_counts.items())), ensure_ascii=False)}", flush=True)
        print(f"CAPTURE_TARGET_TYPES={json.dumps(sorted(target_types_seen), ensure_ascii=False)}", flush=True)
        print(f"CAPTURE_API_STRIPE_ENTRIES={host_counts.get('api.stripe.com', 0)}", flush=True)
        print(f"CAPTURE_COMPLETENESS={'complete' if completeness.get('complete') else 'partial'}", flush=True)
        print(
            f"CAPTURE_MISSING={json.dumps(completeness.get('issues', []), ensure_ascii=False)}",
            flush=True,
        )
        if stop_file is not None:
            stop_file.unlink(missing_ok=True)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
