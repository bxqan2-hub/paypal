"""Mount the upstream PayPal agreement protocol inside the existing Flask app.

The upstream protocol package is copied unchanged under ``paypal_agreement_protocol``.
This module is only an HTTP adapter: it translates a Flask request into the
upstream ``WebHandler`` contract and translates the captured response back into
Werkzeug's response object.  No protocol flow code is changed here.
"""
from __future__ import annotations

import importlib
import io
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any

from flask import Response, jsonify, request

from paypal_agreement_protocol.herosms import (
    HeroSMSClient,
    HeroSMSError,
    HeroSMSNoNumbersError,
)


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROTOCOL_ROOT = _PROJECT_ROOT / "paypal_agreement_protocol"
if str(_PROTOCOL_ROOT) not in sys.path:
    # The upstream source intentionally keeps its ``config`` and ``paypal``
    # imports absolute.  Put only the copied integration directory on the
    # import path; the extraction package remains untouched.
    sys.path.insert(0, str(_PROTOCOL_ROOT))

_protocol = importlib.import_module("paypal_agreement_protocol.web")
_SMS_CLIENT: HeroSMSClient | None = None
_SMS_WATCHERS: dict[str, threading.Thread] = {}
_SMS_WATCHERS_LOCK = threading.RLock()
_SMS_RESERVATIONS_LOCK = threading.RLock()
_SMS_RESERVATIONS_BY_PHONE: dict[str, dict[str, Any]] = {}
_SMS_RESERVATIONS_BY_ACTIVATION: dict[str, str] = {}
_SMS_CANCELLATIONS_LOCK = threading.RLock()
_SMS_CANCELLATIONS: dict[str, dict[str, Any]] = {}
SMS_ROTATION_MAX_ATTEMPTS = 3


def _bounded_seconds_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)) or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


SMS_ROTATION_WAIT_SECONDS = _bounded_seconds_env(
    "HEROSMS_ROTATION_WAIT_SECONDS", 120.0, 30.0, 300.0,
)
SMS_UNIQUE_ACQUIRE_ATTEMPTS = 5
SMS_RESERVATION_TTL_SECONDS = 60.0 * 60.0
SMS_CANCEL_MIN_AGE_SECONDS = _bounded_seconds_env(
    "HEROSMS_CANCEL_MIN_AGE_SECONDS", 120.0, 60.0, 300.0,
)
SMS_CANCEL_RETRY_SECONDS = _bounded_seconds_env(
    "HEROSMS_CANCEL_RETRY_SECONDS", 15.0, 2.0, 60.0,
)
SMS_CANCEL_RETRY_WINDOW_SECONDS = _bounded_seconds_env(
    "HEROSMS_CANCEL_RETRY_WINDOW_SECONDS", 600.0, 120.0, 1800.0,
)
SMS_REPLACEMENT_RETRY_SECONDS = _bounded_seconds_env(
    "HEROSMS_REPLACEMENT_RETRY_SECONDS", 5.0, 0.0, 60.0,
)
SMS_TERMINAL_STATUSES = {
    "STATUS_CANCEL", "STATUS_CANCELLED", "STATUS_FINISH", "6", "8",
}


def _parse_sms_max_price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_price must be numeric") from exc
    if not math.isfinite(price) or price < 0:
        raise ValueError("max_price must be a finite non-negative number")
    return round(price, 2)


def _sms_client() -> HeroSMSClient:
    global _SMS_CLIENT
    if _SMS_CLIENT is None:
        _SMS_CLIENT = HeroSMSClient()
    return _SMS_CLIENT


def _sms_phone_key(value: Any) -> str:
    return "".join(char for char in str(value or "") if char.isdigit())


def _prune_sms_reservations_locked(now: float | None = None) -> None:
    current = time.monotonic() if now is None else now
    stale_ids = [
        str(item.get("activation_id") or "")
        for item in _SMS_RESERVATIONS_BY_PHONE.values()
        if current - float(item.get("touched_at") or item.get("created_at") or current)
        > SMS_RESERVATION_TTL_SECONDS
    ]
    for activation_id in stale_ids:
        phone_key = _SMS_RESERVATIONS_BY_ACTIVATION.pop(activation_id, "")
        if phone_key:
            _SMS_RESERVATIONS_BY_PHONE.pop(phone_key, None)


