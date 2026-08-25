#!/usr/bin/env python3
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no POSIX flock
    fcntl = None


ROOT = Path(__file__).resolve().parent
STOP_EVENT = threading.Event()
CHILDREN_LOCK = threading.RLock()
PRINT_LOCK = threading.RLock()
CHILDREN: set[subprocess.Popen[str]] = set()


@contextmanager
def checkout_request_gate() -> Any:
    """Serialize only the first GCash Checkout request across child processes."""
    path_value = os.environ.get("PAYMENT_BATCH_CHECKOUT_GATE", "").strip()
    if not path_value or fcntl is None:
        yield
        return
    path = Path(path_value).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
URL_RE = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s]+")
GCASH_QR_RE = re.compile(r"^GCSHLNKV[0-9A-Za-z]+$")
ACCOUNT_START_MARKER = "[ACCOUNT_START] "
ACCOUNT_LOG_MARKER = "[ACCOUNT_LOG] "
GOPAY_ACCOUNT_EVENTS = {
    "flow.attempt",
    "checkout.integrity",
    "checkout.created",
    "stripe.init",
    "stripe.elements",
    "checkout.snapshot",
    "stripe.confirm.before",
    "stripe.confirm.after",
    "approve.before",
    "approve.after",
    "poll.state",
    "poll.terminal",
    "flow.failed",
    "flow.exhausted",
    "flow.fatal",
}

METHODS: dict[str, dict[str, Any]] = {
    "stripe": {
        "script": ROOT / "ideal_qr_extract.py",
        "prefix": "IDEAL",
        "token_envs": ("PP_TOKEN", "IDEAL_TOKEN"),
        "marker": "Stripe 支付链接:",
        "detail": "已提取 Stripe 支付链接",
    },
    "card": {
        "script": ROOT / "ideal_qr_extract.py",
        "prefix": "IDEAL",
        "token_envs": ("PP_TOKEN", "IDEAL_TOKEN"),
        "marker": "Card Checkout 短链:",
        "detail": "已提取 Card Checkout 短链",
    },
    "ideal": {
        "script": ROOT / "ideal_qr_extract.py",
        "prefix": "IDEAL",
        "token_envs": ("PP_TOKEN", "IDEAL_TOKEN"),
        "marker": "iDEAL 最终扫码/授权 URL:",
        "detail": "已提取 iDEAL 支付链接",
    },
    "pix": {
        "script": ROOT / "pix" / "pix_extract.py",
        "prefix": "PIX",
        "token_envs": ("PP_TOKEN", "PIX_TOKEN"),
        "marker": "PIX 最终支付 URL:",
        "detail": "已提取 PIX 支付链接",
    },
    "blik": {
        "script": ROOT / "blik" / "blik_qr_extract.py",
        "prefix": "IDEAL",
        "token_envs": ("PP_TOKEN", "IDEAL_TOKEN"),
        "marker": "BLIK 自动提交完成",
        "detail": "BLIK 自动提交完成",
        "marker_only": True,
    },
    "twint": {
        "script": ROOT / "twint" / "twint_extract.py",
        "prefix": "TWINT",
        "token_envs": ("PP_TOKEN", "TWINT_TOKEN"),
        "marker": "TWINT 最终支付 URL:",
        "detail": "已提取 TWINT 支付链接",
    },
    "gcash": {
        "script": ROOT / "gcash" / "gcash_extract.py",
        "prefix": "GCASH",
        "token_envs": ("PP_TOKEN", "GCASH_TOKEN"),
        "marker": "GCash 最终支付 URL:",
        "detail": "已提取 GCash 支付链接",
    },
    "gopay": {
        "script": ROOT / "gopay" / "gopay_extract.py",
        "prefix": "GOPAY",
        "token_envs": ("PP_TOKEN", "GOPAY_TOKEN"),
        "marker": "GoPay 最终支付 URL:",
        "detail": "已提取 GoPay 支付链接",
    },
    "upi": {
        "script": ROOT / "upi" / "upi_extract.py",
        "prefix": "UPI",
        "token_envs": ("PP_TOKEN", "UPI_TOKEN"),
        "marker": "UPI 最终支付 URL:",
        "detail": "已提取 UPI 支付链接",
    },
    "paypal": {
        "script": ROOT / "paypal" / "paypal_extract.py",
        "prefix": "PAYPAL",
        "token_envs": ("PP_TOKEN", "PAYPAL_TOKEN"),
        "marker": "PayPal 最终授权 URL:",
        "detail": "已提取 PayPal 授权链接",
    },
}


def log(message: str) -> None:
    with PRINT_LOCK:
        print(message, flush=True)


