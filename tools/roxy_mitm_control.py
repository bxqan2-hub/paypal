from __future__ import annotations

import argparse
import csv
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

try:
    from .har_capture import CDPWebSocket, audit_har_completeness
    from .mitm_capture import finalize_capture, navigate_page
    from .roxy_har_capture import default_roxy_cache, discover_roxy_targets
except ImportError:
    from har_capture import CDPWebSocket, audit_har_completeness
    from mitm_capture import finalize_capture, navigate_page
    from roxy_har_capture import default_roxy_cache, discover_roxy_targets


CHANNELS = {"paypal", "gopay", "gcash", "momo"}
DEFAULT_ROXY_API = "http://127.0.0.1:50000"
CDP_SUPPLEMENT_HOSTS = {"chatgpt.com", "auth.openai.com", "auth0.openai.com", "login.openai.com"}


def wait_for_roxy_cdp(dir_id: str, existing_ports: set[int], timeout: float = 45) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        targets = discover_roxy_targets(default_roxy_cache(), timeout=0.5)
        exact = [target for target in targets if target.profile_id == dir_id]
        fresh = [target for target in targets if target.port not in existing_ports]
        selected = exact or fresh
        if selected:
            return selected[0].port
        time.sleep(0.3)
    raise TimeoutError("Roxy 新窗口没有开放 CDP 调试端口")


def merge_hybrid_har(mitm_output: Path, cdp_output: Path, channel: str) -> dict[str, object]:
    mitm_har = json.loads(mitm_output.read_text(encoding="utf-8"))
    cdp_har = json.loads(cdp_output.read_text(encoding="utf-8"))
    mitm_log = mitm_har.get("log") if isinstance(mitm_har.get("log"), dict) else {}
    cdp_log = cdp_har.get("log") if isinstance(cdp_har.get("log"), dict) else {}
    mitm_entries = mitm_log.get("entries") if isinstance(mitm_log.get("entries"), list) else []
    cdp_entries = cdp_log.get("entries") if isinstance(cdp_log.get("entries"), list) else []
    supplemented: list[dict[str, Any]] = []
    for entry in cdp_entries:
        if not isinstance(entry, dict):
            continue
        request = entry.get("request") if isinstance(entry.get("request"), dict) else {}
        host = (urlsplit(str(request.get("url") or "")).hostname or "").lower()
        if host not in CDP_SUPPLEMENT_HOSTS:
            continue
        detail = entry.setdefault("_capture", {})
        if isinstance(detail, dict):
            detail["source"] = "roxy-cdp-supplement"
        supplemented.append(entry)
    combined = [entry for entry in mitm_entries if isinstance(entry, dict)] + supplemented
    combined.sort(key=lambda entry: str(entry.get("startedDateTime") or ""))
    mitm_log["entries"] = combined
    capture = mitm_log.setdefault("_capture", {})
    if not isinstance(capture, dict):
        capture = {}
        mitm_log["_capture"] = capture
    capture.update(
        {
            "recorder": "mitmproxy+roxy-cdp",
            "mitmEntryCount": len(mitm_entries),
            "cdpSupplementEntryCount": len(supplemented),
            "entryCount": len(combined),
            "completenessAudit": audit_har_completeness(mitm_har),
        }
    )
    mitm_har["log"] = mitm_log
    mitm_output.write_text(json.dumps(mitm_har, ensure_ascii=False, indent=2), encoding="utf-8")
    result = finalize_capture(mitm_output, channel)
    cdp_output.unlink(missing_ok=True)
    return result


