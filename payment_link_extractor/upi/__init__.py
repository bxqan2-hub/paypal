"""Isolated adapter to the byte-identical upi-zero-link upstream core."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from ..auth import account_email, normalize_access_token
from ..errors import ConfigurationError, ExtractionCancelled, NetworkError, ProtocolError
from ..models import BillingProfile, ExtractionConfig, PaymentLinkResult
from ..transport import normalize_proxy_url

UPI_RESULT_FIELD = "upi_url"
ROOT = Path(__file__).resolve().parents[2]
_ERRORS = {
    "nonzero_due": "本次结账应付金额不是 0，已停止提炼",
    "upi_unavailable": "当前会话不支持 UPI",
    "tax_unverified": "无法读取印度税区金额，已停止提炼",
    "declined": "Stripe 拒绝创建 UPI 委托",
    "link_unverified": "UPI 链接未通过上游核心核验",
    "no_link": "上游未返回 UPI 委托链接",
    "checkout_http_401": "AT 已失效，请重新输入",
    "runtime_failure": "UPI 核心运行失败，请检查本地依赖",
}


def _failure(code: str, *, retryable: bool = False, network: bool = False):
    message = "UPI：" + _ERRORS.get(code, code)
    error = NetworkError("upi", message) if network else ProtocolError(400, message)
    error.retryable = retryable
    error.failure_mode = "upi_" + code
    return error


def _run_core(account, proxy, cancel_event, stage_callback):
    """Send credentials over stdin, not argv; isolate the Node/Python runtime."""
    environment = dict(os.environ)
    for key in tuple(environment):
        if key.startswith("MIN_GCASH_"):
            environment.pop(key)
    environment.update(
        PYTHONUTF8="1", PYTHONIOENCODING="utf-8",
        PYTHON_BIN=sys.executable, SENTINEL_PYTHON=sys.executable,
    )
    if not shutil.which(environment.get("SENTINEL_NODE") or "node"):
        raise _failure("runtime_failure")
    messages = queue.Queue()
    process = subprocess.Popen(
        [sys.executable, "-u", "-m", "payment_link_extractor.upi.worker"],
        cwd=ROOT, env=environment,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding="utf-8",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

    def read_events():
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        messages.put(message)
                except ValueError:
                    pass
        finally:
            messages.put(None)

    reader = threading.Thread(target=read_events, daemon=True, name="upi-core-events")
    reader.start()
    try:
        process.stdin.write(json.dumps({"account": account, "proxy": proxy}) + chr(10))
        process.stdin.close()
        deadline = time.monotonic() + 600
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ExtractionCancelled("UPI 提炼已取消")
            if time.monotonic() >= deadline:
                raise _failure("核心运行超时；未自动重建结账会话")
            try:
                message = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if message is None:
                raise _failure("runtime_failure")
            if message.get("type") == "stage":
                if stage_callback:
                    stage_callback(message["stage"])
            elif message.get("type") == "result":
                if not message.get("ok"):
                    raise _failure(
                        str(message.get("error") or "runtime_failure"),
                        retryable=message.get("retryable") is True,
                        network=message.get("network") is True,
                    )
                return message["result"]
    finally:
        if process.poll() is None:
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        check=False,
                    )
                else:
                    process.terminate()
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
        reader.join(timeout=2)
        if process.stdout:
            process.stdout.close()
        if process.stdin and not process.stdin.closed:
            process.stdin.close()


def extract_upi_payment_link(
    config: ExtractionConfig, *, cancel_event=None, stage_callback=None,
) -> PaymentLinkResult:
    if cancel_event is not None and cancel_event.is_set():
        raise ExtractionCancelled("UPI 提炼已取消")
    token = normalize_access_token(config.access_token)
    email = account_email(token) or str(config.account_email or "").strip()
    if not token or "@" not in email:
        error = ConfigurationError("UPI 无法从 AT 识别邮箱；请导入包含 email 和 access_token 的账号 JSON")
        error.retryable = False
        raise error
    try:
        proxy_url = normalize_proxy_url(config.checkout_proxy)
        proxy = urlsplit(proxy_url)
        valid_proxy = proxy.scheme in {"http", "https", "socks5", "socks5h"} and bool(proxy.hostname)
        proxy.port
    except ValueError:
        valid_proxy = False
    if not valid_proxy:
        raise _failure("请输入有效的 IN 印度出口代理 URL")
    if stage_callback:
        stage_callback("checkout_kind:cs")
    raw = _run_core(
        {"email": email, "access_token": token, "session_token": config.session_token},
        proxy_url, cancel_event, stage_callback,
    )
    url = str(raw.get("url") or "")
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname != "payments.stripe.com" or not parsed.path.startswith("/upi/instructions/"):
        raise _failure("link_unverified")
    if raw.get("due") not in (0, "0"):
        raise _failure("nonzero_due")
    expires = int(raw.get("expires_at") or 0)
    if expires and expires <= time.time():
        raise _failure("UPI 链接已过期")
    billing = raw.get("billing") or {}
    qr = str(raw.get("qr_png") or "")
    qr_parts = urlsplit(qr)
    if qr_parts.scheme != "https" or qr_parts.hostname != "qr.stripe.com":
        qr = ""
    return PaymentLinkResult(
        checkout_session_id="", session_kind="cs", payment_method="upi",
        billing_country="IN", currency="INR", amount_due=0, amount_due_minor=0,
        account_email=email,
        billing=BillingProfile(
            name=str(billing.get("name") or ""), email=email, phone="", country="IN",
            line1=str(billing.get("line1") or ""), city=str(billing.get("city") or ""),
            state=str(billing.get("state") or ""), postal_code=str(billing.get("postal_code") or ""),
        ),
        provider_url=url, provider_field=UPI_RESULT_FIELD, provider_value=url,
        extra={
            "upi_qr_png": qr, "upi_expires_at": expires,
            "upi_seconds_left": max(0, expires - int(time.time())) if expires else None,
            "upi_verified": True,
        },
    )
