from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import hashlib
import json
import os
import socket
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import urlopen

try:
    from .har_capture import CDPWebSocket, HARRecorder, audit_har_completeness, network_enable_params
except ImportError:  # direct ``python tools/roxy_har_capture.py`` invocation
    from har_capture import CDPWebSocket, HARRecorder, audit_har_completeness, network_enable_params


@dataclass(frozen=True)
class RoxyTarget:
    profile_id: str
    port: int
    page_id: str
    title: str
    url: str
    websocket_url: str


def default_roxy_cache() -> Path:
    appdata = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming"))
    return appdata / "RoxyBrowser" / "browser-cache"


def read_devtools_port(profile_dir: Path) -> int | None:
    path = profile_dir / "DevToolsActivePort"
    try:
        first_line = path.read_text(encoding="utf-8").splitlines()[0].strip()
        port = int(first_line)
        return port if 0 < port < 65536 else None
    except (OSError, ValueError, IndexError):
        return None


def _discover_profile(profile_dir: Path, timeout: float) -> list[RoxyTarget]:
    port = read_devtools_port(profile_dir)
    if not port:
        return []
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=timeout) as response:
            pages = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(pages, list):
        return []
    return [
        RoxyTarget(
            profile_id=profile_dir.name,
            port=port,
            page_id=str(page.get("id") or ""),
            title=str(page.get("title") or "(untitled)"),
            url=str(page.get("url") or ""),
            websocket_url=str(page["webSocketDebuggerUrl"]),
        )
        for page in pages
        if isinstance(page, dict) and page.get("type") == "page" and page.get("webSocketDebuggerUrl")
    ]


def discover_roxy_targets(cache_root: Path, *, timeout: float = 0.3) -> list[RoxyTarget]:
    if not cache_root.is_dir():
        return []
    profiles = [item for item in cache_root.iterdir() if item.is_dir() and (item / "DevToolsActivePort").is_file()]
    targets: list[RoxyTarget] = []
    with ThreadPoolExecutor(max_workers=16, thread_name_prefix="roxy-discovery") as executor:
        futures = [executor.submit(_discover_profile, profile, timeout) for profile in profiles]
        for future in as_completed(futures):
            targets.extend(future.result())
    return sorted(targets, key=lambda item: (item.port, item.page_id))


def select_target(targets: list[RoxyTarget], *, port: int = 0, page_id: str = "") -> RoxyTarget:
    filtered = [item for item in targets if (not port or item.port == port) and (not page_id or item.page_id == page_id)]
    if not filtered:
        raise ValueError("no matching open RoxyBrowser page was found")
    if port or page_id or len(filtered) == 1:
        return filtered[0]
    print("ROXY_OPEN_PAGES:")
    for index, item in enumerate(filtered, 1):
        print(f"  [{index}] port={item.port} title={item.title} url={item.url}")
    while True:
        value = input(f"Select page [1-{len(filtered)}]: ").strip()
        try:
            selected = int(value)
        except ValueError:
            selected = 0
        if 1 <= selected <= len(filtered):
            return filtered[selected - 1]
        print("Invalid selection.")


def default_output() -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path("data/captures") / f"roxy-{stamp}.har"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture a selected existing RoxyBrowser page through CDP.")
    parser.add_argument("--cache-root", type=Path, default=default_roxy_cache())
    parser.add_argument("--list", action="store_true", help="list open Roxy pages and exit")
    parser.add_argument("--port", type=int, default=0, help="select a Roxy CDP port")
    parser.add_argument("--page-id", default="", help="select a CDP page id")
    parser.add_argument("--output", "-o", type=Path, default=None)
    parser.add_argument("--stop-file", type=Path, default=Path("data/roxy-capture.stop"))
    parser.add_argument("--start-immediately", action="store_true", help="do not wait for Enter before enabling Network")
    parser.add_argument("--duration", type=float, default=0, help="automatic capture seconds; 0 waits for Enter or stop BAT")
    parser.add_argument("--max-body-bytes", type=int, default=8 * 1024 * 1024)
    parser.add_argument("--response-body-retries", type=int, default=3)
    parser.add_argument("--no-fetch-responses", action="store_true", help="disable Fetch-domain response-body interception")
    parser.add_argument("--no-stream-responses", action="store_true", help="disable CDP streamed response-body fallback")
    parser.add_argument("--require-complete", action="store_true", help="exit 3 unless the complete GCash flow and all expected bodies were saved")
    return parser


def _wait_for_enter(stop_event: threading.Event) -> None:
    try:
        input()
    except (EOFError, KeyboardInterrupt):
        pass
    stop_event.set()