def _reserve_new_sms_activation(activation: dict[str, Any], owner: str = "pending") -> bool:
    activation_id = str(activation.get("activation_id") or "").strip()
    phone_key = _sms_phone_key(activation.get("phone"))
    if not activation_id or not phone_key:
        return False
    with _SMS_RESERVATIONS_LOCK:
        _prune_sms_reservations_locked()
        if activation_id in _SMS_RESERVATIONS_BY_ACTIVATION or phone_key in _SMS_RESERVATIONS_BY_PHONE:
            return False
        now = time.monotonic()
        _SMS_RESERVATIONS_BY_PHONE[phone_key] = {
            "activation_id": activation_id,
            "owner": str(owner or "pending"),
            "created_at": now,
            "touched_at": now,
        }
        _SMS_RESERVATIONS_BY_ACTIVATION[activation_id] = phone_key
        return True


def _claim_sms_activation(activation: dict[str, Any], owner: str) -> bool:
    """Bind a pending activation to a job without permitting phone reuse."""
    activation_id = str(activation.get("activation_id") or "").strip()
    phone_key = _sms_phone_key(activation.get("phone"))
    if not activation_id or not phone_key:
        return False
    with _SMS_RESERVATIONS_LOCK:
        _prune_sms_reservations_locked()
        registered_phone = _SMS_RESERVATIONS_BY_ACTIVATION.get(activation_id)
        if registered_phone:
            if registered_phone != phone_key:
                return False
            reservation = _SMS_RESERVATIONS_BY_PHONE.get(phone_key)
            if not reservation:
                return False
            existing_owner = str(reservation.get("owner") or "pending")
            requested_owner = str(owner or "pending")
            if existing_owner not in {"pending", requested_owner}:
                return False
            reservation["owner"] = requested_owner
            reservation["touched_at"] = time.monotonic()
            return True
        if phone_key in _SMS_RESERVATIONS_BY_PHONE:
            return False
        now = time.monotonic()
        _SMS_RESERVATIONS_BY_PHONE[phone_key] = {
            "activation_id": activation_id,
            "owner": str(owner or "pending"),
            "created_at": now,
            "touched_at": now,
        }
        _SMS_RESERVATIONS_BY_ACTIVATION[activation_id] = phone_key
        return True


def _release_sms_reservation(activation_id: Any) -> None:
    key = str(activation_id or "").strip()
    if not key:
        return
    with _SMS_RESERVATIONS_LOCK:
        phone_key = _SMS_RESERVATIONS_BY_ACTIVATION.pop(key, "")
        if phone_key:
            reservation = _SMS_RESERVATIONS_BY_PHONE.get(phone_key)
            if str((reservation or {}).get("activation_id") or "") == key:
                _SMS_RESERVATIONS_BY_PHONE.pop(phone_key, None)


def _sms_activation_created_at(activation_id: Any) -> float | None:
    key = str(activation_id or "").strip()
    if not key:
        return None
    with _SMS_RESERVATIONS_LOCK:
        phone_key = _SMS_RESERVATIONS_BY_ACTIVATION.get(key, "")
        reservation = _SMS_RESERVATIONS_BY_PHONE.get(phone_key) if phone_key else None
        if not reservation:
            return None
        try:
            return float(reservation.get("created_at"))
        except (TypeError, ValueError):
            return None


def _cancel_sms_activation_worker(
    client: HeroSMSClient,
    activation_id: str,
    not_before: float,
) -> None:
    """Cancel one HeroSMS activation once its two-minute hold has elapsed."""
    try:
        initial_delay = max(0.0, float(not_before) - time.monotonic())
        if initial_delay:
            time.sleep(initial_delay)
        deadline = time.monotonic() + SMS_CANCEL_RETRY_WINDOW_SECONDS
        while True:
            try:
                client.set_status(activation_id, 8)
                break
            except HeroSMSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(SMS_CANCEL_RETRY_SECONDS)
    finally:
        _release_sms_reservation(activation_id)
        with _SMS_CANCELLATIONS_LOCK:
            _SMS_CANCELLATIONS.pop(activation_id, None)


