#!/usr/bin/env python3
"""Local, memory-only web interface for the standalone GCash link flow."""

import argparse
import base64
import hashlib
import json
import os
import re
import secrets
import threading
import time
import urllib.parse
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from gcash_chain import (
    GCashSessionManager,
    QueueFullError,
    _ensure_full_chain_proxy_supported,
    _parse_proxy,
)


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_PROXIES = 100
JOB_RETENTION_SECONDS = 60 * 60
JWT_RE = re.compile(
    r"(eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+)"
)
JOB_RE = re.compile(r"local_[a-f0-9]{16}")
CLIENT_RE = re.compile(r"acct_[a-f0-9]{16}")


def _bounded_env_int(name, default, upper):
    try:
        return max(1, min(int(os.getenv(name, default)), upper))
    except (TypeError, ValueError):
        return default


MAX_CONCURRENCY = _bounded_env_int("MK_MAX_CONCURRENCY", 4, 12)
MAX_ACCOUNTS = _bounded_env_int("MK_MAX_ACCOUNTS", 50, 50)
MAX_SESSION_CONCURRENCY = _bounded_env_int(
    "MK_MAX_SESSION_CONCURRENCY", min(4, MAX_CONCURRENCY), 12
)
MAX_QUEUE = _bounded_env_int("MK_MAX_QUEUE", 50, 200)
CHAIN_MANAGER = GCashSessionManager(
    max_concurrency=MAX_CONCURRENCY,
    max_queue=MAX_QUEUE,
    max_session_concurrency=min(MAX_SESSION_CONCURRENCY, MAX_CONCURRENCY),
)
JOB_META = {}
JOB_META_LOCK = threading.Lock()


def _decode_jwt_payload(token):
    try:
        part = token.split(".")[1]
        part += "=" * ((4 - len(part) % 4) % 4)
        return json.loads(base64.urlsafe_b64decode(part.encode("ascii")).decode("utf-8"))
    except (IndexError, ValueError, UnicodeError, json.JSONDecodeError):
        return {}


def _token_from_text(value):
    match = JWT_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _line_fields(line):
    line = str(line or "").strip()
    if not line:
        return "", "", ""
    email = ""
    name = ""
    parts = [part.strip() for part in line.split("|")]
    if len(parts) >= 3:
        name = "|".join(parts[:-2]).strip()
        if "@" in parts[-2]:
            email = parts[-2]
    elif len(parts) == 2:
        if "@" in parts[0]:
            email = parts[0]
        else:
            name = parts[0]
    return _token_from_text(line), email, name


def _account_from_token(token, explicit_email="", explicit_name=""):
    if not token or len(token) > 32_768:
        raise ValueError("AT 格式无效")
    payload = _decode_jwt_payload(token)
    profile = payload.get("https://api.openai.com/profile")
    auth = payload.get("https://api.openai.com/auth")
    profile = profile if isinstance(profile, dict) else {}
    auth = auth if isinstance(auth, dict) else {}
    expires_at = payload.get("exp")
    try:
        expires_at = int(expires_at) if expires_at is not None else None
    except (TypeError, ValueError):
        expires_at = None
    if expires_at is not None and expires_at <= int(time.time()):
        raise ValueError("AT 已过期")
    email = str(
        explicit_email
        or profile.get("email")
        or payload.get("email")
        or payload.get("preferred_username")
        or ""
    ).strip()
    name = str(
        explicit_name
        or profile.get("name")
        or payload.get("name")
        or payload.get("given_name")
        or ""
    ).strip()
    account_id = str(
        auth.get("chatgpt_account_id")
        or payload.get("chatgpt_account_id")
        or payload.get("account_id")
        or ""
    ).strip()
    return {
        "token": token,
        "email": email,
        "name": name,
        "account_id": account_id,
        "expires_at": expires_at,
    }


