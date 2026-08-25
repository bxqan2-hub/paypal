from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


APP_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = APP_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from payment_batch import parse_tokens


STATIC_DIR = APP_DIR / "static"
DEFAULT_DATA_DIR = APP_DIR / "data"
DEFAULT_BATCH_SCRIPT = PROJECT_ROOT / "payment_batch.py"
MAX_BODY_BYTES = 4_000_000
MAX_LOG_LINES = 4_000
MAX_ACCOUNTS = 2_000
RESULT_HOST = "pm-redirects.stripe.com"
BATCH_RESULT_MARKER = "[BATCH_RESULT] "
ACCOUNT_START_MARKER = "[ACCOUNT_START] "
ACCOUNT_LOG_MARKER = "[ACCOUNT_LOG] "
MAX_ACCOUNT_LOG_LINES = 240


class RequestError(ValueError):
    pass


def int_field(
    payload: dict[str, Any], name: str, default: int, minimum: int, maximum: int
) -> int:
    raw = payload.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"{name} 必须是整数") from exc
    if not minimum <= value <= maximum:
        raise RequestError(f"{name} 必须在 {minimum}-{maximum} 之间")
    return value


def float_field(
    payload: dict[str, Any], name: str, default: float, minimum: float, maximum: float
) -> float:
    raw = payload.get(name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise RequestError(f"{name} 必须是数字") from exc
    if value != value or not minimum <= value <= maximum:
        raise RequestError(f"{name} 必须在 {minimum:g}-{maximum:g} 之间")
    return value


def clean_lines(value: Any) -> list[str]:
    lines = []
    for raw in str(value or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if len(line) > 2_000:
            raise RequestError("单条代理内容过长")
        lines.append(line)
    lines = list(dict.fromkeys(lines))
    if not lines:
        raise RequestError("请填写至少一条代理")
    if len(lines) > 10_000:
        raise RequestError("代理数量不能超过 10000 条")
    return lines


def clean_tokens(value: Any) -> tuple[str, list[str]]:
    raw = str(value or "").strip()
    if not raw:
        raise RequestError("请填写批量 Access Token")
    if len(raw) > 3_000_000:
        raise RequestError("批量 Access Token 内容过大")
    tokens = parse_tokens(raw)
    if not tokens:
        raise RequestError("未识别到有效 Access Token")
    if len(tokens) > MAX_ACCOUNTS:
        raise RequestError(f"账号数量不能超过 {MAX_ACCOUNTS}")
    return raw, tokens


def validate_redirect_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == RESULT_HOST
        and parsed.path.startswith("/authorize/")
        and "oaics_" not in str(value).lower()
    )


_JWT_RE = re.compile(r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}")
_SESSION_RE = re.compile(
    r"(?i)(__Secure-next-auth\.session-token(?:=|\"\s*:\s*\")?)[^\s;\"]+"
)
_PROXY_URL_RE = re.compile(r"(?i)\b(https?|socks5h?)://([^\s/@:]+):([^\s/@]+)@")
_HOST_PROXY_RE = re.compile(
    r"(?<![\w.-])([A-Za-z0-9.-]+):(\d{2,5}):([^\s:]+):([^\s]+)"
)


def redact_log(value: str) -> str:
    text = _JWT_RE.sub("<access-token>", str(value or ""))
    text = _SESSION_RE.sub(r"\1<session-token>", text)
    text = _PROXY_URL_RE.sub(r"\1://<credentials>@", text)
    text = _HOST_PROXY_RE.sub(r"\1:\2:<credentials>", text)
    return text[:8_000]


def log_level(line: str) -> str:
    lowered = line.lower()
    if "[error]" in lowered or "flow.fatal" in lowered or "flow.exhausted" in lowered:
        return "error"
    if "[warn]" in lowered or "flow.failed" in lowered or "失败" in line:
        return "warn"
    if "成功" in line or "poll.redirect" in lowered:
        return "success"
    return "info"


def normalize_account_label(value: Any) -> str:
    account = re.sub(
        r"[^A-Za-z0-9.!#$%&'*+/=?^_`{|}~@-]",
        "",
        str(value or "unknown"),
    )[:254]
    return account or "unknown"


def normalize_batch_result(value: dict[str, Any]) -> dict[str, Any]:
    try:
        index = max(1, int(value.get("index") or 1))
    except (TypeError, ValueError):
        index = 1
    account = normalize_account_label(value.get("account"))
    status = str(value.get("status") or "failed").strip().lower()
    detail = redact_log(str(value.get("detail") or "提链失败"))[:300]
    url = str(value.get("url") or "").strip()

    if status == "success":
        if "oaics_" in url.lower():
            status = "oaics_rejected"
            detail = "已拒绝 oaics_ Checkout"
            url = ""
        elif not validate_redirect_url(url):
            status = "invalid_gopay_redirect"
            detail = "GoPay 返回的授权链接无效"
            url = ""
    else:
        url = ""
    if status == "oaics_rejected":
        detail = "已拒绝 oaics_ Checkout"
        url = ""

    try:
        exit_code = int(value.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = None
    return {
        "index": index,
        "account": account,
        "status": status,
        "url": url,
        "detail": detail,
        "exit_code": exit_code,
    }


def public_error(last_line: str, exit_code: int | None) -> str:
    lowered = last_line.lower()
    if "oaics_" in lowered:
        return "批次拒绝了 oaics_ Checkout"
    if "403" in lowered or "unusual activity" in lowered:
        return "Checkout 请求被拒绝，请更换代理后重试"
    if "proxy" in lowered or "代理" in lowered:
        return "代理节点不可用"
    if exit_code is not None:
        return f"批量进程退出，exit={exit_code}"
    return "批量任务未完成"


_GOPAY_EVENT_RE = re.compile(
    r"^\[[^\]]+\]\s+([a-z][a-z0-9_.-]+):\s*(\{.*\})$"
)


def simplify_account_log(raw_line: str) -> dict[str, str] | None:
    text = redact_log(raw_line)
    match = _GOPAY_EVENT_RE.match(text)
    if not match:
        return None
    event_name = match.group(1)
    try:
        payload = json.loads(match.group(2))
    except json.JSONDecodeError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    if "oaics_" in text.lower():
        return {"stage": "checkout", "level": "error", "message": "已拒绝 oaics_ Checkout"}
    if event_name == "flow.attempt":
        attempt = int(payload.get("attempt") or 1)
        total = int(payload.get("total") or attempt)
        return {"stage": "network", "level": "info", "message": f"第 {attempt}/{total} 次尝试"}
    if event_name == "checkout.integrity":
        return {"stage": "checkout", "level": "info", "message": "正在创建 Checkout"}
    if event_name == "checkout.created":
        return {"stage": "checkout", "level": "success", "message": "Checkout 已创建"}
    if event_name == "stripe.init":
        if payload.get("amount") == 0 and "gopay" in (payload.get("methods") or []):
            return {"stage": "gopay", "level": "success", "message": "0 元 GoPay 已确认"}
        return {"stage": "gopay", "level": "warn", "message": "GoPay 条件不匹配"}
    if event_name == "stripe.elements":
        return {"stage": "gopay", "level": "info", "message": "支付会话已准备"}
    if event_name == "checkout.snapshot":
        return {"stage": "checkout", "level": "info", "message": "Checkout 状态已同步"}
    if event_name == "stripe.confirm.before":
        return {"stage": "submit", "level": "info", "message": "正在提交 GoPay"}
    if event_name == "stripe.confirm.after":
        status = int(payload.get("http_status") or 0)
        return {
            "stage": "submit",
            "level": "success" if status == 200 else "warn",
            "message": "GoPay 已提交" if status == 200 else f"GoPay 提交返回 HTTP {status or '未知'}",
        }
    if event_name == "approve.before":
        return {"stage": "approve", "level": "info", "message": "正在完成支付确认"}
    if event_name == "approve.after":
        approved = str(payload.get("result") or "").lower() == "approved"
        return {
            "stage": "approve",
            "level": "success" if approved else "warn",
            "message": "支付确认已通过" if approved else "支付确认未通过",
        }
    if event_name == "poll.state":
        if str(payload.get("decision") or "").lower() == "redirect":
            return None
        return {"stage": "result", "level": "info", "message": "等待 GoPay 授权链接"}
    if event_name == "poll.terminal":
        return {"stage": "result", "level": "success", "message": "GoPay 授权链接已生成"}
    if event_name == "flow.failed":
        attempt = int(payload.get("attempt") or 0)
        prefix = f"第 {attempt} 次尝试失败" if attempt else "本次尝试失败"
        return {"stage": "retry", "level": "warn", "message": f"{prefix}，切换节点"}
    if event_name in {"flow.exhausted", "flow.fatal"}:
        return {"stage": "result", "level": "error", "message": "所有尝试均未取得 GoPay 链接"}
    return None


class BatchRunner:
    def __init__(
        self,
        *,
        data_dir: Path = DEFAULT_DATA_DIR,
        project_root: Path = PROJECT_ROOT,
        batch_script: Path = DEFAULT_BATCH_SCRIPT,
        python_executable: str = sys.executable,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.project_root = Path(project_root)
        self.batch_script = Path(batch_script)
        self.python_executable = python_executable
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.task_id = ""
        self.state = "idle"
        self.created_at = 0.0
        self.started_at = 0.0
        self.finished_at = 0.0
        self.exit_code: int | None = None
        self.error = ""
        self.config: dict[str, Any] = {}
        self.results: dict[int, dict[str, Any]] = {}
        self.account_logs: dict[int, deque[dict[str, Any]]] = {}
        self.account_log_ids: dict[int, int] = {}
        self.logs: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_LINES)
        self.next_log_id = 1
        self._runtime_dir: Path | None = None
        self._last_diagnostic = ""

    def _append_message_locked(self, line: str) -> None:
        line = redact_log(line.rstrip("\r\n"))
        if not line:
            return
        self.logs.append(
            {
                "id": self.next_log_id,
                "time": int(time.time() * 1000),
                "level": log_level(line),
                "message": line,
            }
        )
        self.next_log_id += 1

    def _append_account_log_locked(
        self,
        index: int,
        account: str,
        *,
        stage: str,
        level: str,
        message: str,
    ) -> None:
        logs = self.account_logs.setdefault(index, deque(maxlen=MAX_ACCOUNT_LOG_LINES))
        if logs and logs[-1]["stage"] == stage and logs[-1]["message"] == message:
            return
        next_id = self.account_log_ids.get(index, 1)
        logs.append(
            {
                "id": next_id,
                "time": int(time.time() * 1000),
                "level": level,
                "stage": stage,
                "message": message,
            }
        )
        self.account_log_ids[index] = next_id + 1
        current = self.results.get(index)
        if current is None:
            self.results[index] = {
                "index": index,
                "account": account,
                "status": "running",
                "url": "",
                "detail": message,
                "exit_code": None,
            }
        elif current.get("status") == "running":
            current["detail"] = message

    def _append_locked(self, raw_line: str) -> None:
        line = raw_line.rstrip("\r\n")
        if not line:
            return
        if line.startswith(ACCOUNT_START_MARKER):
            try:
                payload = json.loads(line[len(ACCOUNT_START_MARKER) :])
                index = max(1, int(payload.get("index") or 1))
                account = normalize_account_label(payload.get("account"))
            except (json.JSONDecodeError, TypeError, ValueError):
                return
            self._append_account_log_locked(
                index,
                account,
                stage="task",
                level="info",
                message="账号任务已启动",
            )
            return
        if line.startswith(ACCOUNT_LOG_MARKER):
            try:
                payload = json.loads(line[len(ACCOUNT_LOG_MARKER) :])
                index = max(1, int(payload.get("index") or 1))
                account = normalize_account_label(payload.get("account"))
            except (json.JSONDecodeError, TypeError, ValueError):
                return
            simplified = simplify_account_log(str(payload.get("line") or ""))
            if simplified is not None:
                self._append_account_log_locked(
                    index,
                    account,
                    stage=simplified["stage"],
                    level=simplified["level"],
                    message=simplified["message"],
                )
            return
        if line.startswith(BATCH_RESULT_MARKER):
            try:
                payload = json.loads(line[len(BATCH_RESULT_MARKER) :])
            except json.JSONDecodeError:
                self._last_diagnostic = "批量结果 JSON 无效"
                self._append_message_locked(self._last_diagnostic)
                return
            if isinstance(payload, dict):
                result = normalize_batch_result(payload)
                self.results[int(result["index"])] = result
                terminal_level = "success" if result["status"] == "success" else "error"
                terminal_message = (
                    "GoPay 授权链接已生成"
                    if result["status"] == "success"
                    else result["detail"]
                )
                self._append_account_log_locked(
                    int(result["index"]),
                    str(result["account"]),
                    stage="result",
                    level=terminal_level,
                    message=terminal_message,
                )
                self._append_message_locked(
                    f"#{result['index']} {result['account']} · {result['detail']}"
                )
            return
        public_line = redact_log(line)
        if "flow.failed" in public_line or "flow.fatal" in public_line or "[ERROR]" in public_line:
            self._last_diagnostic = public_line
        self._append_message_locked(public_line)

    def _cleanup_inputs(self) -> None:
        runtime_dir = self._runtime_dir
        if runtime_dir is None or not runtime_dir.exists():
            return
        for pattern in ("batch_tokens.txt", "proxy_seeds.txt"):
            for path in runtime_dir.rglob(pattern):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def _summary_locked(self) -> dict[str, int]:
        results = list(self.results.values())
        success = sum(item["status"] == "success" for item in results)
        stopped = sum(item["status"] == "stopped" for item in results)
        completed = sum(item["status"] != "running" for item in results)
        return {
            "total": int(self.config.get("account_count") or 0),
            "completed": completed,
            "success": success,
            "failed": completed - success - stopped,
            "stopped": stopped,
        }

    def _reader(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            with self.lock:
                self._append_locked(line)
        exit_code = process.wait()
        with self.lock:
            if process is not self.process:
                return
            self.exit_code = exit_code
            self.finished_at = time.time()
            if self.state == "stopping":
                self.state = "stopped"
                self.error = "批量任务已停止"
            elif exit_code == 0:
                self.state = "completed"
                self.error = ""
            else:
                self.state = "failed"
                self.error = public_error(self._last_diagnostic, exit_code)
            self.process = None
            summary = self._summary_locked()
            if self.state == "completed":
                self._append_message_locked(
                    f"批量完成：成功 {summary['success']}，失败 {summary['failed']}"
                )
            else:
                self._append_message_locked(self.error)
        self._cleanup_inputs()

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        token_text, tokens = clean_tokens(payload.get("tokens"))
        proxies = clean_lines(payload.get("proxies"))
        concurrency = int_field(payload, "concurrency", 0, 0, 500)
        max_retry = int_field(payload, "max_retry", 5, 1, 100)
        poll_timeout = int_field(payload, "poll_timeout", 45, 5, 600)
        poll_interval_ms = int_field(payload, "poll_interval_ms", 1000, 100, 10_000)
        start_interval = float_field(payload, "start_interval", 1.0, 0.0, 60.0)
        proxy_scheme = str(payload.get("proxy_scheme") or "auto").strip().lower()
        proxy_scheme = {"socket": "socks5", "socket5": "socks5"}.get(
            proxy_scheme, proxy_scheme
        )
        if proxy_scheme not in {"auto", "socks5", "socks5h", "http", "https"}:
            raise RequestError("proxy_scheme 必须是 auto/socks5/socks5h/http/https")
        diagnostics = bool(payload.get("diagnostics", False))

        with self.lock:
            if self.process is not None and self.process.poll() is None:
                raise RequestError("已有批量任务正在运行")
            if not self.batch_script.is_file():
                raise RuntimeError(f"批量执行器不存在: {self.batch_script}")

            task_id = uuid.uuid4().hex[:12]
            runtime_dir = self.data_dir / "batches" / task_id
            runtime_dir.mkdir(parents=True, exist_ok=False)
            token_path = runtime_dir / "batch_tokens.txt"
            proxy_path = runtime_dir / "proxy_seeds.txt"
            token_path.write_text(token_text + "\n", encoding="utf-8")
            proxy_path.write_text("\n".join(proxies) + "\n", encoding="utf-8")
            os.chmod(token_path, 0o600)
            os.chmod(proxy_path, 0o600)

            env = os.environ.copy()
            for name in (
                "PP_TOKEN",
                "PP_SESSION_TOKEN",
                "GOPAY_TOKEN",
                "GOPAY_SESSION_TOKEN",
            ):
                env.pop(name, None)
            env.update(
                {
                    "PYTHONUNBUFFERED": "1",
                    "PAYMENT_BATCH_METHOD": "gopay",
                    "PAYMENT_BATCH_TOKEN_FILE": str(token_path),
                    "PAYMENT_BATCH_PROXY_FILE": str(proxy_path),
                    "PAYMENT_BATCH_CONCURRENCY": str(concurrency),
                    "PAYMENT_BATCH_START_INTERVAL": str(start_interval),
                    "PAYMENT_BATCH_RUN_ROOT": str(runtime_dir / "runs"),
                    "GOPAY_MAX_RETRY": str(max_retry),
                    "GOPAY_CHECKOUT_RETRY_MAX": str(max_retry),
                    "GOPAY_PROVIDER_RETRY_MAX": "1",
                    "GOPAY_POLL_TIMEOUT": str(poll_timeout),
                    "GOPAY_POLL_INTERVAL_MS": str(poll_interval_ms),
                    "GOPAY_PROXY_DEFAULT_SCHEME": proxy_scheme,
                    "GOPAY_DUMP": "1" if diagnostics else "0",
                    "GOPAY_ALLOW_DIRECT": "0",
                }
            )

            self.logs.clear()
            self.next_log_id = 1
            self.task_id = task_id
            self.state = "running"
            self.created_at = time.time()
            self.started_at = self.created_at
            self.finished_at = 0.0
            self.exit_code = None
            self.error = ""
            self.results = {}
            self.account_logs = {}
            self.account_log_ids = {}
            effective = len(tokens) if concurrency == 0 else min(concurrency, len(tokens))
            self.config = {
                "account_count": len(tokens),
                "proxy_count": len(proxies),
                "concurrency": concurrency,
                "effective_concurrency": effective,
                "max_retry": max_retry,
                "poll_timeout": poll_timeout,
                "poll_interval_ms": poll_interval_ms,
                "start_interval": start_interval,
                "proxy_scheme": proxy_scheme,
                "diagnostics": diagnostics,
            }
            self._runtime_dir = runtime_dir
            self._last_diagnostic = ""
            self._append_message_locked(
                f"GoPay 批量任务已启动：账号 {len(tokens)}，并发 {effective}"
            )

            try:
                process = subprocess.Popen(
                    [self.python_executable, "-u", str(self.batch_script)],
                    cwd=str(self.project_root),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    start_new_session=True,
                )
            except Exception:
                self.state = "failed"
                self.finished_at = time.time()
                self._cleanup_inputs()
                raise
            self.process = process
            threading.Thread(
                target=self._reader,
                args=(process,),
                name=f"gopay-batch-{task_id}",
                daemon=True,
            ).start()
            return self.snapshot()

    def stop(self) -> dict[str, Any]:
        with self.lock:
            process = self.process
            if process is None or process.poll() is not None:
                raise RequestError("当前没有运行中的批量任务")
            self.state = "stopping"
            self._append_message_locked("正在停止整个批次")
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                process.kill()
        return self.snapshot()

    def snapshot(self, after: int = 0) -> dict[str, Any]:
        with self.lock:
            return {
                "task_id": self.task_id,
                "state": self.state,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "exit_code": self.exit_code,
                "error": self.error,
                "config": self.config.copy(),
                "summary": self._summary_locked(),
                "results": [self.results[index].copy() for index in sorted(self.results)],
                "logs": [item.copy() for item in self.logs if int(item["id"]) > after],
                "next_log_id": self.next_log_id,
            }

    def account_log_snapshot(self, index: int, after: int = 0) -> dict[str, Any]:
        with self.lock:
            logs = self.account_logs.get(index, ())
            account = self.results.get(index, {}).get("account", "")
            return {
                "task_id": self.task_id,
                "index": index,
                "account": account,
                "logs": [item.copy() for item in logs if int(item["id"]) > after],
                "next_log_id": self.account_log_ids.get(index, 1),
            }

    def csv_bytes(self) -> bytes:
        with self.lock:
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["index", "account", "status", "detail", "url"])
            for index in sorted(self.results):
                item = self.results[index]
                writer.writerow(
                    [item["index"], item["account"], item["status"], item["detail"], item["url"]]
                )
            return output.getvalue().encode("utf-8-sig")


RUNNER = BatchRunner(
    data_dir=Path(os.environ.get("GOPAY_LINK_DATA_DIR", "").strip() or DEFAULT_DATA_DIR)
)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "GoPayBatch/2.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _bytes(
        self,
        status: int,
        raw: bytes,
        content_type: str,
        *,
        disposition: str = "",
        cache_control: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _json(self, status: int, payload: dict[str, Any]) -> None:
        self._bytes(
            status,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise RequestError("请求长度无效") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise RequestError("请求内容为空或过大")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("JSON 请求无效") from exc
        if not isinstance(payload, dict):
            raise RequestError("JSON 请求必须是对象")
        return payload

    def _static(self, path: Path, content_type: str) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._bytes(
            HTTPStatus.OK,
            path.read_bytes(),
            content_type,
            cache_control="no-cache",
        )

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        if parsed.path == "/healthz":
            self._json(HTTPStatus.OK, {"ok": True, "state": RUNNER.snapshot()["state"]})
            return
        if parsed.path == "/api/meta":
            self._json(
                HTTPStatus.OK,
                {
                    "name": "GoPay 批量提链",
                    "route": "ID / IDR / GoPay",
                    "oaics_policy": "reject",
                    "defaults": {
                        "concurrency": 0,
                        "start_interval": 1.0,
                        "max_retry": 5,
                        "poll_timeout": 45,
                        "poll_interval_ms": 1000,
                    },
                },
            )
            return
        if parsed.path == "/api/task":
            query = parse_qs(parsed.query)
            try:
                after = max(0, int((query.get("after") or ["0"])[0]))
            except ValueError:
                after = 0
            self._json(HTTPStatus.OK, RUNNER.snapshot(after))
            return
        if parsed.path == "/api/account-logs":
            query = parse_qs(parsed.query)
            try:
                index = max(1, int((query.get("index") or ["0"])[0]))
                after = max(0, int((query.get("after") or ["0"])[0]))
            except ValueError:
                self._json(HTTPStatus.BAD_REQUEST, {"error": "账号索引无效"})
                return
            self._json(HTTPStatus.OK, RUNNER.account_log_snapshot(index, after))
            return
        if parsed.path == "/api/results.csv":
            self._bytes(
                HTTPStatus.OK,
                RUNNER.csv_bytes(),
                "text/csv; charset=utf-8",
                disposition='attachment; filename="gopay-results.csv"',
            )
            return
        if parsed.path in {"/", "/index.html"}:
            self._static(STATIC_DIR / "index.html", "text/html; charset=utf-8")
            return
        if parsed.path == "/static/app.js":
            self._static(STATIC_DIR / "app.js", "text/javascript; charset=utf-8")
            return
        if parsed.path == "/static/styles.css":
            self._static(STATIC_DIR / "styles.css", "text/css; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urlsplit(self.path)
        try:
            if parsed.path == "/api/task/start":
                self._json(HTTPStatus.ACCEPTED, RUNNER.start(self._body()))
                return
            if parsed.path == "/api/task/stop":
                self._json(HTTPStatus.OK, RUNNER.stop())
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except RequestError as exc:
            self._json(HTTPStatus.CONFLICT, {"error": str(exc)})
        except Exception as exc:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)[:300]})


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="GoPay batch link extractor UI")
    parser.add_argument("--host", default=os.environ.get("GOPAY_LINK_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port", type=int, default=int(os.environ.get("GOPAY_LINK_PORT", "8791"))
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    server.daemon_threads = True
    print(f"GoPay 批量提链已启动: http://{args.host}:{args.port}")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