def _cancel_or_defer_sms_activation(
    client: HeroSMSClient,
    activation_id: Any,
) -> dict[str, Any]:
    """Cancel now when allowed, otherwise keep ownership and cancel in background."""
    key = str(activation_id or "").strip()
    if not key:
        return {"deferred": False, "scheduled_in_seconds": 0}
    now = time.monotonic()
    created_at = _sms_activation_created_at(key)
    not_before = (
        created_at + SMS_CANCEL_MIN_AGE_SECONDS
        if created_at is not None else now
    )
    if not_before <= now:
        try:
            client.set_status(key, 8)
            _release_sms_reservation(key)
            return {"deferred": False, "scheduled_in_seconds": 0}
        except HeroSMSError:
            not_before = now + SMS_CANCEL_RETRY_SECONDS
    scheduled_in = max(1, int(math.ceil(not_before - now)))
    with _SMS_CANCELLATIONS_LOCK:
        existing = _SMS_CANCELLATIONS.get(key)
        if existing:
            return {
                "deferred": True,
                "scheduled_in_seconds": max(
                    1,
                    int(math.ceil(float(existing.get("not_before") or not_before) - now)),
                ),
            }
        thread = threading.Thread(
            target=_cancel_sms_activation_worker,
            args=(client, key, not_before),
            name=f"herosms-cancel-{key}",
            daemon=True,
        )
        _SMS_CANCELLATIONS[key] = {
            "not_before": not_before,
            "thread": thread,
        }
        thread.start()
    return {"deferred": True, "scheduled_in_seconds": scheduled_in}


def _sms_activation_is_reserved(activation_id: Any) -> bool:
    key = str(activation_id or "").strip()
    with _SMS_RESERVATIONS_LOCK:
        _prune_sms_reservations_locked()
        return bool(key and key in _SMS_RESERVATIONS_BY_ACTIVATION)


def _acquire_unique_sms_number(
    client: HeroSMSClient,
    country: str,
    *,
    max_price: float | None = None,
    service: str | None = None,
    owner: str = "pending",
) -> dict[str, Any]:
    """Acquire one number that is not reserved by another account/task."""
    last_phone = ""
    for _ in range(SMS_UNIQUE_ACQUIRE_ATTEMPTS):
        activation = client.acquire_number(country, max_price=max_price, service=service)
        if _reserve_new_sms_activation(activation, owner=owner):
            return activation
        activation_id = str(activation.get("activation_id") or "").strip()
        last_phone = str(activation.get("phone") or "").strip()
        # A provider may return the already-active activation itself. Do not
        # cancel it because another account can still be waiting for its code.
        if activation_id and not _sms_activation_is_reserved(activation_id):
            client.finish(activation_id, 6)
    suffix = _sms_phone_key(last_phone)[-4:]
    detail = f" (尾号 {suffix})" if suffix else ""
    raise HeroSMSError(f"HeroSMS 连续返回已分配给其他账号的手机号{detail}，请稍后重试")


class _HeaderMap:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = {str(key).lower(): str(value) for key, value in values.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(str(key).lower(), default)


class _CaptureHandler(_protocol.WebHandler):
    """A socket-free WebHandler used by the Flask bridge."""

    def send_response(self, code: int, message: str | None = None) -> None:  # noqa: D401
        self._response_status = int(code)

    def send_header(self, keyword: str, value: Any) -> None:
        self._response_headers[str(keyword)] = str(value)

    def end_headers(self) -> None:
        return None

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.send_response(code)
        body = (message or "HTTP error").encode("utf-8")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def dispatch_protocol_request(inner_path: str, method: str, body: bytes = b"") -> Response:
    """Dispatch one ``/paypal-pay`` request without opening another port."""

    query = request.query_string.decode("utf-8", errors="replace")
    path = inner_path or "/"
    if query:
        path = f"{path}?{query}"
    headers = {key: value for key, value in request.headers.items()}
    headers.setdefault("Host", request.host)
    headers.setdefault("Content-Length", str(len(body)))
    headers.setdefault("Content-Type", "application/json" if body else "")

