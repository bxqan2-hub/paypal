from __future__ import annotations

import argparse
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
from urllib.request import Request, urlopen


CHANNELS = {"paypal", "gopay", "gcash"}
DEFAULT_ROXY_API = "http://127.0.0.1:50000"


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
    status: str = "idle"
    message: str = "等待设置上游代理"
    output: str = ""
    dir_id: str = ""
    window_name: str = ""
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
            self.dir_id = ""
            self.window_name = window_name
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        output = (self.root / "artifacts-local" / f"{channel}-roxy-mitm-{timestamp}.har").resolve()
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
            raise RuntimeError("mitmproxy 启动失败，请查看状态日志")
        try:
            workspace_id = resolve_workspace_id(api_base, api_key)
            dir_id = create_roxy_window(
                api_base,
                api_key,
                workspace_id,
                window_name,
                self.proxy_port,
            )
            open_roxy_window(api_base, api_key, workspace_id, dir_id)
        except Exception:
            self.stop()
            raise
        with self.lock:
            self.status = "running"
            self.message = "抓包已就绪，Roxy 新窗口已打开"
            self.dir_id = dir_id
        return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            self.status = "stopping"
            self.message = "正在保存 HAR"
        if process is not None and process.poll() is None:
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.send_signal(signal.SIGINT)
                process.wait(timeout=90)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.kill()
        with self.lock:
            self.process = None
            self.status = "idle"
            self.message = "抓包已停止；Roxy 窗口保持打开"
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
.actions{display:flex;gap:8px}.button{height:36px;border:0;border-radius:4px;padding:0 15px;font-weight:600;cursor:pointer;white-space:nowrap}.primary{background:#1683d8;color:#fff}.danger{background:#d64545;color:#fff}.button:disabled{opacity:.45;cursor:not-allowed}
.status{height:46px;background:#f8fafb;border-bottom:1px solid #d7dee5;display:flex;align-items:center;gap:12px;padding:0 18px;font-size:13px}.dot{width:9px;height:9px;border-radius:50%;background:#8996a3}.dot.running{background:#1f9d61}.dot.starting,.dot.stopping{background:#e0a126}.status code{margin-left:auto;color:#52606d}
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
<label>渠道<select id="channel"><option value="gopay">GoPay</option><option value="paypal">PayPal</option><option value="gcash">GCash</option></select></label>
<label>新窗口名称<input id="windowName" placeholder="留空自动命名"></label>
<div class="actions"><button id="start" class="button primary">开始并新建窗口</button><button id="stop" class="button danger">停止抓包</button></div>
<input id="apiBase" type="hidden" value="http://127.0.0.1:50000">
</section>
<section class="status"><span id="dot" class="dot"></span><strong id="state">未启动</strong><span id="message">等待设置上游代理</span><code id="proxy">HTTP 127.0.0.1:8899</code></section>
<section class="workspace"><div class="mitm"><div id="empty" class="empty">启动后在这里查看 mitmweb</div><iframe id="mitm" title="mitmweb"></iframe></div><aside id="log" class="log"><h2>运行状态</h2></aside></section>
</main>
<script>
const elements={upstream:document.querySelector('#upstream'),apiKey:document.querySelector('#apiKey'),apiBase:document.querySelector('#apiBase'),channel:document.querySelector('#channel'),windowName:document.querySelector('#windowName'),start:document.querySelector('#start'),stop:document.querySelector('#stop'),dot:document.querySelector('#dot'),state:document.querySelector('#state'),message:document.querySelector('#message'),proxy:document.querySelector('#proxy'),log:document.querySelector('#log'),mitm:document.querySelector('#mitm'),empty:document.querySelector('#empty')};
async function request(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const data=await response.json();if(!response.ok)throw new Error(data.error||'操作失败');return data}
function render(data){elements.dot.className='dot '+data.status;elements.state.textContent={idle:'未启动',starting:'启动中',running:'运行中',stopping:'停止中'}[data.status]||data.status;elements.message.textContent=data.message;elements.proxy.textContent='HTTP '+data.proxy;elements.start.disabled=data.running||data.status==='starting';elements.stop.disabled=!data.running;elements.log.replaceChildren();const title=document.createElement('h2');title.textContent='运行状态';elements.log.append(title,document.createTextNode(data.logs.join('\n')));if(data.running){elements.empty.hidden=true;if(!elements.mitm.src)elements.mitm.src=data.mitmweb}else{elements.empty.hidden=false;elements.mitm.removeAttribute('src')}}
async function refresh(){try{render(await (await fetch('/api/status')).json())}catch(error){elements.message.textContent=error.message}}
elements.start.addEventListener('click',async()=>{elements.start.disabled=true;elements.message.textContent='正在启动';try{const data=await request('/api/start',{upstream:elements.upstream.value,apiKey:elements.apiKey.value,apiBase:elements.apiBase.value,channel:elements.channel.value,windowName:elements.windowName.value});elements.apiKey.value='';elements.upstream.value='';render(data)}catch(error){elements.message.textContent=error.message;await refresh()}});
elements.stop.addEventListener('click',async()=>{elements.stop.disabled=true;try{render(await request('/api/stop'))}catch(error){elements.message.textContent=error.message;await refresh()}});
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