def extract_token(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip().removeprefix("Bearer ").strip()
        if text.count(".") >= 2 and len(text) >= 100:
            return text
        try:
            return extract_token(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            return ""
    if isinstance(value, dict):
        for key in ("access_token", "accessToken", "token", "bearerToken"):
            token = extract_token(value.get(key))
            if token:
                return token
    return ""


def parse_tokens(text: str) -> list[str]:
    raw = str(text or "").strip()
    if not raw:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        candidates: list[Any] = [line for line in raw.splitlines() if line.strip()]
    else:
        if isinstance(payload, list):
            candidates = payload
        elif isinstance(payload, dict) and isinstance(payload.get("accounts"), list):
            candidates = payload["accounts"]
        else:
            candidates = [payload]

    tokens: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        token = extract_token(candidate)
        if token and token not in seen:
            seen.add(token)
            tokens.append(token)
    return tokens


def account_email_from_token(token: str) -> str:
    """Read the account email from an Access Token JWT without verifying it."""
    try:
        payload_part = str(token or "").split(".", 2)[1]
        padding = "=" * (-len(payload_part) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode((payload_part + padding).encode("ascii"))
        )
    except (IndexError, ValueError, UnicodeError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    profile = payload.get("https://api.openai.com/profile")
    if not isinstance(profile, dict):
        profile = {}
    email = str(profile.get("email") or payload.get("email") or "").strip()
    if (
        not email
        or len(email) > 254
        or email.count("@") != 1
        or any(character.isspace() or ord(character) < 32 for character in email)
    ):
        return ""
    local_part, domain = email.rsplit("@", 1)
    if not local_part or not domain or "." not in domain:
        return ""
    return email


def account_label_from_token(token: str) -> str:
    return (
        account_email_from_token(token)
        or hashlib.sha256(token.encode()).hexdigest()[:10]
    )


def assign_proxy_buckets(proxies: list[str], account_count: int) -> list[list[str]]:
    if account_count <= 0:
        return []
    if not proxies:
        return [[] for _ in range(account_count)]
    buckets = [proxies[index::account_count] for index in range(account_count)]
    for index, bucket in enumerate(buckets):
        if not bucket:
            buckets[index] = [proxies[index % len(proxies)]]
    return buckets


def extract_result(output: str, method: dict[str, Any]) -> tuple[bool, str]:
    marker = str(method["marker"])
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if marker not in line:
            continue
        if method.get("marker_only"):
            return True, ""
        same_line = line.split(marker, 1)[1].strip()
        match = URL_RE.search(same_line)
        if match:
            return True, match.group(0)
        for candidate in lines[index + 1 : index + 4]:
            match = URL_RE.search(candidate.strip())
            if match:
                return True, match.group(0)
        return False, ""
    return False, ""


def extract_gcash_qr_payload(output: str) -> str:
    """Return the raw payload encoded by GCash's QR page, if present."""
    for line in str(output or "").splitlines():
        value = line.strip()
        if GCASH_QR_RE.fullmatch(value):
            return value
    return ""


def is_gopay_authorize_url(value: str) -> bool:
    try:
        parsed = urlsplit(str(value or "").strip())
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == "pm-redirects.stripe.com"
        and parsed.path.startswith("/authorize/")
        and "oaics_" not in str(value).lower()
    )


def classify_failure(output: str, exit_code: int) -> tuple[str, str]:
    lowered = output.lower()
    if "oaics_" in lowered and ("reject" in lowered or "拒绝" in output):
        return "oaics_rejected", "已拒绝 oaics_ Checkout"
    if "user is already paid" in lowered or "用户已支付" in output:
        return "already_paid", "账号已支付"
    if "approve blocked" in lowered or "approve 未通过: blocked" in output:
        return "approve_blocked", "OpenAI approve blocked"
    if "generic_decline" in lowered:
        return "generic_decline", "Stripe generic_decline"
    if "billing_details[tax_id]" in lowered and "parameter_missing" in lowered:
        return "pix_tax_id_missing", "PIX 缺少巴西 CPF 税号"
    if "parameter_missing" in lowered or "invalid_request_error" in lowered:
        return "stripe_invalid_request", "Stripe 请求参数不完整"
    if " 401" in lowered or "http 401" in lowered or "unauthorized" in lowered:
        return "token_invalid", "Token 无效或已过期"
    if " 403" in lowered or "http 403" in lowered or "cf-mitigated" in lowered:
        return "checkout_403", "Checkout 被 403 拒绝"
    if any(
        marker in lowered
        for marker in (
            "tls connect error",
            "proxyerror",
            "proxy connect failed",
            "proxy precheck failed",
            "failed to connect",
            "could not connect",
        )
    ) or "代理连接失败" in output:
        return "proxy_failed", "代理连接失败"
    if STOP_EVENT.is_set() or exit_code in {-15, 143}:
        return "stopped", "任务已停止"
    return "failed", f"提链失败，exit={exit_code}"


def child_environment(
    method_id: str,
    token: str,
    proxy_file: Path,
    state_file: Path,
) -> dict[str, str]:
    method = METHODS[method_id]
    prefix = str(method["prefix"])
    env = os.environ.copy()
    if method_id != "gopay":
        for name in tuple(env):
            if name.startswith("GOPAY_"):
                env.pop(name, None)
    for name in (
        "PP_TOKEN",
        "IDEAL_TOKEN",
        "PIX_TOKEN",
        "TWINT_TOKEN",
        "GCASH_TOKEN",
        "GOPAY_TOKEN",
        "GOPAY_SESSION_TOKEN",
        "UPI_TOKEN",
        "PAYPAL_TOKEN",
        "PP_SESSION_TOKEN",
        "PAYMENT_BATCH_CHECKOUT_GATE",
    ):
        env.pop(name, None)
    for name in method["token_envs"]:
        env[name] = token
    env[f"{prefix}_PROXY_SEED_FILE"] = str(proxy_file)
    env[f"{prefix}_PROXY_STATE_FILE"] = str(state_file)
    env[f"{prefix}_WORKERS"] = "1"
    env[f"{prefix}_WORKERS_MAX"] = "1"
    env[f"{prefix}_PROXY_SKIP_FAILED"] = "0"
    env[f"{prefix}_PROXY_REMOVE_FAILED"] = "0"
    return env


def should_forward(line: str) -> bool:
    lowered = line.lower()
    return any(
        marker in lowered
        for marker in (
            "checkout 创建成功",
            "stripe init",
            "checkout/update 成功",
            "payment_method",
            "stripe confirm",
            "approve",
            "最终支付 url",
            "最终授权 url",
            "支付链接:",
            "checkout 短链:",
            "自动提交完成",
            "全部失败",
            "generic_decline",
            "parameter_missing",
            "missing required param",
            "http 401",
            "http 403",
            "[error]",
        )
    )


def gopay_event_name(line: str) -> str:
    match = re.match(r"^\[[^\]]+\]\s+([a-z][a-z0-9_.-]+):", str(line or ""))
    return match.group(1) if match else ""


def emit_account_event(marker: str, **payload: Any) -> None:
    log(marker + json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def run_account(
    method_id: str,
    index: int,
    token: str,
    proxies: list[str],
    run_dir: Path,
) -> dict[str, Any]:
    method = METHODS[method_id]
    account_hash = hashlib.sha256(token.encode()).hexdigest()[:10]
    account_label = account_label_from_token(token)
    account_dir = run_dir / f"account_{index:06d}_{account_hash}"
    account_dir.mkdir(parents=True, exist_ok=True)
    proxy_file = account_dir / "proxy_seeds.txt"
    proxy_file.write_text("\n".join(proxies) + "\n", encoding="utf-8")
    os.chmod(proxy_file, 0o600)
    env = child_environment(method_id, token, proxy_file, account_dir / "proxy_state.json")
    if method_id == "gopay":
        env["GOPAY_DATA_DIR"] = str(account_dir)
    if method_id == "gcash":
        env["PAYMENT_BATCH_CHECKOUT_GATE"] = str(run_dir / "gcash_checkout.lock")
    process: subprocess.Popen[str] | None = None
    output_lines: list[str] = []
    emit_account_event(
        ACCOUNT_START_MARKER,
        index=index,
        account=account_label,
    )
    try:
        if STOP_EVENT.is_set():
            return {
                "index": index,
                "account": account_label,
                "status": "stopped",
                "url": "",
                "detail": "任务已停止",
                "exit_code": -15,
            }
        process = subprocess.Popen(
            [sys.executable, "-u", str(method["script"])],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with CHILDREN_LOCK:
            CHILDREN.add(process)
        if process.stdout is not None:
            for raw_line in process.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                output_lines.append(line)
                if method_id == "gopay" and gopay_event_name(line) in GOPAY_ACCOUNT_EVENTS:
                    emit_account_event(
                        ACCOUNT_LOG_MARKER,
                        index=index,
                        account=account_label,
                        line=line,
                    )
                elif should_forward(line):
                    log(f"[PAYMENT_BATCH {method_id} #{index} {account_label}] {line}")
            process.stdout.close()
        exit_code = process.wait()
    except Exception as exc:
        output_lines.append(f"{type(exc).__name__}: {exc}")
        exit_code = -1
    finally:
        if process is not None:
            with CHILDREN_LOCK:
                CHILDREN.discard(process)
        try:
            proxy_file.unlink(missing_ok=True)
        except OSError:
            pass

    output = "\n".join(output_lines)
    success, url = extract_result(output, method)
    qr_payload = extract_gcash_qr_payload(output) if method_id == "gcash" else ""
    forced_failure: tuple[str, str] | None = None
    if method_id == "gopay" and success and "oaics_" in url.lower():
        success = False
        url = ""
        forced_failure = ("oaics_rejected", "已拒绝 oaics_ Checkout")
    elif method_id == "gopay" and success and not is_gopay_authorize_url(url):
        success = False
        url = ""
        forced_failure = ("invalid_gopay_redirect", "GoPay 返回的授权链接无效")
    if success:
        status, detail = "success", str(method["detail"])
    elif forced_failure is not None:
        status, detail = forced_failure
    else:
        status, detail = classify_failure(output, exit_code)
    return {
        "index": index,
        "account": account_label,
        "status": status,
        "url": url,
        "qr_payload": qr_payload,
        "detail": detail,
        "exit_code": exit_code,
    }


def stop_children(_signum: int, _frame: Any) -> None:
    STOP_EVENT.set()
    with CHILDREN_LOCK:
        children = list(CHILDREN)
    for process in children:
        if process.poll() is None:
            process.terminate()


def env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default)).strip()))
    except ValueError:
        return max(minimum, default)