def parse_accounts(payload):
    candidates = []
    raw_text = payload.get("tokens", "")
    if raw_text:
        if not isinstance(raw_text, str):
            raise ValueError("tokens 必须是字符串")
        for line_number, line in enumerate(raw_text.splitlines(), 1):
            if not line.strip():
                continue
            token, email, name = _line_fields(line)
            candidates.append((line_number, token, email, name))

    raw_accounts = payload.get("accounts", [])
    if raw_accounts:
        if not isinstance(raw_accounts, list):
            raise ValueError("accounts 必须是数组")
        base_line = len(candidates)
        for index, item in enumerate(raw_accounts, 1):
            if not isinstance(item, dict):
                candidates.append((base_line + index, "", "", ""))
                continue
            token = _token_from_text(
                item.get("access_token") or item.get("token") or item.get("authorization")
            )
            candidates.append((
                base_line + index,
                token,
                str(item.get("email") or "").strip(),
                str(item.get("name") or "").strip(),
            ))

    if not candidates:
        raise ValueError("请至少提交一个 AT")
    if len(candidates) > MAX_ACCOUNTS:
        raise ValueError(f"当前部署每次最多提交 {MAX_ACCOUNTS} 个账号")

    parsed = []
    warnings = []
    seen = set()
    for line_number, token, email, name in candidates:
        if not token:
            warnings.append({"line": line_number, "error": "未找到 JWT AT"})
            continue
        token_hash = hashlib.sha256(token.encode("utf-8")).digest()
        if token_hash in seen:
            warnings.append({"line": line_number, "error": "重复 AT，已跳过"})
            continue
        try:
            account = _account_from_token(token, email, name)
        except ValueError as exc:
            warnings.append({"line": line_number, "error": str(exc)})
            continue
        seen.add(token_hash)
        parsed.append(account)
    if not parsed:
        raise ValueError(warnings[0]["error"] if warnings else "没有可用账号")
    return parsed, warnings


def parse_proxy_pool(value):
    if isinstance(value, str):
        raw_items = value.splitlines()
    elif isinstance(value, list):
        raw_items = value
    else:
        raise ValueError("proxy_pool 必须是字符串或数组")
    pool = []
    seen = set()
    for raw_proxy in raw_items:
        proxy = str(raw_proxy or "").strip()
        if not proxy or proxy in seen:
            continue
        if len(proxy) > 1024 or any(character.isspace() for character in proxy):
            raise ValueError("代理格式无效")
        try:
            _, host, port, _, _ = _parse_proxy(proxy)
            _ensure_full_chain_proxy_supported(proxy)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise ValueError(str(exc) or "代理格式无效") from exc
        if not host or not port or not 1 <= int(port) <= 65535:
            raise ValueError("代理格式无效")
        seen.add(proxy)
        pool.append(proxy)
        if len(pool) > MAX_PROXIES:
            raise ValueError(f"自备代理最多 {MAX_PROXIES} 条")
    if not pool:
        raise ValueError("请填写至少一个 PH 住宅代理")
    return pool


def _cleanup_meta():
    cutoff = time.time() - JOB_RETENTION_SECONDS
    with JOB_META_LOCK:
        expired = [job_id for job_id, meta in JOB_META.items() if meta["created"] < cutoff]
        for job_id in expired:
            JOB_META.pop(job_id, None)
    CHAIN_MANAGER.cleanup()


def create_job(payload):
    if payload.get("proxy_mode") not in (None, "", "custom"):
        raise ValueError("开源版仅支持用户自备代理池")
    accounts, warnings = parse_accounts(payload)
    proxy_pool = parse_proxy_pool(payload.get("proxy_pool", []))
    try:
        max_attempts = int(payload.get("max_attempts", 5))
    except (TypeError, ValueError) as exc:
        raise ValueError("max_attempts 必须是整数") from exc
    if not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts 必须在 1 到 10 之间")

    _cleanup_meta()
    job_id = CHAIN_MANAGER.create_session()
    task_payloads = []
    account_meta = {}
    for index, account in enumerate(accounts):
        client_id = "acct_" + secrets.token_hex(8)
        account_meta[client_id] = {
            "email": account["email"],
            "name": account["name"],
            "expires_at": account["expires_at"],
        }
        task_payloads.append({
            "token": account["token"],
            "account_id": account["account_id"],
            "billing_email": account["email"],
            "billing_name": account["name"],
            "client_account_id": client_id,
            "proxy": proxy_pool[index % len(proxy_pool)],
            "proxy_pool": proxy_pool,
            "max_attempts": max_attempts,
        })
    try:
        CHAIN_MANAGER.submit_jobs(job_id, task_payloads)
    except Exception:
        CHAIN_MANAGER.discard_session(job_id)
        raise
    with JOB_META_LOCK:
        JOB_META[job_id] = {
            "created": time.time(),
            "accounts": account_meta,
            "warnings": warnings,
        }
    return public_job(job_id)


def _job_meta(job_id):
    if not JOB_RE.fullmatch(str(job_id or "")):
        raise KeyError("任务不存在")
    with JOB_META_LOCK:
        meta = JOB_META.get(job_id)
    if not meta:
        raise KeyError("任务不存在或已清理")
    return meta


