from __future__ import annotations

from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import queue
import random
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from ..auth import account_email, normalize_access_token
from ..application import extract_payment_link
from ..errors import ExtractionCancelled, NetworkError
from ..logging_utils import log_context
from ..models import ExtractionConfig, PaymentLinkResult
from .events import EVENT_HISTORY_SIZE, make_event, redact_text, utc_timestamp


TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})
STAGE_PROGRESS = {
    "queued": 0,
    "running": 5,
    "retrying": 0,
    "eligibility_check": 10,
    "eligibility_confirmed": 12,
    "checkout": 15,
    "checkout_update": 25,
    "promotion_applied": 30,
    "stripe_init": 35,
    "elements_session": 50,
    "taxes": 65,
    "payment_confirmation": 80,
    "redirect_resolution": 95,
    "zero_amount_validation": 97,
    "zero_amount_confirmed": 99,
    "completed": 100,
}


class TaskNotFoundError(KeyError):
    pass


class TaskStateError(RuntimeError):
    pass


def _has_nonzero_amount(result: dict[str, Any] | None) -> bool:
    if not isinstance(result, dict):
        return False
    value = result.get("amount_due_minor", result.get("amount_due"))
    if value is None or value == "":
        return False
    try:
        amount = Decimal(str(value))
        return amount.is_finite() and amount != 0
    except (InvalidOperation, TypeError, ValueError):
        return False


@dataclass
class TaskRecord:
    task_id: str
    config: ExtractionConfig
    cancel_event: threading.Event = field(default_factory=threading.Event)
    future: Future[Any] | None = None
    status: str = "queued"
    stage: str = "queued"
    progress: int = 0
    created_at: str = field(default_factory=utc_timestamp)
    started_at: str | None = None
    finished_at: str | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    network_error: bool = False
    account_email: str = ""
    session_kind: str | None = None
    retry_of: str | None = None
    attempt: int = 0