    handler = object.__new__(_CaptureHandler)
    handler.path = path
    handler.headers = _HeaderMap(headers)
    handler.client_address = (request.remote_addr or "127.0.0.1", 0)
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler._response_status = 200
    handler._response_headers = {}
    handler._set_device_cookie = ""

    if method.upper() == "GET":
        handler.do_GET()
    elif method.upper() == "POST":
        handler.do_POST()
    else:
        handler.send_error(405, "Method Not Allowed")

    payload = handler.wfile.getvalue()
    response_headers = dict(handler._response_headers)
    # WebHandler already emits a Content-Length.  Ensure Flask can return a
    # valid response even when a future upstream branch omits it.
    response_headers.setdefault("Content-Length", str(len(payload)))
    return Response(payload, status=handler._response_status, headers=response_headers)


def _watch_sms_job(job_id: str, activation: dict[str, Any]) -> None:
    """Wait for an OTP and rotate the HeroSMS number when it expires.

    The protocol flow already supports submitting a new phone while it is in
    ``awaiting_otp``.  Feeding the replacement number through ``submit_input``
    therefore keeps the upstream resend path unchanged while this adapter owns
    the bounded HeroSMS polling/cleanup loop.
    """
    client = _sms_client()
    current = dict(activation)
    attempt = 1
    submitted = False
    current_closed = False
    registered_activation_id = ""
    last_submitted_code = ""
    observed_retry_count = 0
    last_status_name = ""
    status_poll_failures = 0

    def add_log(job: Any, level: str, message: str) -> None:
        try:
            job.add_log(level, message)
        except Exception:
            return

    def emit_event(job: Any, event_type: str, data: dict[str, Any]) -> None:
        emitter = getattr(job, "emit_event", None)
        if callable(emitter):
            emitter(event_type, data)

    def mark_activation(job: Any, item: dict[str, Any], number: int) -> None:
        setter = getattr(job, "set_sms_activation", None)
        if callable(setter):
            setter(
                str(item.get("activation_id") or ""),
                str(item.get("phone") or ""),
                number,
                SMS_ROTATION_MAX_ATTEMPTS,
            )
        else:
            # Compatibility with an older in-memory job object during a hot
            # reload; the normal WebJob path always has set_sms_activation.
            with job._condition:
                if item.get("phone"):
                    job.phone = str(item["phone"])
                job.updated_at = time.time()

    try:
        while True:
            job = _protocol.get_job(job_id)
            if job is None or job.status in {"completed", "failed", "cancelled"}:
                break
            activation_id = str(current.get("activation_id") or "").strip()
            if not activation_id:
                add_log(job, "ERROR", "短信轮询缺少 HeroSMS activation_id，任务停止")
                break

            if registered_activation_id != activation_id:
                if not _claim_sms_activation(current, job_id):
                    add_log(job, "ERROR", "当前手机号已分配给其他账号，短信轮询已停止，请重新取号")
                    current_closed = True
                    try:
                        job.cancel()
                    except Exception:
                        pass
                    break
                mark_activation(job, current, attempt)
                registered_activation_id = activation_id
            if job.status != "awaiting_otp":
                # The flow may still be sending the SMS or may have accepted a
                # manual value.  Let it reach the next OTP wait before polling.
                time.sleep(min(1.0, client.poll_interval))
                continue

            retry_count = max(0, int(getattr(job, "retry_count", 0) or 0))
            retry_rotation = retry_count > observed_retry_count
            if retry_rotation:
                observed_retry_count = retry_count
                add_log(
                    job,
                    "WARNING",
                    f"自动重试 {retry_count}/{getattr(job, 'max_retries', 2)}：立即取消旧手机号并获取新手机号",
                )