def env_float(name: str, default: float, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        value = default
    if value != value or value in (float("inf"), float("-inf")):
        value = default
    return max(minimum, value)


def main() -> int:
    method_id = os.environ.get("PAYMENT_BATCH_METHOD", "").strip().lower()
    method = METHODS.get(method_id)
    if method is None:
        log("[PAYMENT_BATCH][ERROR] 批量支付方式不受支持")
        return 2
    token_file = Path(os.environ.get("PAYMENT_BATCH_TOKEN_FILE", "")).expanduser()
    proxy_file = Path(os.environ.get("PAYMENT_BATCH_PROXY_FILE", "")).expanduser()
    if not token_file.is_file():
        log("[PAYMENT_BATCH][ERROR] 批量 Token 文件不存在")
        return 2
    if not proxy_file.is_file():
        log("[PAYMENT_BATCH][ERROR] 代理池不存在")
        return 2
    tokens = parse_tokens(token_file.read_text(encoding="utf-8", errors="ignore"))
    proxies = [
        line.strip()
        for line in proxy_file.read_text(encoding="utf-8", errors="ignore").splitlines()
        if line.strip()
    ]
    if not tokens:
        log("[PAYMENT_BATCH][ERROR] 未解析到有效 Access Token")
        return 2
    if not proxies:
        log("[PAYMENT_BATCH][ERROR] 代理池为空")
        return 2

    requested_concurrency = env_int("PAYMENT_BATCH_CONCURRENCY", 0)
    workers = len(tokens) if requested_concurrency == 0 else min(requested_concurrency, len(tokens))
    start_interval = env_float(
        "PAYMENT_BATCH_START_INTERVAL",
        1.0 if method_id in {"gcash", "gopay"} else 0.0,
    )
    root = Path(
        os.environ.get("PAYMENT_BATCH_RUN_ROOT", str(token_file.parent / "batch_runs"))
    ).expanduser()
    run_dir = root / f"{method_id}_{time.strftime('%Y%m%d-%H%M%S')}_{os.getpid()}"
    run_dir.mkdir(parents=True, exist_ok=True)
    buckets = assign_proxy_buckets(proxies, len(tokens))
    log(
        f"[PAYMENT_BATCH] 方式={method_id}，账号={len(tokens)}，代理={len(proxies)}，并发="
        f"{'全部(' + str(workers) + ')' if requested_concurrency == 0 else workers}，"
        f"启动间隔={start_interval:.2f}s，"
        f"首次Checkout锁={'启用' if method_id == 'gcash' and fcntl is not None else '关闭'}"
    )

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"{method_id}-batch") as executor:
        futures: dict[Any, int] = {}
        for index, token in enumerate(tokens, 1):
            if index > 1 and start_interval > 0:
                if STOP_EVENT.wait(start_interval):
                    log("收到停止信号，不再启动新的批量账号", "[WARN] ")
                    break
            future = executor.submit(
                run_account,
                method_id,
                index,
                token,
                buckets[index - 1],
                run_dir,
            )
            futures[future] = index
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "index": futures[future],
                    "account": "unknown",
                    "status": "failed",
                    "url": "",
                    "detail": f"调度异常: {type(exc).__name__}",
                    "exit_code": -1,
                }
            results.append(result)
            log("[BATCH_RESULT] " + json.dumps(result, ensure_ascii=False, separators=(",", ":")))

    results.sort(key=lambda item: int(item["index"]))
    result_file = token_file.parent / f"{method_id}_batch_results.json"
    result_file.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    success = sum(item["status"] == "success" for item in results)
    stopped = sum(item["status"] == "stopped" for item in results)
    log(
        f"[PAYMENT_BATCH] 完成：成功={success}，"
        f"失败={len(results) - success - stopped}，停止={stopped}"
    )
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, stop_children)
    signal.signal(signal.SIGINT, stop_children)
    raise SystemExit(main())
