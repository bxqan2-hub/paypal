from __future__ import annotations

"""Capture all Chrome CDP targets from an already running browser.

Unlike the page-only attach helper, this recorder connects to the browser
WebSocket from ``/json/version`` and enables ``Target.setAutoAttach`` with
``flatten=true``. Stripe cross-origin iframes/OOPIFs therefore receive their
own Network/Fetch recorder and their API bodies are included in the saved HAR.
The browser is never navigated or reloaded by this tool.
"""

import argparse
from collections import defaultdict, deque
import hashlib
import json
import socket
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
            self.recorder.command("Runtime.enable")
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
        except Exception:
            pass
        if self.fetch_responses:
            try:
                self.recorder.command("Fetch.disable")
            except Exception:
                pass
        return self.recorder.entries


def _json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _browser_websocket(port: int) -> str:
    version = _json(f"http://127.0.0.1:{port}/json/version")
    if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
        raise RuntimeError("CDP /json/version did not return browser WebSocket URL")
    return str(version["webSocketDebuggerUrl"])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--output", "-o", type=Path, required=True)
    parser.add_argument("--max-body-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--no-fetch-responses", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    browser = BrowserConnection(_browser_websocket(args.cdp_port))
    recorders: dict[str, TargetRecorder] = {}
    all_entries: list[dict[str, Any]] = []
    page_url = "https://chatgpt.com/"
    try:
        browser.command("Target.setDiscoverTargets", {"discover": True})
        browser.command(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        browser.sock.settimeout(0.25)
        print(f"CAPTURE_CDP=127.0.0.1:{args.cdp_port}", flush=True)
        print("CAPTURE_TARGET_MODE=browser-auto-attach-flatten", flush=True)
        print(f"CAPTURE_OUTPUT={args.output.resolve()}", flush=True)
        print("CAPTURE_READY=1", flush=True)
        print("CAPTURE_ACTION=perform the complete GoPay flow; send the stop instruction when finished", flush=True)
        while True:
            try:
                message = browser.recv_global()
            except socket.timeout:
                continue
            method = str(message.get("method") or "")
            params = message.get("params") if isinstance(message.get("params"), dict) else {}
            if method == "Target.attachedToTarget":
                session_id = str(params.get("sessionId") or "")
                target_info = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
                target_type = str(target_info.get("type") or "")
                if not session_id or target_type not in TARGET_TYPES:
                    continue
                if session_id in recorders:
                    continue
                target = TargetRecorder(
                    browser,
                    session_id,
                    target_info,
                    max_body_bytes=max(args.max_body_bytes, 0),
                    fetch_responses=not args.no_fetch_responses,
                )
                try:
                    target.enable()
                except Exception:
                    continue
                recorders[session_id] = target
                url = str(target_info.get("url") or "")
                if target_type == "page" and "chatgpt.com" in url:
                    page_url = url
                print(
                    f"CAPTURE_TARGET_ATTACHED={target_type}:{urlsplit(url).netloc}{urlsplit(url).path}",
                    flush=True,
                )
            elif method == "Target.detachedFromTarget":
                session_id = str(params.get("sessionId") or "")
                target = recorders.pop(session_id, None)
                if target is not None:
                    all_entries.extend(target.close())
            elif message.get("sessionId"):
                session_id = str(message.get("sessionId"))
                target = recorders.get(session_id)
                if target is not None:
                    target.handle(message)
    except KeyboardInterrupt:
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
                },
            }
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"CAPTURE_SAVED={args.output.resolve()}", flush=True)
        print(f"CAPTURE_ENTRIES={len(all_entries)}", flush=True)
        print(f"CAPTURE_SHA256={hashlib.sha256(args.output.read_bytes()).hexdigest().upper()}", flush=True)
        browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