            timed_out = True
            immediate_send_failure = False
            rotation_reason = "protocol_retry" if retry_rotation else "timeout"
            if not retry_rotation:
                deadline = time.monotonic() + SMS_ROTATION_WAIT_SECONDS
                while time.monotonic() < deadline:
                    job = _protocol.get_job(job_id)
                    if job is None or job.status in {"completed", "failed", "cancelled"}:
                        return
                    if job.status != "awaiting_otp":
                        timed_out = False
                        break
                    consume_failure = getattr(job, "consume_sms_send_failure", None)
                    send_failure = consume_failure() if callable(consume_failure) else ""
                    if send_failure:
                        immediate_send_failure = True
                        rotation_reason = "send_failed"
                        add_log(
                            job,
                            "WARNING",
                            f"PayPal 短信发送失败，立即取消当前手机号并换号；本次不计入 "
                            f"{SMS_ROTATION_MAX_ATTEMPTS} 次有效手机号超时：{send_failure}",
                        )
                        break
                    poll_started = time.monotonic()
                    try:
                        status = client.get_status(activation_id)
                    except HeroSMSError as exc:
                        status_poll_failures += 1
                        if status_poll_failures == 1 or status_poll_failures % 6 == 0:
                            add_log(
                                job,
                                "WARNING",
                                "HeroSMS 状态轮询暂时失败，后台继续重试；轮询故障时间不计入 "
                                f"{int(SMS_ROTATION_WAIT_SECONDS)} 秒等待和 "
                                f"{SMS_ROTATION_MAX_ATTEMPTS} 次有效超时：{exc}",
                            )
                        time.sleep(client.poll_interval)
                        # Provider/API downtime is not an SMS-code timeout.  Move
                        # the deadline forward by the failed request + backoff so
                        # a transient status outage cannot exhaust this phone or
                        # start a clean protocol retry before code polling works.
                        deadline += max(0.0, time.monotonic() - poll_started)
                        continue
                    if status_poll_failures:
                        add_log(job, "INFO", "HeroSMS 状态轮询已恢复，继续等待验证码")
                        status_poll_failures = 0
                    code = str(status.get("code") or "").strip()
                    status_name = str(status.get("status") or "").strip().upper()
                    if status_name and status_name != last_status_name:
                        last_status_name = status_name
                        emit_event(job, "herosms.status", {
                            "activation_id": activation_id,
                            "status": status_name,
                            "attempt": attempt,
                        })
                    if code and code.lower() not in {"none", "null"} and code != last_submitted_code:
                        try:
                            job.submit_input(code)
                        except ValueError:
                            timed_out = False
                            break
                        last_submitted_code = code
                        add_log(job, "SUCCESS", f"手机号轮询第 {attempt}/{SMS_ROTATION_MAX_ATTEMPTS} 次收到验证码，已自动提交")
                        submitted = True
                        emit_event(job, "herosms.code.received", {
                            "activation_id": activation_id,
                            "attempt": attempt,
                            "code_received": True,
                        })
                        timed_out = False
                        break
                    if status_name in SMS_TERMINAL_STATUSES:
                        rotation_reason = "provider_terminal"
                        add_log(job, "WARNING", f"HeroSMS 手机号第 {attempt}/{SMS_ROTATION_MAX_ATTEMPTS} 次已结束但没有验证码，准备换号")
                        break
                    time.sleep(client.poll_interval)

            if not timed_out:
                # The flow changed state (for example a manual OTP/phone was
                # submitted); do not allocate a competing replacement number.
                continue

