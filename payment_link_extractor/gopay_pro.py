from __future__ import annotations

"""Adapter for the isolated GoPay Pro core derived from the GCash framework."""

import importlib.util
import inspect
import sys
import threading
import time
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .auth import account_email
from .config import billing_for_country, currency_minor_scale
from .errors import ExtractionCancelled, ProtocolError
from .models import BillingProfile, ExtractionConfig, PaymentLinkResult
from .upstream_contract import verify_upstream_project


GOPAY_PRO_SOURCE_COMMIT = "derived-from-mk-gcash-2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5"
GOPAY_PRO_PROJECT_DIR = Path(__file__).resolve().parent / "gopay_pro_core"
GOPAY_PRO_APP_PATH = GOPAY_PRO_PROJECT_DIR / "app.py"
GOPAY_PRO_MANIFEST_PATH = Path(__file__).resolve().parents[1] / "gopay_pro_project_manifest.json"
_LOCK = threading.RLock()
_APP: ModuleType | None = None


def _load_gopay_pro_app() -> ModuleType:
    global _APP
    with _LOCK:
        if _APP is not None:
            return _APP
        if not GOPAY_PRO_APP_PATH.is_file():
            raise ProtocolError(500, f"GoPay Pro 核心缺失: {GOPAY_PRO_APP_PATH}")
        verify_upstream_project(
            project_dir=GOPAY_PRO_PROJECT_DIR,
            manifest_path=GOPAY_PRO_MANIFEST_PATH,
            expected_commit=GOPAY_PRO_SOURCE_COMMIT,
            provider="GoPay Pro",
        )
        project = str(GOPAY_PRO_PROJECT_DIR)
        if project not in sys.path:
            sys.path.insert(0, project)
        for name in ("gopay_pro_chain", "gopay_pro_monitor", "gopay_pro_sentinel"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("_gopay_pro_core_app", GOPAY_PRO_APP_PATH)
        if spec is None or spec.loader is None:
            raise ProtocolError(500, "GoPay Pro 核心无法加载")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        for name, expected in {
            "create_job": ("payload",),
            "public_job": ("job_id",),
            "cancel_job": ("job_id",),
        }.items():
            fn = getattr(module, name, None)
            if not callable(fn) or tuple(inspect.signature(fn).parameters)[: len(expected)] != expected:
                raise ProtocolError(500, f"GoPay Pro 核心调用契约不匹配: {name}")
        manager = getattr(module, "CHAIN_MANAGER", None)
        if not callable(getattr(manager, "get_tasks", None)):
            raise ProtocolError(500, "GoPay Pro 核心缺少 CHAIN_MANAGER.get_tasks")
        _APP = module
        return module


def _payload(config: ExtractionConfig) -> dict[str, Any]:
    pool = list(config.proxy_pool or config.checkout_proxy_attempts or (config.checkout_proxy,))
    pool = list(dict.fromkeys(str(item).strip() for item in pool if str(item).strip()))
    if not pool:
        raise ProtocolError(400, "GoPay Pro 提链需要至少一个代理")
    return {
        "accounts": [{
            "access_token": config.access_token,
            "email": str(config.account_email or "").strip(),
            "name": str(config.account_name or "").strip(),
        }],
        "proxy_pool": pool,
        "max_attempts": max(1, min(10, int(config.retry_count) + 1)),
    }


def _billing(config: ExtractionConfig) -> BillingProfile:
    profile = billing_for_country("ID")
    email = str(config.account_email or account_email(config.access_token) or profile.email).strip()
    name = str(config.account_name or profile.name).strip()
    return BillingProfile(
        name=name,
        email=email,
        phone=profile.phone,
        country="ID",
        line1=profile.line1,
        city=profile.city,
        state=profile.state,
        postal_code=profile.postal_code,
    )


def _raw_task(app: ModuleType, job_id: str, client_id: str) -> dict[str, Any]:
    for task in app.CHAIN_MANAGER.get_tasks(job_id):
        if task.get("client_account_id") == client_id:
            return task
    return {}


def _valid_url(app: ModuleType, value: str) -> bool:
    chain = sys.modules.get("gopay_pro_chain")
    validator = getattr(chain, "_is_gopay_pro_url", None)
    return bool(callable(validator) and validator(value))


def extract_gopay_pro_payment_link(
    config: ExtractionConfig,
    *,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    app = _load_gopay_pro_app()
    payload = _payload(config)
    try:
        snapshot = app.create_job(payload)
    except ValueError as exc:
        raise ProtocolError(400, str(exc)) from exc
    job_id = str(snapshot.get("job_id") or "")
    account = (snapshot.get("accounts") or [{}])[0]
    client_id = str(account.get("id") or "")
    if not job_id or not client_id:
        raise ProtocolError(502, "GoPay Pro 核心未返回任务标识")
    last_step = ""
    while True:
        if cancel_event is not None and cancel_event.is_set():
            app.cancel_job(job_id)
            raise ExtractionCancelled("task cancellation requested")
        current = app.public_job(job_id)
        account = next((item for item in current.get("accounts", []) if item.get("id") == client_id), {})
        raw = _raw_task(app, job_id, client_id)
        step = str(account.get("current_step") or raw.get("current_step") or "")
        if step and step != last_step:
            last_step = step
            if stage_callback is not None:
                stage_callback({
                    "proxy_test": "eligibility_check",
                    "create_checkout": "checkout",
                    "configure_taxes": "taxes",
                    "confirm_payment": "payment_confirmation",
                    "start_payment": "payment_confirmation",
                    "follow_redirect": "redirect_resolution",
                }.get(step, step))
        status = str(account.get("status") or raw.get("status") or "")
        if status in {"success", "failed"}:
            break
        time.sleep(0.15)
    if status != "success":
        raise ProtocolError(502, str(account.get("error") or raw.get("error_message") or "GoPay Pro extraction failed"))
    url = str(account.get("link") or raw.get("gopay_pro_url") or "").strip()
    if not url:
        raise ProtocolError(502, "GoPay Pro 核心成功但没有授权链接")
    if not _valid_url(app, url):
        raise ProtocolError(502, "GoPay Pro 核心返回了不符合契约的授权链接")
    billing = _billing(config)
    amount_minor = int(raw.get("checkout_amount") or 0)
    if stage_callback is not None:
        stage_callback("completed")
    return PaymentLinkResult(
        checkout_session_id=str(raw.get("checkout_session_id") or raw.get("cid") or ""),
        session_kind="gopay_pro_custom_checkout",
        payment_method="gopay_pro",
        billing_country="ID",
        currency="IDR",
        amount_due=amount_minor / (10 ** currency_minor_scale("IDR")),
        amount_due_minor=amount_minor,
        billing=billing,
        account_email=billing.email,
        payment_method_id=str(raw.get("payment_method_id") or raw.get("cpmt") or ""),
        provider_url=url,
        provider_field="gopay_pro_url",
        provider_value=url,
        extra={
            "gopay_pro_source_commit": GOPAY_PRO_SOURCE_COMMIT,
            "gopay_pro_project_dir": str(GOPAY_PRO_PROJECT_DIR),
            "payment_route": str(raw.get("payment_route") or ""),
            "qr_text": str(raw.get("qr_text") or ""),
            "qr_short": str(raw.get("qr_short") or ""),
            "qr_expires_at": raw.get("expires_at") or raw.get("qr_expires_at"),
            "monitor_id": str(raw.get("monitor_id") or ""),
            "callback_status": str(raw.get("callback_status") or ""),
            "max_attempts": max(1, min(10, int(config.retry_count) + 1)),
        },
    )