def cleanup_stale_mitmweb(ports: tuple[int, ...]) -> None:
    if os.name != "nt":
        return
    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    target_pids: set[int] = set()
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) < 5 or fields[0].upper() != "TCP" or fields[3].upper() != "LISTENING":
            continue
        local_address = fields[1]
        if not any(local_address.endswith(f":{port}") for port in ports):
            continue
        try:
            target_pids.add(int(fields[4]))
        except ValueError:
            continue
    for process_id in target_pids:
        task = subprocess.run(
            ["tasklist", "/FI", f"PID eq {process_id}", "/FO", "CSV", "/NH"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        rows = list(csv.reader(io.StringIO(task.stdout)))
        if not rows or rows[0][0].lower() != "mitmweb.exe":
            continue
        subprocess.run(
            ["taskkill", "/PID", str(process_id), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def request_cdp_stop(
    cdp_process: subprocess.Popen[str] | None,
    cdp_stop_file: Path | None,
) -> None:
    """Ask the CDP recorder to stop without killing its HAR finalizer."""
    if cdp_process is not None and cdp_process.poll() is None and cdp_stop_file is not None:
        cdp_stop_file.write_text("stop", encoding="ascii")


def force_stop_process_tree(process: subprocess.Popen[str] | None) -> None:
    """Terminate a recorder and its children without asking it to save."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()


def close_cdp_browser(port: int | None) -> None:
    """Close the captured Roxy browser window through its browser CDP target."""
    if not port:
        return
    try:
        with urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as response:
            version = json.loads(response.read().decode("utf-8"))
        websocket_url = str(version.get("webSocketDebuggerUrl") or "") if isinstance(version, dict) else ""
        if not websocket_url:
            return
        cdp = CDPWebSocket(websocket_url, timeout=3)
        try:
            cdp.next_id += 1
            cdp.send_json({"id": cdp.next_id, "method": "Browser.close", "params": {}})
        finally:
            cdp.close()
    except (OSError, EOFError, RuntimeError, ValueError, json.JSONDecodeError):
        return


def roxy_request(
    base_url: str,
    path: str,
    api_key: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: float = 60,
) -> dict[str, Any]:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        method=method,
        headers={"token": api_key, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Roxy API request failed: {path}") from exc
    if not isinstance(result, dict) or result.get("code") != 0:
        message = result.get("msg") if isinstance(result, dict) else "invalid response"
        raise RuntimeError(f"Roxy API error: {message}")
    return result


def resolve_workspace_id(base_url: str, api_key: str) -> str:
    result = roxy_request(base_url, "/browser/workspace?page_index=1&page_size=100", api_key)
    data = result.get("data") or {}
    rows = data.get("rows") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Roxy API returned no workspace")
    workspace = rows[0]
    workspace_id = workspace.get("workspaceId") or workspace.get("id")
    if not workspace_id:
        raise RuntimeError("Roxy workspace ID is missing")
    return str(workspace_id)


def create_roxy_window(
    base_url: str,
    api_key: str,
    workspace_id: str,
    window_name: str,
    proxy_port: int,
) -> str:
    result = roxy_request(
        base_url,
        "/browser/create",
        api_key,
        method="POST",
        payload={
            "workspaceId": workspace_id,
            "windowName": window_name,
            "randomFingerprint": True,
            "proxyInfo": {
                "proxyMethod": "custom",
                "proxyCategory": "HTTP",
                "ipType": "IPV4",
                "protocol": "HTTP",
                "host": "127.0.0.1",
                "port": str(proxy_port),
                "proxyUserName": "",
                "proxyPassword": "",
            },
        },
    )
    data = result.get("data") or {}
    dir_id = data.get("dirId") if isinstance(data, dict) else None
    if not dir_id:
        raise RuntimeError("Roxy did not return the new window ID")
    return str(dir_id)


def open_roxy_window(base_url: str, api_key: str, workspace_id: str, dir_id: str) -> None:
    roxy_request(
        base_url,
        "/browser/open",
        api_key,
        method="POST",
        payload={
            "workspaceId": workspace_id,
            "dirId": dir_id,
            "args": [],
            "forceOpen": False,
            "headless": False,
        },
        timeout=90,
    )


@dataclass
class CaptureState:
    root: Path
    proxy_port: int
    web_port: int
    process: subprocess.Popen[str] | None = None
    cdp_process: subprocess.Popen[str] | None = None
    status: str = "idle"
    message: str = "等待设置上游代理"
    output: str = ""
    dir_id: str = ""
    window_name: str = ""
    stop_file: Path | None = None
    cdp_stop_file: Path | None = None
    cdp_output: Path | None = None
    cdp_port: int | None = None
    channel: str = "gopay"
    logs: list[str] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            return {
                "status": self.status,
                "message": self.message,
                "running": running,
                "output": self.output,
                "captureMode": "mitmproxy+roxy-cdp" if self.cdp_process is not None else "mitmproxy",
                "dirId": self.dir_id,
                "windowName": self.window_name,
                "proxy": f"127.0.0.1:{self.proxy_port}",
                "mitmweb": f"http://127.0.0.1:{self.web_port}/",
                "logs": self.logs[-30:],
            }

    def append_log(self, line: str) -> None:
        safe_line = line.strip()
        if not safe_line:
            return
        with self.lock:
            self.logs.append(safe_line)
            del self.logs[:-100]

    def start(self, options: dict[str, Any]) -> dict[str, Any]:
        upstream = str(options.get("upstream") or "").strip()
        api_key = str(options.get("apiKey") or "").strip()
        api_base = str(options.get("apiBase") or DEFAULT_ROXY_API).strip()
        channel = str(options.get("channel") or "gopay").strip().lower()
        window_name = str(options.get("windowName") or "").strip()
        if not upstream:
            raise ValueError("请输入上游 SOCKS5 代理")
        if not api_key:
            raise ValueError("请输入 Roxy API Key")
        if channel not in CHANNELS:
            raise ValueError("抓包渠道无效")
        if not window_name:
            window_name = f"MITM-{channel.upper()}-{time.strftime('%m%d-%H%M%S')}"
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RuntimeError("抓包服务已经在运行")
            self.status = "starting"
            self.message = "正在启动 mitmproxy"
            self.logs.clear()
            self.output = ""
            self.cdp_process = None
            self.cdp_stop_file = None
            self.cdp_output = None
            self.cdp_port = None
            self.channel = channel
            self.dir_id = ""
            self.window_name = window_name
        cleanup_stale_mitmweb((self.proxy_port, self.web_port))
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output = (self.root / "artifacts-local" / f"{channel}-roxy-mitm-{timestamp}.har").resolve()
        stop_file = output.with_suffix(".stop")
        stop_file.unlink(missing_ok=True)
        command = [
            sys.executable,
            str(self.root / "tools" / "mitm_capture.py"),
            "--channel",
            channel,
            "--output",
            str(output),
            "--proxy-port",
            str(self.proxy_port),
            "--web-port",
            str(self.web_port),
            "--no-browser",
            "--stop-file",
            str(stop_file),
        ]
        env = os.environ.copy()
        env["OPLL_CAPTURE_UPSTREAM"] = upstream
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            command,
            cwd=self.root,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creation_flags,
        )
        with self.lock:
            self.process = process
            self.output = str(output)
            self.stop_file = stop_file
        ready = threading.Event()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                self.append_log(line)
                if line.startswith("CAPTURE_READY=1"):
                    ready.set()
            if process.poll() is not None and not ready.is_set():
                ready.set()

        threading.Thread(target=read_output, daemon=True).start()
        if not ready.wait(45) or process.poll() is not None:
            self.stop()
            with self.lock:
                self.message = "启动失败：上游代理格式无效，请填写 HOST:PORT:USERNAME:PASSWORD 或完整代理 URL"
            raise RuntimeError("mitmproxy 启动失败，请查看状态日志")
        try:
            existing_ports = {target.port for target in discover_roxy_targets(default_roxy_cache())}
            workspace_id = resolve_workspace_id(api_base, api_key)
            dir_id = create_roxy_window(
                api_base,
                api_key,
                workspace_id,
                window_name,
                self.proxy_port,
            )
            open_roxy_window(api_base, api_key, workspace_id, dir_id)
            cdp_port = wait_for_roxy_cdp(dir_id, existing_ports)
            cdp_output = output.with_name(f"{output.stem}-cdp.har")
            cdp_stop_file = output.with_name(f"{output.stem}-cdp.stop")
            cdp_output.unlink(missing_ok=True)
            cdp_stop_file.unlink(missing_ok=True)
            cdp_command = [
                sys.executable,
                str(self.root / "tools" / "har_capture_browser_attach.py"),
                "--cdp-port",
                str(cdp_port),
                "--channel",
                channel,
                "--output",
                str(cdp_output),
                "--stop-file",
                str(cdp_stop_file),
            ]
            # The recorder polls its stop marker and must remain alive long
            # enough to run its finally block and write the HAR.
            cdp_process = subprocess.Popen(
                cdp_command,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=0,
            )
            cdp_ready = threading.Event()

            def read_cdp_output() -> None:
                assert cdp_process.stdout is not None
                for line in cdp_process.stdout:
                    self.append_log(f"CDP {line}")
                    if line.startswith("CAPTURE_READY=1"):
                        cdp_ready.set()
                if cdp_process.poll() is not None and not cdp_ready.is_set():
                    cdp_ready.set()

            threading.Thread(target=read_cdp_output, daemon=True).start()
            with self.lock:
                self.cdp_process = cdp_process
                self.cdp_stop_file = cdp_stop_file
                self.cdp_output = cdp_output
                self.cdp_port = cdp_port
            if not cdp_ready.wait(30) or cdp_process.poll() is not None:
                raise RuntimeError("Roxy CDP 补充记录器启动失败")
            # Roxy's open endpoint initially exposes its internal dashboard.
            # Navigate that same fingerprinted profile to the real capture origin.
            navigate_page(cdp_port, "https://chatgpt.com/")
        except Exception as exc:
            self.stop()
            with self.lock:
                self.message = f"启动失败：{exc}"
            raise
        with self.lock:
            self.status = "running"
            self.message = "混合抓包已就绪：mitmproxy 主抓，CDP 自动补齐 ChatGPT"
            self.dir_id = dir_id
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            stop_file = self.stop_file
            cdp_process = self.cdp_process
            cdp_stop_file = self.cdp_stop_file
            cdp_output = self.cdp_output
            output = Path(self.output) if self.output else None
            channel = self.channel
            self.status = "stopping"
            self.message = "正在保存并合并 HAR"
        # The CDP recorder polls the stop marker and flushes before exiting.
        request_cdp_stop(cdp_process, cdp_stop_file)
        if process is not None and process.poll() is None:
            try:
                if stop_file is not None:
                    stop_file.write_text("stop", encoding="ascii")
                elif os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.send_signal(signal.SIGINT)
                process.wait(timeout=180)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
        if cdp_process is not None and cdp_process.poll() is None:
            try:
                cdp_process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                cdp_process.terminate()
                try:
                    cdp_process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    cdp_process.kill()
        merge_result: dict[str, object] | None = None
        merge_error = ""
        if output is not None and cdp_output is not None and cdp_output.is_file():
            try:
                merge_result = merge_hybrid_har(output, cdp_output, channel)
                audit = merge_result.get("audit") if isinstance(merge_result.get("audit"), dict) else {}
                missing = audit.get("issues") if isinstance(audit, dict) else []
                self.append_log(f"CAPTURE_HYBRID_ENTRIES={merge_result.get('entries', 0)}")
                self.append_log(f"CAPTURE_HYBRID_COMPLETENESS={'complete' if audit.get('complete') else 'partial'}")
                self.append_log(f"CAPTURE_HYBRID_MISSING={json.dumps(missing, ensure_ascii=False)}")
            except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as exc:
                merge_error = str(exc)
                self.append_log(f"CAPTURE_HYBRID_ERROR={merge_error}")
        elif cdp_output is not None and not cdp_output.is_file():
            merge_error = "CDP HAR 未写出，已保留 mitmproxy 主 HAR"
            self.append_log(f"CAPTURE_HYBRID_ERROR={merge_error}")
        with self.lock:
            self.process = None
            self.cdp_process = None
            self.stop_file = None
            self.cdp_stop_file = None
            self.cdp_output = None
            self.cdp_port = None
            self.status = "idle"
            if merge_error:
                self.message = f"抓包已停止，但自动合并失败：{merge_error}"
            elif merge_result is not None:
                audit = merge_result.get("audit") if isinstance(merge_result.get("audit"), dict) else {}
                state = "完整" if audit.get("complete") else "部分完整"
                self.message = f"混合 HAR 已保存（{state}）；Roxy 窗口保持打开"
            else:
                self.message = "抓包已停止；Roxy 窗口保持打开"
        if stop_file is not None:
            stop_file.unlink(missing_ok=True)
        if cdp_stop_file is not None:
            cdp_stop_file.unlink(missing_ok=True)
        cleanup_stale_mitmweb((self.proxy_port, self.web_port))
        return self.snapshot()

    def discard(self) -> dict[str, Any]:
        """Close the capture window and discard all artifacts without merging."""
        with self.lock:
            process = self.process
            cdp_process = self.cdp_process
            output = Path(self.output) if self.output else None
            stop_file = self.stop_file
            cdp_stop_file = self.cdp_stop_file
            cdp_output = self.cdp_output
            cdp_port = self.cdp_port
            self.status = "discarding"
            self.message = "正在放弃抓包并关闭窗口"

        force_stop_process_tree(cdp_process)
        force_stop_process_tree(process)
        close_cdp_browser(cdp_port)

        paths: set[Path] = set()
        if output is not None:
            paths.update(
                {
                    output,
                    output.with_suffix(".mitm"),
                    output.with_suffix(".report.md"),
                    output.with_suffix(".summary.md"),
                    output.with_suffix(".stop"),
                }
            )
        if stop_file is not None:
            paths.add(stop_file)
        if cdp_output is not None:
            paths.add(cdp_output)
            paths.add(cdp_output.with_name(f".{cdp_output.name}.checkpoint"))
        if cdp_stop_file is not None:
            paths.add(cdp_stop_file)
        for path in paths:
            path.unlink(missing_ok=True)

        cleanup_stale_mitmweb((self.proxy_port, self.web_port))
        with self.lock:
            self.process = None
            self.cdp_process = None
            self.stop_file = None
            self.cdp_stop_file = None
            self.cdp_output = None
            self.cdp_port = None
            self.output = ""
            self.dir_id = ""
            self.window_name = ""
            self.logs.append("CAPTURE_DISCARDED=1")
            self.status = "idle"
            self.message = "本次抓包已放弃，窗口已关闭，未保存文件"
        return self.snapshot()


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>mitmproxy Roxy 控制台</title>
<style>
:root{color-scheme:light;font-family:Segoe UI,Microsoft YaHei,sans-serif;color:#17202a;background:#eef2f5}
*{box-sizing:border-box}body{margin:0;min-height:100vh}header{height:56px;background:#20262d;color:#fff;display:flex;align-items:center;padding:0 18px;gap:14px}header h1{font-size:17px;margin:0;font-weight:600}header span{font-size:12px;color:#b8c2cc}
main{display:grid;grid-template-rows:auto auto minmax(420px,1fr);min-height:calc(100vh - 56px)}
.controls{background:#fff;border-bottom:1px solid #d7dee5;padding:14px 18px;display:grid;grid-template-columns:minmax(320px,2fr) minmax(220px,1fr) 150px 190px auto;gap:10px;align-items:end}
label{display:grid;gap:5px;font-size:12px;color:#52606d}input,select{height:36px;border:1px solid #b8c2cc;border-radius:4px;padding:0 10px;font:inherit;background:#fff;min-width:0}input:focus,select:focus{outline:2px solid #2589d8;outline-offset:-1px}
.actions{display:flex;gap:8px}.button{height:36px;border:0;border-radius:4px;padding:0 15px;font-weight:600;cursor:pointer;white-space:nowrap}.primary{background:#1683d8;color:#fff}.danger{background:#d64545;color:#fff}.discard{background:#59636e;color:#fff}.button:disabled{opacity:.45;cursor:not-allowed}
.status{height:46px;background:#f8fafb;border-bottom:1px solid #d7dee5;display:flex;align-items:center;gap:12px;padding:0 18px;font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:#8996a3}.dot.running{background:#1f9d61}.dot.starting,.dot.stopping,.dot.discarding{background:#e0a126}.status code{margin-left:auto;color:#52606d}
.workspace{display:grid;grid-template-columns:minmax(0,1fr) 320px;min-height:0}.mitm{position:relative;background:#fff}.mitm iframe{width:100%;height:100%;border:0}.empty{position:absolute;inset:0;display:grid;place-items:center;color:#6b7785;background:#fff}.log{background:#20262d;color:#d9e1e8;padding:12px;overflow:auto;font:12px/1.5 Consolas,monospace;white-space:pre-wrap}.log h2{font:600 13px Segoe UI;margin:0 0 8px;color:#fff}
@media(max-width:1000px){.controls{grid-template-columns:1fr 1fr}.actions{grid-column:1/-1}.workspace{grid-template-columns:1fr}.log{max-height:220px}.controls label:first-child{grid-column:1/-1}}
</style>
</head>
<body>
<header><h1>mitmproxy Roxy 控制台</h1><span>上游代理仅保存在当前进程内存</span></header>
<main>
<section class="controls">
<label>上游 SOCKS5 代理<input id="upstream" type="password" autocomplete="off" placeholder="HOST:PORT:USERNAME:PASSWORD"></label>
<label>Roxy API Key<input id="apiKey" type="password" autocomplete="off" placeholder="仅本机 API 使用"></label>
<label>渠道<select id="channel"><option value="gopay">GoPay</option><option value="momo">MoMo</option><option value="paypal">PayPal</option><option value="gcash">GCash</option></select></label>
<label>新窗口名称<input id="windowName" placeholder="留空自动命名"></label>
<div class="actions"><button id="start" class="button primary">开始并新建窗口</button><button id="stop" class="button danger">停止并保存</button><button id="discard" class="button discard">放弃并关闭窗口</button></div>
<input id="apiBase" type="hidden" value="http://127.0.0.1:50000">
</section>
<section class="status"><span id="dot" class="dot"></span><strong id="state">未启动</strong><span id="message">等待设置上游代理</span><code id="proxy">HTTP 127.0.0.1:8899</code></section>
<section class="workspace"><div class="mitm"><div id="empty" class="empty">启动后在这里查看 mitmweb</div><iframe id="mitm" title="mitmweb"></iframe></div><aside id="log" class="log"><h2>运行状态</h2></aside></section>
</main>
<script>
const elements={upstream:document.querySelector('#upstream'),apiKey:document.querySelector('#apiKey'),apiBase:document.querySelector('#apiBase'),channel:document.querySelector('#channel'),windowName:document.querySelector('#windowName'),start:document.querySelector('#start'),stop:document.querySelector('#stop'),discard:document.querySelector('#discard'),dot:document.querySelector('#dot'),state:document.querySelector('#state'),message:document.querySelector('#message'),proxy:document.querySelector('#proxy'),log:document.querySelector('#log'),mitm:document.querySelector('#mitm'),empty:document.querySelector('#empty')};
async function request(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const data=await response.json();if(!response.ok)throw new Error(data.error||'操作失败');return data}
function render(data){elements.dot.className='dot '+data.status;elements.state.textContent={idle:'未启动',starting:'启动中',running:'运行中',stopping:'停止保存中',discarding:'正在放弃'}[data.status]||data.status;elements.message.textContent=data.message;elements.proxy.textContent='HTTP '+data.proxy;const busy=data.status==='starting'||data.status==='stopping'||data.status==='discarding';elements.start.disabled=data.running||busy;elements.stop.disabled=!data.running||busy;elements.discard.disabled=!data.running||busy;elements.log.replaceChildren();const title=document.createElement('h2');title.textContent='运行状态';elements.log.append(title,document.createTextNode(data.logs.join('\n')));if(data.running){elements.empty.hidden=true;if(!elements.mitm.src)elements.mitm.src=data.mitmweb}else{elements.empty.hidden=false;elements.mitm.removeAttribute('src')}}
async function refresh(){try{render(await (await fetch('/api/status')).json())}catch(error){elements.message.textContent=error.message}}
elements.start.addEventListener('click',async()=>{elements.start.disabled=true;elements.message.textContent='正在启动';try{const data=await request('/api/start',{upstream:elements.upstream.value,apiKey:elements.apiKey.value,apiBase:elements.apiBase.value,channel:elements.channel.value,windowName:elements.windowName.value});elements.apiKey.value='';elements.upstream.value='';render(data)}catch(error){elements.message.textContent=error.message;await refresh()}});
elements.stop.addEventListener('click',async()=>{elements.stop.disabled=true;try{render(await request('/api/stop'))}catch(error){elements.message.textContent=error.message;await refresh()}});
elements.discard.addEventListener('click',async()=>{elements.discard.disabled=true;try{render(await request('/api/discard'))}catch(error){elements.message.textContent=error.message;await refresh()}});
setInterval(refresh,1500);refresh();
</script>
</body>
</html>"""


def build_handler(state: CaptureState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def send_json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 65536:
                return {}
            value = json.loads(self.rfile.read(length).decode("utf-8"))
            return value if isinstance(value, dict) else {}

        def do_GET(self) -> None:
            if self.path == "/":
                data = HTML.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path == "/api/status":
                self.send_json(200, state.snapshot())
                return
            self.send_error(404)

        def do_POST(self) -> None:
            try:
                if self.path == "/api/start":
                    self.send_json(200, state.start(self.read_json()))
                    return
                if self.path == "/api/stop":
                    self.send_json(200, state.stop())
                    return
                if self.path == "/api/discard":
                    self.send_json(200, state.discard())
                    return
                self.send_error(404)
            except (ValueError, RuntimeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc), **state.snapshot()})

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description="Local mitmproxy and RoxyBrowser control panel")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--proxy-port", type=int, default=8899)
    parser.add_argument("--web-port", type=int, default=8081)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    state = CaptureState(root=root, proxy_port=args.proxy_port, web_port=args.web_port)
    server = ThreadingHTTPServer((args.host, args.port), build_handler(state))
    url = f"http://{args.host}:{args.port}/"
    print("ROXY_MITM_CONTROL_READY=1", flush=True)
    print(f"ROXY_MITM_CONTROL={url}", flush=True)
    if not args.no_open:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
