from __future__ import annotations

"""Attach the HAR recorder to an already running Chrome CDP endpoint.

The recorder never navigates or reloads the selected page. It waits for the
operator to perform the manual flow and saves a HAR when interrupted with
Ctrl+C.
"""

import argparse
import hashlib
import json
import socket
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

try:
    from .har_capture import CDPWebSocket, HARRecorder, network_enable_params
except ImportError:  # direct ``python tools/har_capture_attach.py`` invocation
    from har_capture import CDPWebSocket, HARRecorder, network_enable_params


def _json(url: str) -> object:
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.loads(response.read().decode("utf-8"))


def _select_page(port: int, contains: str) -> dict[str, object]:
    pages = _json(f"http://127.0.0.1:{port}/json/list")
    if not isinstance(pages, list):
        raise RuntimeError("CDP /json/list did not return a page list")
    needle = str(contains or "").lower()
    for page in pages:
        if not isinstance(page, dict) or page.get("type") != "page":
            continue
        url = str(page.get("url") or "")
        if needle in url.lower() and page.get("webSocketDebuggerUrl"):
            return page
    raise RuntimeError(f"CDP page containing {contains!r} was not found")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cdp-port", type=int, required=True)
    parser.add_argument("--output", "-o", type=Path, required=True)
    parser.add_argument("--page-contains", default="chatgpt.com")
    parser.add_argument("--max-body-bytes", type=int, default=16 * 1024 * 1024)
    parser.add_argument("--no-fetch-responses", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    page = _select_page(args.cdp_port, args.page_contains)
    websocket = str(page["webSocketDebuggerUrl"])
    cdp = CDPWebSocket(websocket, timeout=5)
    recorder = HARRecorder(
        cdp,
        max_body_bytes=max(args.max_body_bytes, 0),
        stream_responses=not args.no_fetch_responses,
    )
    try:
        recorder.command("Page.enable")
        recorder.command("Runtime.enable")
        recorder.command("Network.enable", network_enable_params(max(args.max_body_bytes, 0)))
        if not args.no_fetch_responses:
            recorder.command(
                "Fetch.enable",
                {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]},
            )
        cdp.sock.settimeout(0.25)
        page_url = str(page.get("url") or "")
        print(f"CAPTURE_CDP=127.0.0.1:{args.cdp_port}", flush=True)
        print(f"CAPTURE_PAGE={urlsplit(page_url).netloc}{urlsplit(page_url).path}", flush=True)
        print(f"CAPTURE_OUTPUT={args.output.resolve()}", flush=True)
        print("CAPTURE_READY=1", flush=True)
        print("CAPTURE_ACTION=perform the complete GoPay flow; send the stop instruction when finished", flush=True)
        while True:
            try:
                recorder.handle(cdp.recv_json())
            except socket.timeout:
                continue
    except KeyboardInterrupt:
        recorder.flush_pending()
        har = recorder.as_har(title=page_url or "https://chatgpt.com/")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
        digest = hashlib.sha256(args.output.read_bytes()).hexdigest().upper()
        print(f"CAPTURE_SAVED={args.output.resolve()}", flush=True)
        print(f"CAPTURE_ENTRIES={len(har['log']['entries'])}", flush=True)
        print(f"CAPTURE_SHA256={digest}", flush=True)
        return 0
    finally:
        try:
            if not args.no_fetch_responses:
                recorder.command("Fetch.disable")
        except Exception:
            pass
        cdp.close()


if __name__ == "__main__":
    raise SystemExit(main())