            job = _protocol.get_job(job_id)
            if job is None or job.status in {"completed", "failed", "cancelled"}:
                break
            timeout_consumed = (
                not retry_rotation
                and not immediate_send_failure
                and rotation_reason == "timeout"
            )
            if retry_rotation:
                add_log(job, "WARNING", f"自动重试 {retry_count}/{getattr(job, 'max_retries', 2)}：已停止使用旧手机号")
            elif immediate_send_failure:
                add_log(
                    job,
                    "WARNING",
                    f"当前手机号发送失败，未等待超时且不占用第 {attempt}/{SMS_ROTATION_MAX_ATTEMPTS} "
                    "次有效超时名额，已停止使用旧号",
                )
            elif rotation_reason == "provider_terminal":
                add_log(
                    job,
                    "WARNING",
                    f"HeroSMS 当前手机号提前结束且未返回验证码，不占用第 {attempt}/{SMS_ROTATION_MAX_ATTEMPTS} "
                    "次有效超时名额，已停止使用旧号",
                )
            else:
                add_log(
                    job,
                    "WARNING",
                    f"已成功发送短信的手机号第 {attempt}/{SMS_ROTATION_MAX_ATTEMPTS} 次等待 "
                    f"{int(SMS_ROTATION_WAIT_SECONDS)} 秒仍未收到验证码，计入一次有效超时并停止使用旧号",
                )
            cancellation = _cancel_or_defer_sms_activation(client, activation_id)
            if cancellation["deferred"]:
                add_log(
                    job,
                    "INFO",
                    f"HeroSMS 旧号未达到可取消时间，后台将在约 {cancellation['scheduled_in_seconds']} 秒后取消并自动重试",
                )
            emit_event(job, "herosms.number.closed", {
                "activation_id": activation_id,
                "attempt": attempt,
                "reason": rotation_reason,
                "cancellation_deferred": cancellation["deferred"],
                "cancel_in_seconds": cancellation["scheduled_in_seconds"],
            })
            current_closed = True
            if retry_rotation:
                # The per-phone resend budget belongs to one protocol attempt.
                # A fresh protocol retry must always start with a fresh number
                # instead of inheriting an exhausted 3-number budget and
                # feeding "q" into wait_for_retry_phone().
                attempt = 1
            if timeout_consumed and attempt >= SMS_ROTATION_MAX_ATTEMPTS:
                retry_before = max(0, int(getattr(job, "retry_count", 0) or 0))
                add_log(
                    job,
                    "WARNING",
                    f"已达到最多 {SMS_ROTATION_MAX_ATTEMPTS} 次有效手机号超时，触发新的协议重试并继续换号",
                )
                try:
                    job.submit_input("q")
                except Exception:
                    break
                # ``q`` wakes the protocol input loop.  Keep this watcher alive
                # until run_job either enters its bounded clean-session retry or
                # makes the job terminal; otherwise wait_for_retry_phone has no
                # watcher left to supply the required fresh number and appears
                # permanently stuck.
                while True:
                    job = _protocol.get_job(job_id)
                    if job is None or job.status in {"completed", "failed", "cancelled"}:
                        return
                    if max(0, int(getattr(job, "retry_count", 0) or 0)) > retry_before:
                        break
                    time.sleep(min(0.1, client.poll_interval))
                continue

