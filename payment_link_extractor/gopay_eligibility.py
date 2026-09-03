from __future__ import annotations

"""Standalone GoPay-region Plus zero-trial eligibility detection."""

from dataclasses import replace
import random
import threading
from typing import Any, Callable

from .auth import decode_jwt_payload, normalize_access_token
from .config import normalize_payment_method
from .errors import ConfigurationError, ExtractionCancelled
from .gopay_checkout import PromoEligibilityError, probe_coupon_eligibility
from .gopay_transport import GoPayTransportFactory, TransportFactory, safe_close
from .logging_utils import stage_logger
from .models import ExtractionConfig


GOPAY_TRIAL_COUPON = "plus-1-month-free"


def _proxy_candidates(config: ExtractionConfig) -> tuple[str, ...]:
    values = config.proxy_pool or config.checkout_proxy_attempts or (config.checkout_proxy,)
    return tuple(
        dict.fromkeys(str(value).strip() for value in values if str(value).strip())
    )


def _randomized_proxy_plan(config: ExtractionConfig) -> tuple[str, ...]:
    values = list(_proxy_candidates(config))
    random.SystemRandom().shuffle(values)
    return tuple(values)


def _attempt_config(config: ExtractionConfig, proxy: str) -> ExtractionConfig:
    return replace(
        config,
        checkout_proxy=proxy,
        update_proxy=proxy,
        checkout_proxy_attempts=(proxy,),
        update_proxy_attempts=(proxy,),
        proxy_pool=(proxy,),
    )


def probe_gopay_zero_trial_eligibility(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Try ID proxies without replacement and stop at the first eligible result.

    This function performs only the coupon GET. It never creates Checkout,
    initializes Stripe, updates billing, confirms payment, or resolves a link.
    """
    if normalize_payment_method(config.payment_method) != "gopay":
        raise ConfigurationError("GoPay eligibility probe requires payment_method=gopay")
    if str(config.country or "").strip().upper() != "ID":
        raise ConfigurationError("GoPay eligibility probe requires country=ID")
    token = normalize_access_token(config.access_token)
    if not token:
        raise ConfigurationError("AT is required")
    if len(token.split(".")) == 3 and not decode_jwt_payload(token):
        raise ConfigurationError("AT payload is invalid")
    config = replace(config, access_token=token)
    proxies = _randomized_proxy_plan(config)
    if not proxies:
        raise ConfigurationError("checkout proxy is required")

    factory = transport_factory or GoPayTransportFactory()
    log = stage_logger(config.verbose)
    probes: list[dict[str, Any]] = []
    verified = False
    last_state = ""
    for index, proxy in enumerate(proxies, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(f"eligibility_proxy:{index}")
        attempt = _attempt_config(config, proxy)
        chatgpt = factory.chatgpt(attempt, proxy)
        try:
            provider = getattr(chatgpt, "openai_sentinel_provider", None)
            prepare = getattr(provider, "prepare", None)
            if callable(prepare):
                prepare()
            result = probe_coupon_eligibility(attempt, chatgpt, log)
            state = str(result.get("state") or "")
            eligible = bool(result.get("eligible"))
            verified = verified or bool(result.get("http_status"))
            last_state = state or last_state
            probes.append(
                {
                    "attempt": index,
                    "proxy_slot": index,
                    "http_status": int(result.get("http_status") or 0),
                    "state": state,
                    "eligible": eligible,
                    "coupon": GOPAY_TRIAL_COUPON,
                }
            )
            if eligible:
                if stage_callback is not None:
                    stage_callback("eligibility_confirmed")
                return {
                    "ok": True,
                    "eligible": True,
                    "state": state,
                    "coupon": GOPAY_TRIAL_COUPON,
                    "country": "ID",
                    "currency": "IDR",
                    "attempt": index,
                    "max_attempts": len(proxies),
                    "proxy_slot": index,
                    "source": "chatgpt_check_coupon",
                    "probes": probes,
                }
        except PromoEligibilityError as exc:
            if int(exc.status_code) == 401:
                raise
            probes.append(
                {
                    "attempt": index,
                    "proxy_slot": index,
                    "http_status": int(exc.status_code),
                    "state": "",
                    "eligible": False,
                    "coupon": GOPAY_TRIAL_COUPON,
                    "failure_mode": exc.failure_mode,
                }
            )
        except Exception as exc:
            probes.append(
                {
                    "attempt": index,
                    "proxy_slot": index,
                    "http_status": 0,
                    "state": "",
                    "eligible": False,
                    "coupon": GOPAY_TRIAL_COUPON,
                    "failure_mode": type(exc).__name__,
                }
            )
        finally:
            safe_close(chatgpt)

    return {
        "ok": verified,
        "eligible": False if verified else None,
        "state": last_state,
        "coupon": GOPAY_TRIAL_COUPON,
        "country": "ID",
        "currency": "IDR",
        "attempt": len(probes),
        "max_attempts": len(proxies),
        "proxy_slot": None,
        "source": "chatgpt_check_coupon",
        "probes": probes,
    }
