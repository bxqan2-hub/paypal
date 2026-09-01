from __future__ import annotations

import argparse
import getpass
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import urlopen

try:
    from .har_capture import CDPWebSocket, _find_browser, audit_har_completeness
    from .har_cdp_gopay_summary import render as render_gopay_summary
    from .har_cdp_gopay_summary import summarize as summarize_gopay
    from .har_utils import analyze_har, load_har, markdown_report
except ImportError:
    from har_capture import CDPWebSocket, _find_browser, audit_har_completeness
    from har_cdp_gopay_summary import render as render_gopay_summary
    from har_cdp_gopay_summary import summarize as summarize_gopay
    from har_utils import analyze_har, load_har, markdown_report


MITMPROXY_BIN_DIRS = (
    Path(r"C:\Program Files\mitmproxy\bin"),
    Path(r"C:\Program Files (x86)\mitmproxy\bin"),
)
CHANNELS = ("paypal", "gopay", "gcash")
ROXY_IP_CHECK_IGNORE_HOSTS = r"^ipcheck\.roxybrowser\.(?:com|co):443$"


def find_mitm_binary(name: str, explicit: str = "") -> Path:
    candidates = [Path(explicit)] if explicit else []
    path_value = os.getenv("PATH", "")
    candidates.extend(Path(folder) / f"{name}.exe" for folder in path_value.split(os.pathsep) if folder)
    candidates.extend(folder / f"{name}.exe" for folder in MITMPROXY_BIN_DIRS)
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise FileNotFoundError(
        f"{name} was not found; install it with: winget install --id mitmproxy.mitmproxy -e"
    )


def read_devtools_port(profile: Path) -> int | None:
    active_port = profile / "DevToolsActivePort"
    if not active_port.is_file():
        return None
    try:
        port = int(active_port.read_text(encoding="utf-8").splitlines()[0].strip())
    except (OSError, ValueError, IndexError):
        return None
    return port if port > 0 else None


def endpoint_ready(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as response:
            return response.status == 200
    except OSError:
        return False


def wait_for_port(
    port: int,
    process: subprocess.Popen[bytes],
    timeout: float = 20,
    label: str = "mitmproxy",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"{label} exited before becoming ready (status {process.returncode})")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.2)
    raise TimeoutError(f"{label} did not listen on 127.0.0.1:{port}")


def require_free_port(port: int, label: str) -> None:
    if not 1 <= port <= 65535:
        raise ValueError(f"{label} port must be between 1 and 65535")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", port))
        except OSError as exc:
            raise RuntimeError(f"{label} port is already in use: 127.0.0.1:{port}") from exc


def reserve_free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def wait_for_cdp(profile: Path, timeout: float = 30) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        port = read_devtools_port(profile)
        if port and endpoint_ready(port):
            return port
        time.sleep(0.2)
    raise TimeoutError(f"Chrome DevTools endpoint did not start for {profile}")


def first_page(port: int) -> dict[str, object]:
    with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=5) as response:
        targets = json.loads(response.read().decode("utf-8"))
    page = next(
        (
            item
            for item in targets
            if item.get("type") == "page" and item.get("webSocketDebuggerUrl")
        ),
        None,
    )
    if not isinstance(page, dict):
        raise RuntimeError("managed Chrome did not expose a debuggable page")
    return page


def navigate_page(port: int, url: str) -> str:
    page = first_page(port)
    cdp = CDPWebSocket(str(page["webSocketDebuggerUrl"]))
    try:
        cdp.next_id += 1
        command_id = cdp.next_id
        cdp.send_json({"id": command_id, "method": "Page.navigate", "params": {"url": url}})
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            message = cdp.recv_json()
            if message.get("id") == command_id:
                if message.get("error"):
                    raise RuntimeError(str(message["error"]))
                return url
        raise TimeoutError(f"navigation timed out: {url}")
    finally:
        cdp.close()


