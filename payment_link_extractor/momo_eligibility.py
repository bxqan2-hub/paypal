from __future__ import annotations

"""Standalone VN MoMo trial-eligibility probe.

The probe is deliberately separate from Checkout: it can rotate a VN proxy
pool and only the first eligible proxy is allowed to create an ``oaics_*``
session.
"""

from dataclasses import replace
import threading
from typing import Any, Callable

from .auth import normalize_access_token
from .config import normalize_payment_method
from .errors import ConfigurationError, ExtractionCancelled, ProtocolError
from .momo_transport import MomoTransportFactory, close, momo_request_headers


MOMO_TRIAL_COUPON = "plus-1-month-free"
MOMO_ELIGIBILITY_PATH = "/backend-api/promo_campaign/check_coupon"


class MomoEligibilityError(ProtocolError):
    """Eligibility failure that occurred before Checkout creation."""

    def __init__(self, status_code: int, message: str, *, retryable: bool = True) -> None:
        super().__init__(status_code, message)
        self.retryable = retryable


def _proxy_candidates(config: Any) -> tuple[str, ...]:
    values = getattr(config, "proxy_pool", ()) or getattr(
        config, "checkout_proxy_attempts", ()
    ) or (getattr(config, "checkout_proxy", ""),)
    return tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))


def _attempt_config(config: Any, proxy: str) -> Any:
    return replace(
        config,
        checkout_proxy=proxy,
        update_proxy=proxy,
        checkout_proxy_attempts=(proxy,),
        update_proxy_attempts=(proxy,),
        proxy_pool=(proxy,),
    )


def probe_momo_trial_eligibility(
    config: Any,
    *,
    transport_factory: Any = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Check the VN trial coupon before creating a Checkout session."""
    if normalize_payment_method(str(getattr(config, "payment_method", ""))) != "momo":
        raise ConfigurationError("Momo eligibility probe requires payment_method=momo")
    if str(getattr(config, "country", "")).strip().upper() != "VN":
        raise ConfigurationError("Momo eligibility probe requires country=VN")
    token = normalize_access_token(str(getattr(config, "access_token", "") or ""))
    if not token:
        raise ConfigurationError("AT is required")
    proxies = _proxy_candidates(config)
    if not proxies:
        raise ConfigurationError("VN checkout proxy is required for Momo eligibility")

    factory = transport_factory or MomoTransportFactory()
    probes: list[dict[str, Any]] = []
    for index, proxy in enumerate(proxies, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(f"eligibility_proxy:{index}")
        attempt = _attempt_config(replace(config, access_token=token), proxy)
        chatgpt = factory.chatgpt(attempt, proxy)
        url = (
            f"https://chatgpt.com{MOMO_ELIGIBILITY_PATH}"
            f"?coupon={MOMO_TRIAL_COUPON}&is_coupon_from_query_param=true"
        )
        try:
            response = chatgpt.request(
                "GET",
                url,
                headers=momo_request_headers(
                    chatgpt,
                    "GET",
                    url,
                    {
                        "Accept": "application/json",
                        "Referer": "https://chatgpt.com/?promo_campaign=plus-1-month-free",
                        "x-openai-target-path": MOMO_ELIGIBILITY_PATH,
                        "x-openai-target-route": MOMO_ELIGIBILITY_PATH,
                    },
                ),
                timeout=30,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            if status >= 400:
                probes.append(
                    {
                        "attempt": index,
                        "proxy_slot": index,
                        "http_status": status,
                        "state": "",
                        "eligible": False,
                        "failure_mode": "access_token_invalid" if status == 401 else "eligibility_http_error",
                    }
                )
                if status == 401:
                    raise MomoEligibilityError(
                        status,
                        "Momo eligibility authentication failed (HTTP 401)",
                        retryable=False,
                    )
                continue
            try:
                payload = response.json() or {}
            except Exception as exc:
                probes.append(
                    {
                        "attempt": index,
                        "proxy_slot": index,
                        "http_status": status,
                        "state": "",
                        "eligible": False,
                        "failure_mode": "invalid_json",
                    }
                )
                continue
            if not isinstance(payload, dict):
                state = ""
                eligible = False
            else:
                state = str(payload.get("state") or "").strip().lower()
                eligible = state == "eligible" or bool(payload.get("eligible") is True)
            probes.append(
                {
                    "attempt": index,
                    "proxy_slot": index,
                    "http_status": status,
                    "state": state,
                    "eligible": eligible,
                    "coupon": MOMO_TRIAL_COUPON,
                }
            )
            if eligible:
                if stage_callback is not None:
                    stage_callback("eligibility_confirmed")
                return {
                    "ok": True,
                    "eligible": True,
                    "state": state,
                    "coupon": MOMO_TRIAL_COUPON,
                    "country": "VN",
                    "currency": "VND",
                    "attempt": index,
                    "max_attempts": len(proxies),
                    "proxy_slot": index,
                    "proxy": proxy,
                    "source": "chatgpt_check_coupon",
                    "probes": probes,
                }
        except MomoEligibilityError:
            raise
        except Exception as exc:
            probes.append(
                {
                    "attempt": index,
                    "proxy_slot": index,
                    "http_status": 0,
                    "state": "",
                    "eligible": False,
                    "failure_mode": type(exc).__name__,
                }
            )
        finally:
            close(chatgpt)

    raise MomoEligibilityError(
        409,
        "Momo trial eligibility rejected on all VN proxies",
        retryable=True,
    )