            next_attempt = (
                1
                if retry_rotation
                else (attempt + 1 if timeout_consumed else attempt)
            )
            replacement_failures = 0
            while True:
                job = _protocol.get_job(job_id)
                if job is None or job.status in {"completed", "failed", "cancelled"}:
                    return
                try:
                    replacement = _acquire_unique_sms_number(
                        client,
                        str(getattr(job, "country", "") or ""),
                        owner=job_id,
                    )
                    replacement_id = str(replacement.get("activation_id") or "").strip()
                    replacement_phone = str(replacement.get("phone") or "").strip()
                    if not replacement_id or not replacement_phone:
                        raise HeroSMSError("HeroSMS returned incomplete replacement data")
                    if replacement_failures:
                        add_log(
                            job,
                            "INFO",
                            f"HeroSMS 换号取号已恢复，成功获取第 {next_attempt}/{SMS_ROTATION_MAX_ATTEMPTS} 个手机号",
                        )
                    break
                except HeroSMSError as exc:
                    replacement_failures += 1
                    try:
                        retry_delay = float(
                            getattr(exc, "retry_after_seconds", SMS_REPLACEMENT_RETRY_SECONDS)
                            or SMS_REPLACEMENT_RETRY_SECONDS
                        )
                    except (TypeError, ValueError):
                        retry_delay = SMS_REPLACEMENT_RETRY_SECONDS
                    retry_delay = max(0.0, min(60.0, retry_delay))
                    if replacement_failures == 1 or replacement_failures % 6 == 0:
                        add_log(
                            job,
                            "WARNING",
                            f"HeroSMS 第 {next_attempt}/{SMS_ROTATION_MAX_ATTEMPTS} 个换号号码暂未取到，"
                            f"后台将在约 {retry_delay:g} 秒后继续取号；任务不会退出：{exc}",
                        )
                    emit_event(job, "herosms.number.retry_wait", {
                        "attempt": next_attempt,
                        "acquire_failures": replacement_failures,
                        "retry_in_seconds": retry_delay,
                    })
                    retry_deadline = time.monotonic() + retry_delay
                    while time.monotonic() < retry_deadline:
                        job = _protocol.get_job(job_id)
                        if job is None or job.status in {"completed", "failed", "cancelled"}:
                            return
                        time.sleep(min(0.5, retry_deadline - time.monotonic()))
                    continue
                except ValueError as exc:
                    add_log(
                        job,
                        "ERROR",
                        f"自动获取第 {next_attempt}/{SMS_ROTATION_MAX_ATTEMPTS} 个手机号参数无效：{exc}",
                    )
                    try:
                        job.submit_input("q")
                    except Exception:
                        pass
                    return
            current = dict(replacement)
            attempt = next_attempt
            last_status_name = ""
            last_submitted_code = ""
            status_poll_failures = 0
            submitted = False
            current_closed = False
            mark_activation(job, current, attempt)
            registered_activation_id = replacement_id
            if retry_rotation:
                add_log(job, "INFO", f"自动重试 {retry_count}/{getattr(job, 'max_retries', 2)}：已获取新手机号并开始新的协议尝试")
            else:
                add_log(job, "INFO", f"已自动获取第 {attempt}/{SMS_ROTATION_MAX_ATTEMPTS} 个手机号并重新发送验证码")
            try:
                # _confirm_phone_with_retry receives this value through its
                # existing new-phone branch and sends the next SMS itself.
                job.submit_input(replacement_phone)
            except Exception as exc:
                add_log(job, "ERROR", f"提交新手机号失败：{exc}")
                break
    finally:
        current_id = str(current.get("activation_id") or "").strip()
        if current_id and not current_closed:
            if submitted:
                client.finish(current_id, 6)
                _release_sms_reservation(current_id)
            else:
                _cancel_or_defer_sms_activation(client, current_id)
        with _SMS_WATCHERS_LOCK:
            _SMS_WATCHERS.pop(job_id, None)


def _start_sms_watchers(payload: dict[str, Any], activations: list[dict[str, Any]]) -> None:
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else [payload.get("job")]
    activation_by_index = {
        int(item.get("index") or position): item
        for position, item in enumerate(activations, start=1)
        if isinstance(item, dict) and str(item.get("activation_id") or "").strip()
    }
    for position, item in enumerate(jobs, start=1):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        activation = activation_by_index.get(int(item.get("batch_index") or position))
        if activation is None:
            continue
        job_id = str(item["id"])
        with _SMS_WATCHERS_LOCK:
            if job_id in _SMS_WATCHERS:
                continue
            thread = threading.Thread(target=_watch_sms_job, args=(job_id, dict(activation)), name=f"herosms-{job_id}", daemon=True)
            _SMS_WATCHERS[job_id] = thread
            thread.start()


def register_paypal_protocol(app: Any) -> None:
    """Register ``/paypal-pay`` and delegate all protocol work to the source."""