def capture_target(target: RoxyTarget, args: argparse.Namespace) -> tuple[Path, dict[str, Any]]:
    output = (args.output or default_output()).resolve()
    stop_file = args.stop_file.resolve()
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.unlink(missing_ok=True)
    if not args.start_immediately:
        print("ROXY_CAPTURE_READY=navigate the selected Roxy page to the exact start node")
        input("Press Enter to START capture: ")
    cdp = CDPWebSocket(target.websocket_url)
    recorder = HARRecorder(
        cdp,
        max_body_bytes=max(args.max_body_bytes, 0),
        response_body_retries=max(args.response_body_retries, 1),
        stream_responses=not args.no_stream_responses,
    )
    started_at = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    stop_event = threading.Event()
    fetch_enabled = False
    try:
        recorder.command("Network.enable", network_enable_params(max(args.max_body_bytes, 0)))
        if not args.no_fetch_responses:
            # RoxyChrome can omit the Fetch domain entirely; stream-based body
            # recovery keeps the capture complete even when it is unavailable.
            try:
                recorder.command("Fetch.enable", {"patterns": [{"urlPattern": "*", "requestStage": "Response"}]})
                fetch_enabled = True
            except RuntimeError as exc:
                print(f"ROXY_CAPTURE_FETCH_UNAVAILABLE={exc}", file=sys.stderr)
        cdp.sock.settimeout(0.25)
        print(f"ROXY_CAPTURE_STARTED=port={target.port} page={target.page_id}")
        print("ROXY_CAPTURE_STOP=press Enter here or double-click ROXY_CAPTURE_STOP.bat")
        if args.duration <= 0:
            threading.Thread(target=_wait_for_enter, args=(stop_event,), name="roxy-capture-stop", daemon=True).start()
        deadline = time.monotonic() + args.duration if args.duration > 0 else None
        while not stop_event.is_set():
            if stop_file.exists() or (deadline is not None and time.monotonic() >= deadline):
                break
            try:
                recorder.handle(cdp.recv_json())
            except socket.timeout:
                continue
        recorder.flush_pending()
        har = recorder.as_har(title=target.title)
        audit_har_completeness(har)
        har["log"]["_capture"].update(
            {
                "mode": "attach-existing-roxy",
                "profileId": target.profile_id,
                "cdpPort": target.port,
                "pageId": target.page_id,
                "startMarker": started_at,
                "stopMarker": dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            }
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
        return output, har
    finally:
        stop_file.unlink(missing_ok=True)
        if fetch_enabled and not args.no_fetch_responses:
            try:
                recorder.command("Fetch.disable")
            except Exception:
                pass
        cdp.close()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    targets = discover_roxy_targets(args.cache_root.resolve())
    if args.list:
        if not targets:
            print("ROXY_OPEN_PAGES=0")
            return 2
        for index, item in enumerate(targets, 1):
            print(f"ROXY_PAGE={index} port={item.port} page={item.page_id} title={item.title} url={item.url}")
        return 0
    try:
        target = select_target(targets, port=args.port, page_id=args.page_id)
        output, har = capture_target(target, args)
        print(f"ROXY_CAPTURE_SAVED={output}")
        print(f"ROXY_CAPTURE_ENTRIES={len(har['log']['entries'])}")
        print(f"ROXY_CAPTURE_SHA256={hashlib.sha256(output.read_bytes()).hexdigest().upper()}")
        audit = har["log"]["_capture"].get("completenessAudit", {})
        print(f"ROXY_CAPTURE_CHANNEL={str(audit.get('channel') or 'unknown').lower()}")
        print(f"ROXY_CAPTURE_COMPLETE={str(bool(audit.get('complete'))).lower()}")
        print(f"ROXY_CAPTURE_CRITICAL_COMPLETE={str(bool(audit.get('criticalComplete'))).lower()}")
        redirect = audit.get("gopayRedirect") if isinstance(audit.get("gopayRedirect"), dict) else {}
        print(f"ROXY_CAPTURE_GOPAY_REDIRECT_FOUND={str(bool(redirect.get('found'))).lower()}")
        if redirect.get("sha256"):
            print(f"ROXY_CAPTURE_GOPAY_REDIRECT_SHA256={str(redirect['sha256'])}")
        if audit.get("issues"):
            print("ROXY_CAPTURE_ISSUES=" + ",".join(str(item) for item in audit["issues"]))
        if args.require_complete and not audit.get("complete"):
            return 3
        return 0
    except (EOFError, OSError, RuntimeError, ValueError) as exc:
        print(f"ROXY_CAPTURE_ERROR={exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("ROXY_CAPTURE_INTERRUPTED=before capture was saved", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