def _task(job_id, client_id):
    _job_meta(job_id)
    if not CLIENT_RE.fullmatch(str(client_id or "")):
        raise KeyError("账号任务不存在")
    for task in CHAIN_MANAGER.get_tasks(job_id):
        if task.get("client_account_id") == client_id:
            return task
    raise KeyError("账号任务不存在")


def _public_task(job_id, task, meta):
    client_id = task.get("client_account_id") or ""
    account = meta["accounts"].get(client_id, {})
    monitor = {}
    monitor_id = task.get("monitor_id") or ""
    if monitor_id:
        monitor = CHAIN_MANAGER.monitor_status(monitor_id) or {}
    monitor_status = monitor.get("status") or task.get("callback_status") or "unavailable"
    callback_error = ""
    if monitor_status == "callback_unconfirmed":
        callback_error = "回调已受理，但未确认 Plus 权益到账"
    elif monitor_status == "callback_failed":
        callback_error = "支付回调失败"
    elif monitor_status == "expired":
        callback_error = "二维码已过期"
    status = task.get("status") or "queued"
    link = task.get("gcash_url") or monitor.get("gcash_url") or ""
    qr_ready = bool(monitor.get("qr_ready"))
    return {
        "id": client_id,
        "email": account.get("email") or "",
        "name": account.get("name") or "",
        "status": status,
        "current_step": task.get("current_step") or "",
        "steps": task.get("steps") or [],
        "queue_position": task.get("queue_position"),
        "attempts_used": int(task.get("attempts_used") or 0),
        "error": task.get("error_message") or callback_error,
        "link_ready": status == "success" and bool(link),
        "link": link,
        "expires_at": monitor.get("expires_at") or task.get("expires_at"),
        "qr_ready": qr_ready,
        "qr_source": monitor.get("qr_source") or "",
        "qr_version": int(monitor.get("qr_version") or 0),
        "qr_url": (
            f"/api/jobs/{job_id}/accounts/{client_id}/qr.png"
            if monitor_id and qr_ready else ""
        ),
        "refresh_available": bool(monitor.get("refresh_available")),
        "payment_status": monitor_status,
        "payment_success": monitor_status == "completed",
    }


def public_job(job_id):
    meta = _job_meta(job_id)
    session = CHAIN_MANAGER.get_session(job_id)
    if not session:
        raise KeyError("任务不存在或已清理")
    items = [_public_task(job_id, task, meta) for task in session.get("tasks") or []]
    return {
        "job_id": job_id,
        "done": bool(session.get("done")),
        "accounts": items,
        "warnings": list(meta.get("warnings") or []),
        "queue": CHAIN_MANAGER.queue_status(),
    }


def cancel_job(job_id):
    _job_meta(job_id)
    summary = CHAIN_MANAGER.cancel_session(job_id)
    return {"ok": True, **summary, "job": public_job(job_id)}


def refresh_account(job_id, client_id):
    task = _task(job_id, client_id)
    monitor_id = task.get("monitor_id") or ""
    if not monitor_id:
        raise RuntimeError("该账号尚未生成可刷新的 GCash 页面")
    CHAIN_MANAGER.refresh_monitor(monitor_id)
    return _public_task(job_id, _task(job_id, client_id), _job_meta(job_id))


ABANDONABLE_MONITOR_STATUSES = {
    "starting", "waiting_scan", "refreshing", "redirect_captured",
    "callback_processing", "expired",
}


def abandon_account(job_id, client_id):
    task = _task(job_id, client_id)
    monitor_id = task.get("monitor_id") or ""
    if task.get("status") != "success" or not monitor_id:
        raise RuntimeError("该账号没有活动支付监控")
    status = CHAIN_MANAGER.monitor_status(monitor_id).get("status") or "unavailable"
    if status == "completed":
        raise RuntimeError("支付已经完成")
    if status not in ABANDONABLE_MONITOR_STATUSES:
        raise RuntimeError("当前支付监控已经结束")
    CHAIN_MANAGER.abandon_monitor(monitor_id)
    return _public_task(job_id, _task(job_id, client_id), _job_meta(job_id))


def abandon_job(job_id):
    _job_meta(job_id)
    session = CHAIN_MANAGER.get_session(job_id)
    if not session:
        raise KeyError("任务不存在或已清理")
    if not session.get("done"):
        raise RuntimeError("提链任务尚未完成，请先停止任务")

    abandoned = 0
    for task in session.get("tasks") or []:
        monitor_id = task.get("monitor_id") or ""
        if task.get("status") != "success" or not monitor_id:
            continue
        status = CHAIN_MANAGER.monitor_status(monitor_id).get("status") or "unavailable"
        if status in ABANDONABLE_MONITOR_STATUSES:
            CHAIN_MANAGER.abandon_monitor(monitor_id)
            abandoned += 1
    return {"ok": True, "abandoned": abandoned, "job": public_job(job_id)}


