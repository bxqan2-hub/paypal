from __future__ import annotations

"""Direct bridge to the complete vendored MK-GCash-Link-OpenSource project.

The upstream project is copied unchanged under ``mk_gcash_open_source``. This
module only converts this site's task configuration into the upstream app.py
payload and converts the upstream task snapshot back into this site's result
model; it does not reimplement or merge the GCash checkout chain.
"""

import asyncio
import importlib.util
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from curl_cffi import CurlOpt

from .errors import ExtractionCancelled, ProtocolError
from .models import BillingProfile, ExtractionConfig, PaymentLinkResult


MK_GCASH_SOURCE_COMMIT = "2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5"
MK_GCASH_PROJECT_DIR = Path(__file__).resolve().parent / "mk_gcash_open_source"
MK_GCASH_APP_PATH = MK_GCASH_PROJECT_DIR / "app.py"

_STAGE_MAP = {
    "proxy_test": "eligibility_check",
    "create_checkout": "checkout",
    "configure_taxes": "taxes",
    "confirm_payment": "payment_confirmation",
    "start_payment": "payment_confirmation",
    "follow_redirect": "redirect_resolution",
}
_APP_LOCK = threading.RLock()
_UPSTREAM_APP: ModuleType | None = None


def _load_upstream_app() -> ModuleType:
    """Load the copied upstream app.py, with its project directory first."""
    global _UPSTREAM_APP
    with _APP_LOCK:
        if _UPSTREAM_APP is not None:
            return _UPSTREAM_APP
        if not MK_GCASH_APP_PATH.is_file():
            raise ProtocolError(500, f"GCash 开源项目缺失: {MK_GCASH_APP_PATH}")
        project_path = str(MK_GCASH_PROJECT_DIR)
        if project_path not in sys.path:
            sys.path.insert(0, project_path)
        # The copied app imports these upstream top-level names. Remove a stale
        # module with the same name so the import comes from the copied project.
        for name in ("gcash_chain", "payment_monitor", "sentinel"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("_mk_gcash_open_source_app", MK_GCASH_APP_PATH)
        if spec is None or spec.loader is None:
            raise ProtocolError(500, "GCash 开源 app.py 无法加载")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _install_connect_timeout_hook()
        _UPSTREAM_APP = module
        return module


def _connect_timeout_ms() -> int:
    try:
        import os

        return max(3_000, min(int(os.getenv("MK_GCASH_CONNECT_TIMEOUT_MS", "8000")), 15_000))
    except (TypeError, ValueError):
        return 8_000


def _install_connect_timeout_hook() -> None:
    """Cap only dead-proxy TCP connect time without changing vendored source."""
    chain_module = sys.modules.get("gcash_chain")
    chain_type = getattr(chain_module, "GCashChain", None)
    if chain_type is None or getattr(chain_type, "_site_timeout_hook", False):
        return
    original_init = chain_type.__init__

    def hooked_init(self: Any, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        session = getattr(self, "_session", None)
        options = getattr(session, "curl_options", None)
        if isinstance(options, dict):
            options[CurlOpt.CONNECTTIMEOUT_MS] = _connect_timeout_ms()

    chain_type.__init__ = hooked_init
    chain_type._site_timeout_hook = True


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _max_attempts(config: ExtractionConfig) -> int:
    return max(1, min(10, _as_int(config.retry_count) + 1))


def _proxy_pool(config: ExtractionConfig) -> list[str]:
    values = config.proxy_pool or config.checkout_proxy_attempts or (config.checkout_proxy,)
    return list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _payload(config: ExtractionConfig) -> dict[str, Any]:
    pool = _proxy_pool(config)
    if not pool:
        raise ProtocolError(400, "GCash 提链需要至少一个代理")
    return {
        "accounts": [{
            "access_token": config.access_token,
            "email": str(config.account_email or "").strip(),
            "name": str(config.account_name or "").strip(),
        }],
        "proxy_pool": pool,
        "max_attempts": _max_attempts(config),
    }


def _raw_task(app: ModuleType, job_id: str, client_id: str) -> dict[str, Any]:
    for task in app.CHAIN_MANAGER.get_tasks(job_id):
        if task.get("client_account_id") == client_id:
            return task
    return {}


def _retry_decision(result: dict[str, Any]) -> tuple[bool, str]:
    try:
        from gcash_chain import _retry_decision as upstream_retry_decision

        return upstream_retry_decision(result)
    except Exception:
        return False, ""


def prewarm_mk_gcash_runtime(timeout: float = 20.0) -> str:
    """Start the copied project's shared browser before the first task."""
    _load_upstream_app()
    monitor = sys.modules.get("payment_monitor")
    if monitor is None:
        raise ProtocolError(500, "GCash 开源支付监控模块未加载")
    monitor.manager._ensure_loop()
    future = asyncio.run_coroutine_threadsafe(monitor.manager._get_browser(), monitor.manager._loop)
    browser = future.result(timeout=max(1.0, float(timeout)))
    return str(browser.version)


def _billing(email: str, name: str) -> BillingProfile:
    return BillingProfile(
        name=name or "GCash User",
        email=email,
        phone="",
        country="PH",
        line1="",
        city="",
        state="",
        postal_code="",
    )


def extract_mk_gcash_payment_link(
    config: ExtractionConfig,
    *,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Call the copied project's app.py create_job directly and await its task."""
    app = _load_upstream_app()
    started_at = time.perf_counter()
    stage_offsets_ms: dict[str, int] = {}
    last_stage = ""
    payload = _payload(config)
    try:
        snapshot = app.create_job(payload)
    except ValueError as exc:
        raise ProtocolError(400, str(exc)) from exc
    job_id = str(snapshot.get("job_id") or "")
    account = (snapshot.get("accounts") or [{}])[0]
    client_id = str(account.get("id") or "")
    if not job_id or not client_id:
        raise ProtocolError(502, "GCash 开源项目未返回任务标识")

    while True:
        if cancel_event is not None and cancel_event.is_set():
            try:
                app.cancel_job(job_id)
            finally:
                raise ExtractionCancelled("task cancellation requested")
        try:
            current = app.public_job(job_id)
        except KeyError as exc:
            raise ProtocolError(502, "GCash 开源项目任务已丢失") from exc
        account = next((item for item in current.get("accounts", []) if item.get("id") == client_id), {})
        raw = _raw_task(app, job_id, client_id)
        upstream_stage = str(account.get("current_step") or raw.get("current_step") or "")
        if upstream_stage and upstream_stage != last_stage:
            last_stage = upstream_stage
            stage_offsets_ms[upstream_stage] = round((time.perf_counter() - started_at) * 1000)
            stage = _STAGE_MAP.get(upstream_stage)
            if stage_callback is not None and stage:
                stage_callback(stage)

        status = str(account.get("status") or raw.get("status") or "")
        if status in {"success", "failed"}:
            break
        time.sleep(0.15)

    if status != "success":
        detail = str(account.get("error") or raw.get("error_message") or "GCash extraction failed")
        if (cancel_event is not None and cancel_event.is_set()) or "TASK_CANCELLED" in detail:
            raise ExtractionCancelled("task cancellation requested")
        retryable, retry_reason = _retry_decision({
            "status": status,
            "current_step": upstream_stage,
            "error_message": detail,
        })
        error = ProtocolError(502, detail)
        error.mk_retryable = bool(retryable)  # type: ignore[attr-defined]
        error.mk_retry_reason = str(retry_reason or "")  # type: ignore[attr-defined]
        error.mk_stage_offsets_ms = dict(stage_offsets_ms)  # type: ignore[attr-defined]
        error.mk_elapsed_ms = round((time.perf_counter() - started_at) * 1000)  # type: ignore[attr-defined]
        raise error

    gcash_url = str(account.get("link") or raw.get("gcash_url") or "")
    if not gcash_url:
        raise ProtocolError(502, "GCash 开源项目返回成功但没有 gcash_url")
    email = str(account.get("email") or payload["accounts"][0]["email"] or "")
    name = str(account.get("name") or payload["accounts"][0]["name"] or "")
    if stage_callback is not None:
        stage_callback("completed")
    amount_minor = _as_int(raw.get("checkout_amount"))
    return PaymentLinkResult(
        checkout_session_id=str(raw.get("checkout_session_id") or raw.get("cid") or ""),
        session_kind="openai_custom_checkout",
        payment_method="gcash",
        billing_country="PH",
        currency="PHP",
        amount_due=amount_minor / 100,
        amount_due_minor=amount_minor,
        billing=_billing(email, name),
        account_email=email,
        payment_method_id=str(raw.get("payment_method_id") or raw.get("cpmt") or ""),
        stripe_redirect_url=str(raw.get("adyen_url") or ""),
        provider_url=gcash_url,
        provider_field="gcash_url",
        provider_value=gcash_url,
        extra={
            "mk_gcash_source_commit": MK_GCASH_SOURCE_COMMIT,
            "mk_gcash_project_dir": str(MK_GCASH_PROJECT_DIR),
            "payment_route": str(raw.get("payment_route") or ""),
            "qr_text": str(raw.get("qr_text") or ""),
            "qr_short": str(raw.get("qr_short") or ""),
            "net_auth_id": str(raw.get("net_auth_id") or ""),
            "qr_expires_at": raw.get("expires_at") or raw.get("qr_expires_at"),
            "monitor_id": str(raw.get("monitor_id") or ""),
            "callback_status": str(raw.get("callback_status") or ""),
            "stage_offsets_ms": stage_offsets_ms,
            "total_elapsed_ms": round((time.perf_counter() - started_at) * 1000),
            "max_attempts": _max_attempts(config),
        },
    )