    @app.route("/paypal-pay", methods=["GET", "POST"])
    @app.route("/paypal-pay/", defaults={"protocol_path": ""}, methods=["GET", "POST"])
    @app.route("/paypal-pay/<path:protocol_path>", methods=["GET", "POST"])
    def paypal_protocol(protocol_path: str = "") -> Response:
        if protocol_path == "api/sms/number" and request.method == "POST":
            data = request.get_json(silent=True) or {}
            try:
                max_price = _parse_sms_max_price(data.get("max_price"))
                client = _sms_client()
                activation = _acquire_unique_sms_number(
                    client,
                    str(data.get("country") or ""),
                    max_price=max_price,
                    service=str(data.get("service") or "") or None,
                )
                returned_price = activation.get("price")
                if max_price is not None and returned_price not in (None, ""):
                    actual_price = float(returned_price)
                    if not math.isfinite(actual_price) or actual_price > max_price + 1e-9:
                        rejected_activation_id = str(activation.get("activation_id") or "")
                        client.finish(rejected_activation_id, 6)
                        _release_sms_reservation(rejected_activation_id)
                        return jsonify({
                            "ok": False,
                            "error": f"HeroSMS price {actual_price:.2f} exceeds max_price {max_price:.2f}",
                            "max_price": max_price,
                            "price": actual_price,
                        }), 422
                return jsonify({"ok": True, "max_price": max_price, **activation})
            except HeroSMSNoNumbersError as exc:
                return jsonify({
                    "ok": False,
                    "error": (
                        f"HeroSMS 当前地区暂时没有可用号码，已自动重试 {exc.attempts} 次，"
                        "请稍后再次取号"
                    ),
                    "code": exc.provider_code,
                    "retryable": exc.retryable,
                    "retry_after_seconds": exc.retry_after_seconds,
                    "attempts": exc.attempts,
                }), 503
            except (HeroSMSError, ValueError) as exc:
                return jsonify({"ok": False, "error": str(exc)}), 503
        if protocol_path == "api/sms/status" and request.method == "POST":
            data = request.get_json(silent=True) or {}
            raw_ids = data.get("activation_ids")
            if raw_ids is None:
                raw_ids = [data.get("activation_id")]
            if not isinstance(raw_ids, list):
                return jsonify({"ok": False, "error": "activation_ids must be a list"}), 400
            activation_ids = [str(value or "").strip() for value in raw_ids if str(value or "").strip()]
            if not activation_ids:
                return jsonify({"ok": False, "error": "activation_ids is required"}), 400
            if len(activation_ids) > 20:
                return jsonify({"ok": False, "error": "activation_ids supports at most 20 items"}), 400
            client = _sms_client()
            terminal_statuses = {"STATUS_CANCEL", "STATUS_CANCELLED", "STATUS_FINISH", "6", "8"}
            results = []
            for activation_id in activation_ids:
                try:
                    status = client.get_status(activation_id)
                    status_name = str(status.get("status") or "").strip().upper()
                    if status_name in terminal_statuses:
                        _release_sms_reservation(activation_id)
                    results.append({
                        "activation_id": activation_id,
                        "status": status_name,
                        "code": str(status.get("code") or "").strip(),
                        "active": status_name not in terminal_statuses,
                    })
                except HeroSMSError as exc:
                    results.append({
                        "activation_id": activation_id,
                        "status": "UNKNOWN",
                        "code": "",
                        "active": False,
                        "error": str(exc),
                    })
            return jsonify({"ok": True, "activations": results})
        if protocol_path == "api/sms/cancel" and request.method == "POST":
            data = request.get_json(silent=True) or {}
            activation_id = str(data.get("activation_id") or data.get("id") or "").strip()
            if not activation_id:
                return jsonify({"ok": False, "error": "activation_id is required"}), 400
            cancellation = _cancel_or_defer_sms_activation(_sms_client(), activation_id)
            return jsonify({
                "ok": True,
                "activation_id": activation_id,
                "status": 8,
                **cancellation,
            })
        inner_path = "/" + protocol_path if protocol_path else "/"
        body = request.get_data(cache=False)
        activations: list[dict[str, Any]] = []
        if protocol_path == "api/jobs" and request.method == "POST":
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
                raw_activations = parsed.get("sms_activations")
                if isinstance(raw_activations, list):
                    activations = [
                        {
                            "index": int(item.get("index") or index),
                            "activation_id": str(item.get("activation_id") or "").strip(),
                            "phone": str(item.get("phone") or "").strip(),
                            "country": str(item.get("country") or "").strip().upper(),
                        }
                        for index, item in enumerate(raw_activations, start=1)
                        if isinstance(item, dict) and str(item.get("activation_id") or "").strip()
                    ]
                activation_id = str(parsed.get("sms_activation_id") or "").strip()
                if activation_id and not activations:
                    activations = [{"index": 1, "activation_id": activation_id}]
            except (TypeError, ValueError, UnicodeDecodeError):
                activations = []
        response = dispatch_protocol_request(inner_path, request.method, body)
        if activations and response.status_code in {200, 201}:
            try:
                _start_sms_watchers(json.loads(response.get_data(as_text=True)), activations)
            except (ValueError, TypeError):
                pass
        return response