def parse_upstream(value: str):
    text = value.strip()
    if not text:
        return None
    if "://" not in text:
        parts = text.split(":", 3)
        if len(parts) != 4:
            raise ValueError("OPLL_CAPTURE_UPSTREAM must be a proxy URL or HOST:PORT:USERNAME:PASSWORD")
        host, port, username, password = parts
        text = f"socks5://{username}:{password}@{host}:{port}"
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname or not parsed.port:
        raise ValueError("OPLL_CAPTURE_UPSTREAM must use http, https, or socks5")
    host = parsed.hostname
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return parsed, host


def upstream_arguments(value: str) -> list[str]:
    parsed_info = parse_upstream(value)
    if parsed_info is None:
        return ["--mode", "regular"]
    parsed, host = parsed_info
    mode = f"upstream:{parsed.scheme}://{host}:{parsed.port}"
    result = ["--mode", mode]
    if parsed.username is not None or parsed.password is not None:
        result.extend(["--upstream-auth", f"{parsed.username or ''}:{parsed.password or ''}"])
    return result


def start_socks5_bridge(value: str) -> tuple[list[str], subprocess.Popen[bytes] | None, Path | None]:
    parsed_info = parse_upstream(value)
    if parsed_info is None or parsed_info[0].scheme != "socks5":
        return upstream_arguments(value), None, None
    parsed, _ = parsed_info
    uvx = next((Path(folder) / "uvx.exe" for folder in os.getenv("PATH", "").split(os.pathsep) if folder and (Path(folder) / "uvx.exe").is_file()), None)
    if uvx is None:
        raise RuntimeError("SOCKS5 upstream requires uvx (install uv or use an HTTP upstream)")
    bridge_port = reserve_free_port()
    descriptor, auth_name = tempfile.mkstemp(prefix="mitm-socks-auth-", suffix=".txt")
    os.close(descriptor)
    auth_file = Path(auth_name)
    auth_file.write_text(
        f"{unquote(parsed.username or '')}:{unquote(parsed.password or '')}",
        encoding="utf-8",
    )
    command = [
        str(uvx),
        "--python",
        "3.11",
        "--from",
        "pproxy",
        "pproxy",
        "-l",
        f"http://127.0.0.1:{bridge_port}",
        "-r",
        f"socks5://{parsed.hostname}:{parsed.port}##{auth_file}",
    ]
    creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    if os.name == "nt":
        creation_flags |= subprocess.CREATE_NO_WINDOW
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creation_flags,
        )
    except Exception:
        auth_file.unlink(missing_ok=True)
        raise
    try:
        wait_for_port(bridge_port, process, timeout=20, label="SOCKS5 bridge")
    except Exception:
        stop_proxy(process)
        auth_file.unlink(missing_ok=True)
        raise
    return ["--mode", f"upstream:http://127.0.0.1:{bridge_port}"], process, auth_file


def start_browser(browser: str, profile: Path, proxy_port: int, url: str) -> tuple[int, bool]:
    port_marker = profile / "mitmproxy-proxy-port.txt"
    existing_port = read_devtools_port(profile)
    if existing_port and endpoint_ready(existing_port):
        if port_marker.is_file() and port_marker.read_text(encoding="ascii").strip() != str(proxy_port):
            raise RuntimeError("managed Chrome is already running with a different proxy port")
        port_marker.write_text(str(proxy_port), encoding="ascii")
        navigate_page(existing_port, url)
        return existing_port, True
    profile.mkdir(parents=True, exist_ok=True)
    port_marker.write_text(str(proxy_port), encoding="ascii")
    command = [
        browser,
        "--remote-debugging-port=0",
        f"--user-data-dir={profile}",
        f"--proxy-server=http://127.0.0.1:{proxy_port}",
        "--disable-quic",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        url,
    ]
    subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return wait_for_cdp(profile), False