class TaskManager:
    """Thread-backed in-memory task manager with one global event stream."""

    def __init__(
        self,
        extractor: Callable[..., PaymentLinkResult] = extract_payment_link,
        *,
        max_workers: int = 2,
        concurrency: int | None = None,
        ttl_seconds: int = 3600,
        history_size: int = EVENT_HISTORY_SIZE,
    ) -> None:
        self._extractor = extractor
        self._capacity = max(1, max_workers)
        self._concurrency = max(
            1, min(self._capacity, concurrency if concurrency is not None else self._capacity)
        )
        self._active_slots = 0
        self._executor = ThreadPoolExecutor(max_workers=self._capacity, thread_name_prefix="payment-task")
        self._ttl = max(1, ttl_seconds)
        self._lock = threading.RLock()
        self._tasks: dict[str, TaskRecord] = {}
        self._history: deque[dict[str, Any]] = deque(maxlen=max(1, history_size))
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()

    @property
    def concurrency(self) -> int:
        with self._lock:
            return self._concurrency

    @property
    def max_concurrency(self) -> int:
        return self._capacity

    def set_concurrency(self, value: int) -> int:
        normalized = max(1, min(self._capacity, int(value)))
        with self._lock:
            self._concurrency = normalized
            self._publish_locked(
                "",
                "task.concurrency",
                {
                    "concurrency": self._concurrency,
                    "max_concurrency": self._capacity,
                    "active_slots": self._active_slots,
                },
            )
        return normalized

    def concurrency_snapshot(self) -> dict[str, int]:
        with self._lock:
            return {
                "concurrency": self._concurrency,
                "max_concurrency": self._capacity,
                "active_slots": self._active_slots,
            }

    def close(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def create(self, config: ExtractionConfig) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            return self._create_locked(config)

    def retry(
        self,
        task_id: str,
        *,
        checkout_proxy: str | None = None,
        update_proxy: str | None = None,
        retry_count: int | None = None,
        checkout_proxy_attempts: tuple[str, ...] | None = None,
        update_proxy_attempts: tuple[str, ...] | None = None,
        proxy_pool: tuple[str, ...] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            if record.status == "succeeded":
                if not _has_nonzero_amount(record.result):
                    raise TaskStateError("only succeeded tasks with a non-zero amount can be retried")
            elif record.status not in {"failed", "cancelled"}:
                raise TaskStateError("only failed, cancelled, or non-zero succeeded tasks can be retried")
            retry_config = record.config
            if checkout_proxy is not None:
                proxy = str(checkout_proxy).strip()
                if not proxy:
                    raise TaskStateError("checkout proxy is required for retry")
                retry_config = replace(record.config, checkout_proxy=proxy)
            if update_proxy is not None:
                proxy = str(update_proxy).strip()
                if not proxy:
                    raise TaskStateError("update proxy is required for retry")
                retry_config = replace(retry_config, update_proxy=proxy)
            if retry_count is not None:
                normalized_retry_count = int(retry_count)
                if normalized_retry_count < 0 or normalized_retry_count > 10:
                    raise TaskStateError("retry count must be between 0 and 10")
                retry_config = replace(retry_config, retry_count=normalized_retry_count)
            if proxy_pool is not None:
                retry_config = replace(retry_config, proxy_pool=proxy_pool)
            total_attempts = self._total_attempts(retry_config)
            if proxy_pool is not None:
                unified_attempts = self._fit_proxy_attempts(
                    retry_config.checkout_proxy,
                    proxy_pool,
                    total_attempts,
                )
                retry_config = replace(
                    retry_config,
                    checkout_proxy=unified_attempts[0],
                    update_proxy=unified_attempts[0],
                    checkout_proxy_attempts=unified_attempts,
                    update_proxy_attempts=unified_attempts,
                    proxy_pool=proxy_pool,
                )
                checkout_attempts = unified_attempts
                update_attempts = unified_attempts
            else:
                checkout_attempts = self._fit_proxy_attempts(
                    retry_config.checkout_proxy,
                    checkout_proxy_attempts
                    if checkout_proxy_attempts is not None
                    else retry_config.checkout_proxy_attempts,
                    total_attempts,
                )
                update_attempts = self._fit_proxy_attempts(
                    retry_config.update_proxy,
                    update_proxy_attempts
                    if update_proxy_attempts is not None
                    else retry_config.update_proxy_attempts,
                    total_attempts,
                )
            retry_config = replace(
                retry_config,
                checkout_proxy_attempts=checkout_attempts,
                update_proxy_attempts=update_attempts,
            )
            self._tasks.pop(task_id, None)
            self._publish_locked(task_id, "task.deleted", {"status": record.status, "reason": "retry"})
            return self._create_locked(retry_config, retry_of=task_id)

    def _create_locked(self, config: ExtractionConfig, *, retry_of: str | None = None) -> dict[str, Any]:
        task_id = uuid.uuid4().hex
        record = TaskRecord(
            task_id=task_id,
            config=config,
            account_email=account_email(normalize_access_token(config.access_token)),
            retry_of=retry_of,
        )
        self._tasks[task_id] = record
        total_attempts = self._total_attempts(record.config)
        created_data: dict[str, Any] = {
            "status": "queued",
            "account_email": record.account_email,
            "payment_method": record.config.payment_method,
            "billing_country": record.config.country,
            "progress": record.progress,
            "attempt": 1,
            "retry_count": total_attempts - 1,
            "max_attempts": total_attempts,
        }
        if retry_of:
            created_data["retry_of"] = retry_of
        self._publish_locked(task_id, "task.created", created_data)
        log_context(component="task", task_id=task_id).info("task queued")
        record.future = self._executor.submit(self._run, task_id)
        return self._snapshot_locked(record)

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            self._cleanup_locked()
            record = self._tasks.get(task_id)
            return self._snapshot_locked(record) if record else None

    def list(self) -> list[dict[str, Any]]:
        """Return all non-expired task snapshots without exposing task config."""
        with self._lock:
            self._cleanup_locked()
            records = sorted(
                self._tasks.values(),
                key=lambda record: record.created_at,
                reverse=True,
            )
            return [self._snapshot_locked(record) for record in records]

    def resolve_paypal(self, task_id: str) -> dict[str, Any]:
        """Resolve an already-successful Stripe redirect into a strict BA URL."""
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            if record.status != "succeeded" or not isinstance(record.result, dict):
                raise TaskStateError("only succeeded PayPal tasks can be resolved")
            if record.config.payment_method != "paypal":
                raise TaskStateError("task is not PayPal")
            source_url = str(
                record.result.get("stripe_redirect_url")
                or record.result.get("provider_url")
                or record.result.get("paypal_url")
                or ""
            ).strip()
            config = record.config

        from ..stripe_common import is_paypal_ba_approval_url, resolve_external_redirect
        from ..transport import DefaultTransportFactory, safe_close

        stripe = DefaultTransportFactory().stripe(config)
        try:
            final_url = resolve_external_redirect(
                stripe,
                source_url,
                preferred_hosts=("paypal.com",),
                max_hops=8,
            )
        finally:
            safe_close(stripe)
        if not is_paypal_ba_approval_url(final_url):
            raise TaskStateError("PayPal BA 链仍未解析成功，请更换代理后重试")

        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or not isinstance(record.result, dict):
                raise TaskNotFoundError(task_id)
            record.result["provider_url"] = final_url
            record.result["paypal_url"] = final_url
            self._publish_locked(
                task_id,
                "task.succeeded",
                {"status": record.status, "result": record.result, "progress": record.progress},
            )
            return self._snapshot_locked(record)

    def cancel(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            if record.status in TERMINAL_STATES:
                raise TaskStateError(f"task is already {record.status}")
            record.cancel_event.set()
            log_context(component="task", task_id=task_id).info("task cancellation requested")
            if record.status == "queued":
                if record.future is not None:
                    record.future.cancel()
                record.status = "cancelled"
                record.stage = "cancelled"
                record.finished_at = utc_timestamp()
                self._publish_locked(
                    task_id,
                    "task.cancelled",
                    {"status": record.status, "progress": record.progress},
                )
            elif record.status == "running":
                record.status = "cancel_requested"
                self._publish_locked(task_id, "task.cancel_requested", {"status": record.status})
            return self._snapshot_locked(record)

    def delete(self, task_id: str) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            record = self._tasks.get(task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            if record.status not in {"succeeded", "failed", "cancelled"}:
                raise TaskStateError("only succeeded, failed, or cancelled tasks can be deleted")
            status = record.status
            self._tasks.pop(task_id, None)
            self._publish_locked(task_id, "task.deleted", {"status": status})
            log_context(component="task", task_id=task_id).info("task deleted")
            return {"ok": True, "task_id": task_id, "status": "deleted"}

    def delete_by_statuses(self, statuses: set[str]) -> dict[str, Any]:
        with self._lock:
            self._cleanup_locked()
            records = [record for record in self._tasks.values() if record.status in statuses]
            task_ids = []
            for record in records:
                self._tasks.pop(record.task_id, None)
                task_ids.append(record.task_id)
                self._publish_locked(
                    record.task_id,
                    "task.deleted",
                    {"status": record.status, "reason": "bulk"},
                )
                log_context(component="task", task_id=record.task_id).info("task deleted in bulk")
            return {"ok": True, "deleted_count": len(task_ids), "task_ids": task_ids}

    def subscribe(self) -> tuple[list[dict[str, Any]], queue.Queue[dict[str, Any]]]:
        subscriber: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=256)
        with self._lock:
            self._cleanup_locked()
            active_task_ids = set(self._tasks)
            terminal_task_ids = {
                task_id
                for task_id, record in self._tasks.items()
                if record.status in TERMINAL_STATES
            }
            history = [
                event
                for event in self._history
                if (
                    not event.get("task_id")
                    or event["task_id"] in active_task_ids
                    and (
                        event["task_id"] not in terminal_task_ids
                        or event["type"] in {"task.succeeded", "task.failed", "task.cancelled"}
                    )
                )
            ]
            self._subscribers.add(subscriber)
        return history, subscriber

    def unsubscribe(self, subscriber: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def _acquire_slot(self, task_id: str) -> bool:
        while True:
            with self._lock:
                record = self._tasks.get(task_id)
                if record is None or record.status == "cancelled":
                    return False
                if self._active_slots < self._concurrency:
                    self._active_slots += 1
                    return True
            if record.cancel_event.wait(0.1):
                return False

    def _release_slot(self) -> None:
        with self._lock:
            self._active_slots = max(0, self._active_slots - 1)

    def _run(self, task_id: str) -> None:
        if not self._acquire_slot(task_id):
            return
        try:
            self._run_with_slot(task_id)
        finally:
            self._release_slot()

    @staticmethod
    def _fit_proxy_attempts(
        primary: str,
        candidates: tuple[str, ...],
        total_attempts: int,
    ) -> tuple[str, ...]:
        values = [str(value).strip() for value in candidates if str(value).strip()]
        primary = str(primary or "").strip()
        if primary and (not values or values[0] != primary):
            values.insert(0, primary)
        if not values:
            return ()
        seed = tuple(values)
        while len(values) < total_attempts:
            values.append(seed[len(values) % len(seed)])
        return tuple(values[:total_attempts])

    @classmethod
    def _config_for_attempt(
        cls,
        config: ExtractionConfig,
        attempt_index: int,
        *,
        proxy_plan: tuple[str, ...] | None = None,
    ) -> ExtractionConfig:
        total_attempts = cls._total_attempts(config)
        if config.proxy_pool:
            attempts = proxy_plan or cls._fit_proxy_attempts(
                config.checkout_proxy, config.proxy_pool, total_attempts
            )
            proxy = attempts[attempt_index] if attempts else config.checkout_proxy
            return replace(
                config,
                checkout_proxy=proxy,
                update_proxy=proxy,
                checkout_proxy_attempts=attempts,
                update_proxy_attempts=attempts,
                proxy_pool=attempts,
            )
        checkout_attempts = cls._fit_proxy_attempts(
            config.checkout_proxy, config.checkout_proxy_attempts, total_attempts
        )
        update_attempts = cls._fit_proxy_attempts(
            config.update_proxy, config.update_proxy_attempts, total_attempts
        )
        checkout_proxy = checkout_attempts[attempt_index] if checkout_attempts else config.checkout_proxy
        update_proxy = update_attempts[attempt_index] if update_attempts else config.update_proxy
        return replace(
            config,
            checkout_proxy=checkout_proxy,
            update_proxy=update_proxy,
            checkout_proxy_attempts=checkout_attempts,
            update_proxy_attempts=update_attempts,
        )

    @staticmethod
    def _total_attempts(config: ExtractionConfig) -> int:
        """Return the channel-specific full-attempt budget.

        GoPay treats a supplied proxy pool as an exhaustive, without-
        replacement attempt set. Other channels retain the existing bounded
        retry_count contract.
        """
        if config.payment_method == "gopay" and config.proxy_pool:
            unique = {
                str(item).strip() for item in config.proxy_pool if str(item).strip()
            }
            if unique:
                return len(unique)
        return max(1, min(11, int(config.retry_count) + 1))

    @staticmethod
    def _random_proxy_plan(
        proxy_pool: tuple[str, ...], total_attempts: int
    ) -> tuple[str, ...]:
        """Choose a fresh randomized proxy order for one account task.

        A task keeps the selected proxy for its complete attempt (eligibility,
        Checkout, Stripe and provider). Retries start a new attempt with a
        new randomized export entry; entries are not repeated until the pool
        is exhausted.
        """
        values = list(dict.fromkeys(str(item).strip() for item in proxy_pool if str(item).strip()))
        if not values:
            return ()
        rng = random.SystemRandom()
        plan: list[str] = []
        while len(plan) < max(1, int(total_attempts)):
            cycle = list(values)
            rng.shuffle(cycle)
            if plan and len(values) > 1 and cycle[0] == plan[-1]:
                cycle[0], cycle[1] = cycle[1], cycle[0]
            plan.extend(cycle)
        return tuple(plan[: max(1, int(total_attempts))])

    def _run_with_slot(self, task_id: str) -> None:
        task_log = log_context(component="task", task_id=task_id)
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.status == "cancelled":
                return
            retry_plan = record.config
            total_attempts = self._total_attempts(retry_plan)
            retry_count = total_attempts - 1
            proxy_plan = (
                self._random_proxy_plan(retry_plan.proxy_pool, total_attempts)
                if retry_plan.proxy_pool
                else None
            )

        for attempt_index in range(total_attempts):
            with self._lock:
                record = self._tasks.get(task_id)
                if record is None or record.status == "cancelled":
                    return
                if record.cancel_event.is_set() or record.status == "cancel_requested":
                    self._finish_cancelled_locked(record)
                    return
                record.config = self._config_for_attempt(
                    retry_plan, attempt_index, proxy_plan=proxy_plan
                )
                record.attempt = attempt_index + 1
                record.status = "running"
                record.stage = "running"
                record.progress = STAGE_PROGRESS[record.stage]
                record.started_at = record.started_at or utc_timestamp()
                record.finished_at = None
                record.result = None
                record.error = None
                record.network_error = False
                record.session_kind = None
                self._publish_locked(
                    task_id,
                    "task.started",
                    {
                        "status": record.status,
                        "progress": record.progress,
                        "attempt": record.attempt,
                        "max_attempts": total_attempts,
                    },
                )
                self._publish_locked(
                    task_id,
                    "task.log",
                    {"message": f"full extraction attempt {record.attempt}/{total_attempts} started"},
                )
                task_log.info("full extraction attempt {}/{} started", record.attempt, total_attempts)

            try:
                attempt_started = time.perf_counter()
                result = self._extractor(
                    record.config,
                    cancel_event=record.cancel_event,
                    stage_callback=lambda stage: self._stage(task_id, stage),
                )
            except ExtractionCancelled as exc:
                with self._lock:
                    record = self._tasks.get(task_id)
                    if record is not None:
                        self._finish_cancelled_locked(record, str(exc))
                return
            except Exception as exc:
                with self._lock:
                    record = self._tasks.get(task_id)
                    if record is None:
                        return
                    if record.cancel_event.is_set() or record.status == "cancel_requested":
                        self._finish_cancelled_locked(record, str(exc))
                        return
                    error = redact_text(exc, self._secrets(record.config))
                    elapsed_ms = round((time.perf_counter() - attempt_started) * 1000)
                    mk_retryable = bool(getattr(exc, "mk_retryable", False))
                    is_gopay = record.config.payment_method == "gopay"
                    explicit_retryable = getattr(exc, "retryable", None)
                    status_code = getattr(exc, "status_code", None)
                    try:
                        status_code = int(status_code) if status_code is not None else None
                    except (TypeError, ValueError):
                        status_code = None
                    failure_mode = str(
                        getattr(exc, "failure_mode", "") or type(exc).__name__
                    )
                    may_retry = (
                        attempt_index < retry_count
                        and (not is_gopay or explicit_retryable is not False)
                        and not (is_gopay and status_code == 401)
                        and (
                            record.config.payment_method != "gcash"
                            or mk_retryable
                        )
                    )
                    if may_retry:
                        record.status = "running"
                        record.stage = "retrying"
                        record.progress = STAGE_PROGRESS[record.stage]
                        record.error = None
                        self._publish_locked(
                            task_id,
                            "task.retrying",
                            {
                                "status": record.status,
                                "stage": record.stage,
                                "progress": record.progress,
                                "error": error,
                                "attempt": record.attempt,
                                "next_attempt": record.attempt + 1,
                                "max_attempts": total_attempts,
                                "ip_rotated": True,
                                **(
                                    {"failure_mode": failure_mode, "retryable": True}
                                    if is_gopay
                                    else {}
                                ),
                                "elapsed_ms": elapsed_ms,
                            },
                        )
                        task_log.warning(
                            "full extraction attempt {}/{} failed; restarting from the beginning with the next proxy IP: {}",
                            record.attempt,
                            total_attempts,
                            f"{error} elapsed_ms={elapsed_ms}",
                        )
                        continue
                    record.status = "failed"
                    record.stage = "failed"
                    record.error = error
                    record.network_error = isinstance(exc, NetworkError)
                    record.finished_at = utc_timestamp()
                    self._publish_locked(
                        task_id,
                        "task.failed",
                        {
                            "status": record.status,
                            "error": record.error,
                            "network_error": record.network_error,
                            **(
                                {
                                    "failure_mode": failure_mode,
                                    "retryable": bool(explicit_retryable),
                                }
                                if is_gopay
                                else {}
                            ),
                            "progress": record.progress,
                            "attempt": record.attempt,
                            "max_attempts": total_attempts,
                            "elapsed_ms": elapsed_ms,
                        },
                    )
                    task_log.error(
                        "task failed after {}/{} attempts elapsed_ms={}: {}",
                        record.attempt,
                        total_attempts,
                        elapsed_ms,
                        record.error,
                    )
                return

            with self._lock:
                record = self._tasks.get(task_id)
                if record is None:
                    return
                if record.cancel_event.is_set() or record.status == "cancel_requested":
                    self._finish_cancelled_locked(record)
                    return
                record.status = "succeeded"
                record.stage = "completed"
                record.progress = STAGE_PROGRESS[record.stage]
                record.result = result.to_dict() if hasattr(result, "to_dict") else dict(result)
                record.finished_at = utc_timestamp()
                self._publish_locked(
                    task_id,
                    "task.succeeded",
                    {
                        "status": record.status,
                        "result": record.result,
                        "checkout_proxy": record.config.checkout_proxy,
                        "progress": record.progress,
                        "attempt": record.attempt,
                        "max_attempts": total_attempts,
                    },
                )
                task_log.info(
                    "task succeeded on full extraction attempt {}/{} elapsed_ms={}",
                    record.attempt,
                    total_attempts,
                    round((time.perf_counter() - attempt_started) * 1000),
                )
            return

    def _stage(self, task_id: str, stage: str) -> None:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None or record.status in TERMINAL_STATES:
                return
            if str(stage).startswith("checkout_kind:"):
                record.session_kind = str(stage).split(":", 1)[1]
                self._publish_locked(
                    task_id,
                    "task.checkout_detected",
                    {
                        "session_kind": record.session_kind,
                        "status": record.status,
                        "progress": record.progress,
                    },
                )
                return
            record.stage = str(stage)
            record.progress = STAGE_PROGRESS.get(record.stage, record.progress)
            self._publish_locked(
                task_id,
                "task.stage",
                {
                    "stage": record.stage,
                    "status": record.status,
                    "progress": record.progress,
                },
            )
            log_context(component="task", task_id=task_id, stage=record.stage).info(
                "task stage: {} progress={}", record.stage, record.progress
            )

    def _finish_cancelled_locked(self, record: TaskRecord, detail: str = "") -> None:
        record.status = "cancelled"
        record.stage = "cancelled"
        record.error = redact_text(detail) if detail else None
        record.finished_at = utc_timestamp()
        self._publish_locked(
            record.task_id,
            "task.cancelled",
            {"status": record.status, "progress": record.progress},
        )
        log_context(component="task", task_id=record.task_id).info("task cancelled")

    def _publish_locked(self, task_id: str, event_type: str, data: dict[str, Any]) -> None:
        event = make_event(task_id, event_type, data)
        self._history.append(event)
        for subscriber in tuple(self._subscribers):
            try:
                subscriber.put_nowait(event)
            except queue.Full:
                # Preserve terminal events; transient logs/stages may be dropped.
                if event_type in {"task.succeeded", "task.failed", "task.cancelled"}:
                    try:
                        subscriber.get_nowait()
                        subscriber.put_nowait(event)
                    except queue.Empty:
                        pass

    def _snapshot_locked(self, record: TaskRecord) -> dict[str, Any]:
        total_attempts = self._total_attempts(record.config)
        snapshot: dict[str, Any] = {
            "ok": True,
            "task_id": record.task_id,
            "status": record.status,
            "stage": record.stage,
            "progress": record.progress,
            "created_at": record.created_at,
            "started_at": record.started_at,
            "finished_at": record.finished_at,
            "account_email": record.account_email,
            "payment_method": record.config.payment_method,
            "billing_country": record.config.country,
            "attempt": max(1, record.attempt),
            "retry_count": total_attempts - 1,
            "max_attempts": total_attempts,
        }
        if record.session_kind:
            snapshot["session_kind"] = record.session_kind
        if record.retry_of:
            snapshot["retry_of"] = record.retry_of
        if record.status == "succeeded":
            snapshot["checkout_proxy"] = record.config.checkout_proxy
        if record.result is not None:
            snapshot["result"] = record.result
        if record.error:
            snapshot["error"] = record.error
        if record.status == "failed":
            snapshot["network_error"] = record.network_error
        return snapshot

    def _cleanup_locked(self) -> None:
        cutoff = datetime_now() - timedelta(seconds=self._ttl)
        expired = []
        for task_id, record in self._tasks.items():
            if record.status in TERMINAL_STATES and record.finished_at:
                try:
                    finished = datetime.fromisoformat(record.finished_at.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if finished < cutoff:
                    expired.append(task_id)
        for task_id in expired:
            self._tasks.pop(task_id, None)

    @staticmethod
    def _secrets(config: ExtractionConfig) -> tuple[str, ...]:
        return (
            config.access_token,
            config.checkout_proxy,
            config.update_proxy,
            config.stripe_hcaptcha_token,
            *config.checkout_proxy_attempts,
            *config.update_proxy_attempts,
        )


def datetime_now() -> datetime:
    return datetime.now(timezone.utc)