def account_qr(job_id, client_id):
    task = _task(job_id, client_id)
    monitor_id = task.get("monitor_id") or ""
    return CHAIN_MANAGER.monitor_qr_image(monitor_id) if monitor_id else b""


class Handler(SimpleHTTPRequestHandler):
    server_version = "MKLink/1.0"
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".webp": "image/webp",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(WEB_ROOT), **kwargs)

    def log_message(self, _format, *_args):
        return

    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Opener-Policy", "same-origin")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; form-action 'self'; "
            "frame-ancestors 'none'; object-src 'none'; script-src 'self'; "
            "style-src 'self'; img-src 'self' data:; connect-src 'self'",
        )
        super().end_headers()

    def _json(self, value, status=200):
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip()
        if content_type != "application/json":
            raise ValueError("Content-Type 必须是 application/json")
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if not 0 <= length <= MAX_BODY_BYTES:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length)
        value = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(value, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return value

    def _error(self, exc):
        if isinstance(exc, KeyError):
            message = str(exc.args[0] if exc.args else "任务不存在")
            return self._json({"error": message}, 404)
        if isinstance(exc, QueueFullError):
            return self._json({"error": str(exc)}, 429)
        if isinstance(exc, (ValueError, json.JSONDecodeError, UnicodeError)):
            return self._json({"error": str(exc)}, 400)
        if isinstance(exc, RuntimeError):
            return self._json({"error": str(exc)}, 409)
        return self._json({"error": "请求失败"}, 500)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/health":
                return self._json({
                    "ok": True,
                    "queue": CHAIN_MANAGER.queue_status(),
                    "limits": {"max_accounts": MAX_ACCOUNTS},
                })
            match = re.fullmatch(r"/api/jobs/(local_[a-f0-9]{16})", path)
            if match:
                return self._json(public_job(match.group(1)))
            match = re.fullmatch(
                r"/api/jobs/(local_[a-f0-9]{16})/accounts/(acct_[a-f0-9]{16})/qr\.png",
                path,
            )
            if match:
                png = account_qr(match.group(1), match.group(2))
                if not png:
                    return self._json({"error": "二维码尚未就绪"}, 404)
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(png)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(png)
                return
            if path == "/":
                self.path = "/index.html"
                return super().do_GET()
            if path in {
                "/index.html", "/app.js", "/style.css", "/assets/logo.svg",
                "/assets/mikael-mail-logo.webp",
            }:
                return super().do_GET()
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            return self._error(exc)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/jobs":
                return self._json(create_job(self._body()), 202)
            match = re.fullmatch(r"/api/jobs/(local_[a-f0-9]{16})/cancel", path)
            if match:
                self._body()
                return self._json(cancel_job(match.group(1)))
            match = re.fullmatch(r"/api/jobs/(local_[a-f0-9]{16})/abandon", path)
            if match:
                self._body()
                return self._json(abandon_job(match.group(1)))
            match = re.fullmatch(
                r"/api/jobs/(local_[a-f0-9]{16})/accounts/(acct_[a-f0-9]{16})/refresh",
                path,
            )
            if match:
                self._body()
                return self._json(refresh_account(match.group(1), match.group(2)))
            match = re.fullmatch(
                r"/api/jobs/(local_[a-f0-9]{16})/accounts/(acct_[a-f0-9]{16})/abandon",
                path,
            )
            if match:
                self._body()
                return self._json(abandon_account(match.group(1), match.group(2)))
            return self._json({"error": "not found"}, 404)
        except Exception as exc:
            return self._error(exc)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128


def main():
    parser = argparse.ArgumentParser(description="MK GCash link workspace")
    parser.add_argument("--host", default=os.getenv("MK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("MK_PORT", "8931")))
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    server = Server((args.host, args.port), Handler)
    print(f"MK GCash Link: http://{args.host}:{args.port}/", flush=True)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: the service has no login layer; place it behind your own authentication.", flush=True)
    if args.open_browser:
        browser_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
        opener = threading.Timer(
            0.8, webbrowser.open, args=(f"http://{browser_host}:{args.port}/",)
        )
        opener.daemon = True
        opener.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
