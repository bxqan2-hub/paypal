from __future__ import annotations

"""Prepare a persistent Playwright profile, attach to CDP, capture, and analyze."""

import argparse
import datetime as dt
from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, Iterable
import urllib.request


WORKSPACE = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_DIR = WORKSPACE / "data" / "playwright-capture-profile"
DEFAULT_CAPTURE_DIR = WORKSPACE / "artifacts-local" / "playwright-captures"


@dataclass(frozen=True)
class BrowserSession:
    profile_dir: Path | None
    port: int
    browser: str
    websocket_url: str
    pages: tuple[dict[str, str], ...]

    @property
    def endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def read_devtools_port(profile_dir: Path) -> int | None:
    try:
        value = (profile_dir / "DevToolsActivePort").read_text(encoding="utf-8").splitlines()[0]
        port = int(value.strip())
    except (OSError, ValueError, IndexError):
        return None
    return port if 0 < port < 65536 else None


def _json(url: str, timeout: float) -> Any:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def probe_port(port: int, profile_dir: Path | None = None, timeout: float = 0.5) -> BrowserSession | None:
    try:
        version = _json(f"http://127.0.0.1:{port}/json/version", timeout)
        targets = _json(f"http://127.0.0.1:{port}/json/list", timeout)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(version, dict) or not version.get("webSocketDebuggerUrl"):
        return None
    pages = tuple(
        {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "url": str(item.get("url") or ""),
        }
        for item in (targets if isinstance(targets, list) else [])
        if isinstance(item, dict) and item.get("type") == "page"
    )
    return BrowserSession(
        profile_dir=profile_dir.resolve() if profile_dir else None,
        port=port,
        browser=str(version.get("Browser") or "Chromium"),
        websocket_url=str(version["webSocketDebuggerUrl"]),
        pages=pages,
    )


def candidate_profile_dirs(explicit: Path | None = None) -> list[Path]:
    local = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData/Local"))
    roaming = Path(os.getenv("APPDATA", Path.home() / "AppData/Roaming"))
    values: list[Path] = []
    if explicit:
        values.append(explicit)
    if os.getenv("PLAYWRIGHT_CAPTURE_PROFILE_DIR", "").strip():
        values.append(Path(os.environ["PLAYWRIGHT_CAPTURE_PROFILE_DIR"]))
    values.extend(
        [
            local / "Google/Chrome/User Data",
            local / "Microsoft/Edge/User Data",
            DEFAULT_PROFILE_DIR,
        ]
    )
    roxy = roaming / "RoxyBrowser/browser-cache"
    if roxy.is_dir():
        values.extend(item for item in roxy.iterdir() if item.is_dir())
    result: list[Path] = []
    seen: set[str] = set()
    for value in values:
        key = os.path.normcase(str(value.resolve()))
        if key not in seen:
            seen.add(key)
            result.append(value)
    return result


def discover_sessions(profile_dir: Path | None = None) -> list[BrowserSession]:
    sessions: list[BrowserSession] = []
    ports: set[int] = set()
    for candidate in candidate_profile_dirs(profile_dir):
        port = read_devtools_port(candidate)
        if not port or port in ports:
            continue
        session = probe_port(port, candidate)
        if session:
            ports.add(port)
            sessions.append(session)
    return sessions


def select_session(sessions: Iterable[BrowserSession], url_contains: str = "") -> BrowserSession | None:
    choices = list(sessions)
    if not choices:
        return None
    needle = url_contains.casefold().strip()

    def score(session: BrowserSession) -> tuple[int, int, int]:
        urls = [page.get("url", "").casefold() for page in session.pages]
        return (
            int(bool(needle) and any(needle in url for url in urls)),
            int(session.profile_dir == DEFAULT_PROFILE_DIR.resolve()),
            int(any(url and url != "about:blank" for url in urls)),
        )

    return max(choices, key=score)


def playwright_chromium_executable() -> Path:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        return Path(playwright.chromium.executable_path).resolve()


def launch_managed_session(profile_dir: Path, start_url: str, timeout: float) -> BrowserSession:
    profile_dir = profile_dir.resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    old_port = read_devtools_port(profile_dir)
    if old_port:
        session = probe_port(old_port, profile_dir)
        if session:
            return session
        (profile_dir / "DevToolsActivePort").unlink(missing_ok=True)
    command = [
        str(playwright_chromium_executable()),
        f"--user-data-dir={profile_dir}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    subprocess.Popen(
        command,
        cwd=WORKSPACE,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
    )
    deadline = time.monotonic() + max(timeout, 1.0)
    while time.monotonic() < deadline:
        port = read_devtools_port(profile_dir)
        if port:
            session = probe_port(port, profile_dir, timeout=1.0)
            if session:
                return session
        time.sleep(0.2)
    raise RuntimeError(f"managed browser did not publish DevToolsActivePort within {timeout:.1f}s")


def verify_connection(session: BrowserSession) -> tuple[int, int]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(session.endpoint)
        return len(browser.contexts), sum(len(context.pages) for context in browser.contexts)