def stop_proxy(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        process.send_signal(signal.CTRL_BREAK_EVENT)
        process.wait(timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def convert_flow_file(mitmdump: Path, flow_file: Path, output: Path) -> None:
    if not flow_file.is_file() or flow_file.stat().st_size == 0:
        raise RuntimeError("mitmproxy did not write a flow file")
    if output.exists():
        output.unlink()
    subprocess.run(
        [
            str(mitmdump),
            "-n",
            "-r",
            str(flow_file),
            "--set",
            f"hardump={output}",
            "--quiet",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=120,
    )
    if not output.is_file():
        raise RuntimeError("mitmproxy flow conversion did not create a HAR")
    flow_file.unlink()


def finalize_capture(output: Path, channel: str) -> dict[str, object]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not output.is_file():
        time.sleep(0.2)
    har, _ = load_har(output)
    audit = audit_har_completeness(har)
    capture = har["log"].setdefault("_capture", {})
    capture.update(
        {
            "recorder": "mitmproxy",
            "channelRequested": channel,
            "completenessAudit": audit,
        }
    )
    output.write_text(json.dumps(har, ensure_ascii=False, indent=2), encoding="utf-8")
    report_path = output.with_suffix(".report.md")
    report_path.write_text(markdown_report(analyze_har(output)), encoding="utf-8")
    summary_path: Path | None = None
    if channel == "gopay":
        summary_path = output.with_suffix(".summary.md")
        summary_path.write_text(render_gopay_summary(summarize_gopay(output)), encoding="utf-8")
    raw = output.read_bytes()
    entries = har.get("log", {}).get("entries", [])
    return {
        "entries": len(entries) if isinstance(entries, list) else 0,
        "sha256": hashlib.sha256(raw).hexdigest().upper(),
        "audit": audit,
        "report": report_path,
        "summary": summary_path,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Capture a persistent Chrome session through mitmproxy.")
    parser.add_argument("--channel", choices=CHANNELS, default="gopay")
    parser.add_argument("--output", "-o", type=Path)
    parser.add_argument("--url", default="https://chatgpt.com/")
    parser.add_argument("--profile", type=Path, default=Path("data/mitmproxy-capture-profile"))
    parser.add_argument("--browser", default="")
    parser.add_argument("--mitmdump", default="")
    parser.add_argument("--mitmweb", default="")
    parser.add_argument("--proxy-port", type=int, default=8899)
    parser.add_argument("--web-port", type=int, default=8081)
    parser.add_argument("--duration", type=float, default=0)
    parser.add_argument("--prompt-upstream", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--doctor", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        mitmdump = find_mitm_binary("mitmdump", args.mitmdump)
        mitmweb = find_mitm_binary("mitmweb", args.mitmweb)
        browser = _find_browser(args.browser)
        if args.doctor:
            version = subprocess.run(
                [str(mitmdump), "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()[0]
            print(f"MITMPROXY_READY=1")
            print(f"MITMPROXY_VERSION={version}")
            print(f"MITMPROXY_BIN={mitmdump}")
            print(f"MITMPROXY_WEB_BIN={mitmweb}")
            print(f"MITMPROXY_BROWSER={browser}")
            return 0
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output = (args.output or Path("artifacts-local") / f"{args.channel}-mitm-capture-{timestamp}.har").resolve()
        profile = args.profile.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        flow_file = output.with_suffix(".mitm")
        if flow_file.exists():
            flow_file.unlink()
        upstream = os.getenv("OPLL_CAPTURE_UPSTREAM", "")
        if args.prompt_upstream and not upstream:
            upstream = getpass.getpass(
                "Upstream proxy (SOCKS5 URL or HOST:PORT:USERNAME:PASSWORD; blank for direct): "
            ).strip()
        require_free_port(args.proxy_port, "proxy")
        require_free_port(args.web_port, "Web UI")
        upstream_args, bridge_process, bridge_auth_file = start_socks5_bridge(upstream)
        command = [
            str(mitmweb),
            *upstream_args,
            "--listen-host",
            "127.0.0.1",
            "--listen-port",
            str(args.proxy_port),
            "--web-host",
            "127.0.0.1",
            "--web-port",
            str(args.web_port),
            "--web-open-browser",
            "--set",
            "http3=false",
            "--ignore-hosts",
            ROXY_IP_CHECK_IGNORE_HOSTS,
            "--save-stream-file",
            str(flow_file),
            "--quiet",
        ]
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=creation_flags,
            )
        except Exception:
            if bridge_process is not None:
                stop_proxy(bridge_process)
            if bridge_auth_file is not None:
                bridge_auth_file.unlink(missing_ok=True)
            raise
        cdp_port: int | None = None
        try:
            wait_for_port(args.proxy_port, process)
            if args.no_browser:
                reused = False
                host = "roxy-proxy"
            else:
                cdp_port, reused = start_browser(browser, profile, args.proxy_port, args.url)
                page = first_page(cdp_port)
                host = urlsplit(str(page.get("url") or "")).hostname or ""
            print("CAPTURE_READY=1", flush=True)
            print("CAPTURE_ENGINE=mitmweb", flush=True)
            print(f"CAPTURE_CHANNEL={args.channel}", flush=True)
            print(f"CAPTURE_CDP={'ROXY_EXTERNAL' if cdp_port is None else f'127.0.0.1:{cdp_port}'}", flush=True)
            print(f"CAPTURE_PROXY=127.0.0.1:{args.proxy_port}", flush=True)
            print(f"CAPTURE_WEB=http://127.0.0.1:{args.web_port}/", flush=True)
            print("CAPTURE_WEB_AUTO_OPEN=1", flush=True)
            print(f"CAPTURE_OUTPUT={output}", flush=True)
            print(f"CAPTURE_TARGET_ATTACHED=page:{host}", flush=True)
            print(f"CAPTURE_BROWSER_REUSED={int(reused)}", flush=True)
            print("CAPTURE_ACTION=complete the flow, then press Ctrl+C", flush=True)
            deadline = time.monotonic() + args.duration if args.duration > 0 else None
            while deadline is None or time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"mitmdump exited during capture (status {process.returncode})")
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            returned_main = int(args.no_browser)
            if cdp_port is not None:
                try:
                    navigate_page(cdp_port, "https://chatgpt.com/")
                    returned_main = 1
                except Exception:
                    pass
            print("CAPTURE_BROWSER_PRESERVED=1", flush=True)
            print(f"CAPTURE_RETURNED_MAIN={returned_main}", flush=True)
            print(f"CAPTURE_NEXT_CYCLE_READY={returned_main}", flush=True)
            stop_proxy(process)
            if bridge_process is not None:
                stop_proxy(bridge_process)
            if bridge_auth_file is not None:
                bridge_auth_file.unlink(missing_ok=True)
        convert_flow_file(mitmdump, flow_file, output)
        result = finalize_capture(output, args.channel)
        audit = result["audit"] if isinstance(result["audit"], dict) else {}
        print(f"CAPTURE_SAVED={output}")
        print(f"CAPTURE_ENTRIES={result['entries']}")
        print(f"CAPTURE_SHA256={result['sha256']}")
        print(f"CAPTURE_COMPLETENESS={'complete' if audit.get('complete') else 'partial'}")
        print(f"CAPTURE_MISSING={json.dumps(audit.get('issues', []), ensure_ascii=False)}")
        print(f"CAPTURE_REPORT={result['report']}")
        if result["summary"]:
            print(f"CAPTURE_SUMMARY={result['summary']}")
        return 0 if audit.get("complete") else 3
    except (FileNotFoundError, OSError, RuntimeError, subprocess.SubprocessError, TimeoutError, ValueError) as exc:
        print(f"CAPTURE_ERROR={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
