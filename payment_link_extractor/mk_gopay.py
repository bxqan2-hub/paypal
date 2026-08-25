from __future__ import annotations

"""Adapter for the vendored GoPay project.

The upstream protocol remains untouched.  Selecting GoPay in the workbench
enters this adapter, loads the copied ``gopay_extract.py`` module, and calls
its public ``run_gopay_flow`` function directly.  Only the task/result shape
is translated for the existing UI.
"""

import importlib.util
import sys
import threading
import types
from pathlib import Path
from types import ModuleType
from typing import Any, Callable

from .auth import account_email
from .errors import ExtractionCancelled, ProtocolError
from .models import BillingProfile, ExtractionConfig, PaymentLinkResult


MK_GOPAY_SOURCE_COMMIT = "3d2af69d848e6f292ef5abcb763c89dac3fbbea5"
MK_GOPAY_PROJECT_DIR = Path(__file__).resolve().parent / "mk_gopay_open_source"
MK_GOPAY_MODULE_PATH = MK_GOPAY_PROJECT_DIR / "gopay" / "gopay_extract.py"
_APP_LOCK = threading.RLock()
_UPSTREAM_GOPAY: ModuleType | None = None


def _install_sentinel_compat() -> None:
    """Provide the upstream project's optional nicepay import from this app."""
    try:
        __import__("nicepay.nicepay_link_extractor.kakao_extract")
        return
    except ImportError:
        pass
    from sentinel import mint_sentinel_sync

    package = types.ModuleType("nicepay")
    extractor_package = types.ModuleType("nicepay.nicepay_link_extractor")
    kakao = types.ModuleType("nicepay.nicepay_link_extractor.kakao_extract")

    def checkout_sentinel_headers(
        session: Any, device_id: str, profile: Any, *, flow: str
    ) -> dict[str, str]:
        proxies = getattr(session, "proxies", {}) or {}
        proxy = str(proxies.get("https") or proxies.get("http") or "")
        cookies = getattr(session, "cookies", None)
        try:
            cookie_header = "; ".join(f"{key}={value}" for key, value in cookies.items())
        except Exception:
            cookie_header = ""
        main, so = mint_sentinel_sync(
            flow=flow,
            device_id=device_id,
            user_agent=str(getattr(profile, "user_agent", "")),
            proxy=proxy,
            cores=int(getattr(profile, "cores", 8) or 8),
            page_url="https://chatgpt.com/",
            language=str(getattr(profile, "language", "en-US")),
            timezone=str(getattr(profile, "timezone_id", "America/New_York")),
            cookie_header=cookie_header,
        )
        result = {
            "OpenAI-Sentinel-Token": main,
            "OAI-Telemetry": "[1,null]",
        }
        if so:
            result["OpenAI-Sentinel-So-Token"] = so
        return result

    kakao.checkout_sentinel_headers = checkout_sentinel_headers  # type: ignore[attr-defined]
    sys.modules.setdefault("nicepay", package)
    sys.modules.setdefault("nicepay.nicepay_link_extractor", extractor_package)
    sys.modules.setdefault("nicepay.nicepay_link_extractor.kakao_extract", kakao)


def _load_upstream_gopay() -> ModuleType:
    global _UPSTREAM_GOPAY
    with _APP_LOCK:
        if _UPSTREAM_GOPAY is not None:
            return _UPSTREAM_GOPAY
        if not MK_GOPAY_MODULE_PATH.is_file():
            raise ProtocolError(500, f"GoPay 开源项目缺失: {MK_GOPAY_MODULE_PATH}")
        project_path = str(MK_GOPAY_PROJECT_DIR)
        if project_path not in sys.path:
            sys.path.insert(0, project_path)
        _install_sentinel_compat()
        spec = importlib.util.spec_from_file_location(
            "_mk_gopay_open_source_gopay_extract", MK_GOPAY_MODULE_PATH
        )
        if spec is None or spec.loader is None:
            raise ProtocolError(500, "GoPay 开源协议模块无法加载")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _UPSTREAM_GOPAY = module
        return module


def _billing(module: ModuleType, config: ExtractionConfig) -> BillingProfile:
    email = str(config.account_email or account_email(config.access_token) or "").strip()
    try:
        profile = module.indonesia_billing_profile(email)
    except Exception:
        profile = {
            "name": "GoPay User",
            "email": email,
            "line1": "Jl Merdeka 18",
            "city": "Bandung",
            "state": "Jawa Barat",
            "postal_code": "40111",
            "country": "ID",
        }
    return BillingProfile(
        name=str(profile.get("name") or "GoPay User"),
        email=str(profile.get("email") or email),
        phone=str(profile.get("phone") or ""),
        country="ID",
        line1=str(profile.get("line1") or ""),
        city=str(profile.get("city") or ""),
        state=str(profile.get("state") or ""),
        postal_code=str(profile.get("postal_code") or ""),
    )


def extract_mk_gopay_payment_link(
    config: ExtractionConfig,
    *,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Call the upstream GoPay core and map its redirect into the site model."""
    if cancel_event is not None and cancel_event.is_set():
        raise ExtractionCancelled("task cancellation requested")
    module = _load_upstream_gopay()
    billing = _billing(module, config)
    if stage_callback is not None:
        stage_callback("checkout")
    proxy = str(config.checkout_proxy or "").strip()
    try:
        redirect_url = module.run_gopay_flow(
            str(config.access_token),
            str(config.session_token or ""),
            proxy,
            billing.to_dict(),
        )
    except ExtractionCancelled:
        raise
    except Exception as exc:
        error = ProtocolError(502, f"GoPay 开源项目提链失败: {str(exc)[:300]}")
        error.mk_retryable = True  # type: ignore[attr-defined]
        raise error from exc
    if cancel_event is not None and cancel_event.is_set():
        raise ExtractionCancelled("task cancellation requested")
    url = str(redirect_url or "").strip()
    if not url:
        raise ProtocolError(502, "GoPay 开源项目返回成功但没有授权链接")
    if stage_callback is not None:
        stage_callback("redirect_resolution")
        stage_callback("completed")
    return PaymentLinkResult(
        checkout_session_id="",
        session_kind="gopay_custom_checkout",
        payment_method="gopay",
        billing_country="ID",
        currency="IDR",
        amount_due=0.0,
        amount_due_minor=0,
        billing=billing,
        account_email=billing.email,
        stripe_redirect_url=url,
        provider_url=url,
        provider_field="gopay_url",
        provider_value=url,
        extra={
            "mk_gopay_source_commit": MK_GOPAY_SOURCE_COMMIT,
            "mk_gopay_project_dir": str(MK_GOPAY_PROJECT_DIR),
            "payment_route": "gopay",
        },
    )