def return_to_main(session: BrowserSession, return_url: str) -> str:
    """Return an attached browser to its signed-in main page without closing it."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(session.endpoint)
        if not browser.contexts:
            raise RuntimeError("connected browser has no persistent context")
        context = browser.contexts[0]
        pages = list(context.pages)
        page = next((item for item in pages if "chatgpt.com" in item.url), pages[0] if pages else context.new_page())
        page.goto(return_url, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("networkidle", timeout=5_000)
        except PlaywrightTimeoutError:
            pass
        final_url = page.url
    if probe_port(session.port, session.profile_dir, timeout=2.0) is None:
        raise RuntimeError("browser stopped after returning to the main page")
    print("CAPTURE_BROWSER_PRESERVED=1", flush=True)
    print(f"CAPTURE_RETURNED_MAIN={final_url}", flush=True)
    print("CAPTURE_NEXT_CYCLE_READY=1", flush=True)
    return final_url


def ensure_session(args: argparse.Namespace) -> BrowserSession:
    profile = args.profile_dir.resolve() if args.profile_dir else None
    if args.cdp_port:
        session = probe_port(args.cdp_port, profile, timeout=2.0)
        if session is None:
            raise RuntimeError(f"CDP endpoint 127.0.0.1:{args.cdp_port} is not reachable")
    elif profile:
        port = read_devtools_port(profile)
        session = probe_port(port, profile, timeout=2.0) if port else None
        if session is None:
            session = launch_managed_session(profile, args.start_url, args.launch_timeout)
    else:
        session = select_session(discover_sessions(), args.url_contains)
        if session is None:
            session = launch_managed_session(DEFAULT_PROFILE_DIR, args.start_url, args.launch_timeout)
    contexts, pages = verify_connection(session)
    print(f"PLAYWRIGHT_CONNECTED=127.0.0.1:{session.port}", flush=True)
    print(f"PLAYWRIGHT_BROWSER={session.browser}", flush=True)
    print(f"PLAYWRIGHT_PROFILE={session.profile_dir or '(external)'}", flush=True)
    print(f"PLAYWRIGHT_CONTEXTS={contexts}", flush=True)
    print(f"PLAYWRIGHT_PAGES={pages}", flush=True)
    for page in session.pages:
        print(f"PLAYWRIGHT_PAGE={page.get('title', '')}|{page.get('url', '')}", flush=True)
    return session


def default_output(channel: str) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return DEFAULT_CAPTURE_DIR / f"{channel}-cdp-capture-{stamp}.har"


def run_capture(args: argparse.Namespace) -> int:
    session = ensure_session(args)
    output = (args.output or default_output(args.channel)).resolve()
    command = [
        sys.executable,
        str(WORKSPACE / "tools/har_capture_browser_attach.py"),
        "--cdp-port",
        str(session.port),
        "--output",
        str(output),
    ]
    if args.duration > 0:
        command.extend(["--duration", str(args.duration)])
    capture = subprocess.run(command, cwd=WORKSPACE, check=False)
    status = capture.returncode or (0 if output.is_file() else 2)
    if status == 0:
        summary = output.with_suffix(".summary.md")
        analysis = subprocess.run(
            [sys.executable, str(WORKSPACE / "tools/har_analyze.py"), str(output), "--output", str(summary)],
            cwd=WORKSPACE,
            check=False,
        )
        status = analysis.returncode
        if status == 0:
            print(f"CAPTURE_ANALYSIS={summary}", flush=True)
        if status == 0 and args.channel == "gopay":
            channel_summary = output.with_suffix(".gopay.md")
            channel_analysis = subprocess.run(
                [
                    sys.executable,
                    str(WORKSPACE / "tools/har_cdp_gopay_summary.py"),
                    str(output),
                    "--output",
                    str(channel_summary),
                ],
                cwd=WORKSPACE,
                check=False,
            )
            status = channel_analysis.returncode
            if status == 0:
                print(f"CAPTURE_CHANNEL_ANALYSIS={channel_summary}", flush=True)
    try:
        return_to_main(session, args.return_url)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"CAPTURE_RETURN_ERROR={exc}", file=sys.stderr)
        status = status or 2
    return status


def add_connection_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile-dir", type=Path)
    parser.add_argument("--cdp-port", type=int, default=0)
    parser.add_argument("--start-url", default="https://chatgpt.com/")
    parser.add_argument("--url-contains", default="")
    parser.add_argument("--launch-timeout", type=float, default=20.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--profile-dir", type=Path)
    prepare = commands.add_parser("prepare")
    add_connection_options(prepare)
    capture = commands.add_parser("capture")
    add_connection_options(capture)
    capture.add_argument("--channel", choices=("generic", "paypal", "gopay", "gcash"), default="generic")
    capture.add_argument("--output", "-o", type=Path)
    capture.add_argument("--duration", type=float, default=0)
    capture.add_argument("--return-url", default="https://chatgpt.com/")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "list":
            sessions = discover_sessions(args.profile_dir)
            print(f"PLAYWRIGHT_BROWSERS={len(sessions)}")
            for session in sessions:
                print(f"PLAYWRIGHT_BROWSER_SESSION={session.port}|{session.profile_dir}|{session.browser}")
                for page in session.pages:
                    print(f"PLAYWRIGHT_PAGE={page.get('title', '')}|{page.get('url', '')}")
            return 0
        if args.command == "prepare":
            ensure_session(args)
            print("PLAYWRIGHT_PROFILE_READY=1")
            return 0
        return run_capture(args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"PLAYWRIGHT_CAPTURE_ERROR={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
