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
        campaign_url = (
            "https://chatgpt.com/?promo_campaign=" + MOMO_TRIAL_COUPON
        )
        url = (
            f"https://chatgpt.com{MOMO_ELIGIBILITY_PATH}"
            f"?coupon={MOMO_TRIAL_COUPON}&is_coupon_from_query_param=true"
        )
        eligibility_headers = {
            "Accept": "application/json",
            "Referer": campaign_url,
            "x-openai-target-path": MOMO_ELIGIBILITY_PATH,
            "x-openai-target-route": MOMO_ELIGIBILITY_PATH,
        }
        session_kept = False
        try:
            # Establish the same promo landing-page context used by the
            # successful zero-due HAR before asking for coupon eligibility.
            try:
                chatgpt.request(
                    "GET",
                    campaign_url,
                    headers={"Accept": "text/html", "Referer": "https://chatgpt.com/"},
                    timeout=30,
                )
                setattr(chatgpt, "momo_promo_context_ready", True)
            except Exception:
                pass
            response = chatgpt.request(
                "GET",
                url,
                headers=momo_request_headers(
                    chatgpt,
                    "GET",
                    url,
                    eligibility_headers,
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
            state = ""
            eligible = False
            redeemed = False
            campaign_id = ""
            if not isinstance(payload, dict):
                state = ""
            else:
                state = str(payload.get("state") or "").strip().lower()
                redemption = payload.get("redemption")
                redemption = redemption if isinstance(redemption, dict) else {}
                redeemed = bool(
                    redemption.get("redeemed")
                    or redemption.get("redeemed_by_user")
                    or redemption.get("redeemed_by_workspace")
                )
                eligible = state == "eligible" and not redeemed
                campaign_id = MOMO_TRIAL_COUPON if eligible else ""
            probes.append(
                {
                    "attempt": index,
                    "proxy_slot": index,
                    "http_status": status,
                    "state": state,
                    "eligible": eligible,
                    "coupon": MOMO_TRIAL_COUPON,
                    "campaign_id": campaign_id if eligible else "",
                    "redeemed": redeemed if isinstance(payload, dict) else False,
                }
            )
            if eligible:
                if stage_callback is not None:
                    stage_callback("eligibility_confirmed")
                session_kept = True
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
                    "campaign_id": campaign_id,
                    "source": "chatgpt_check_coupon",
                    "probes": probes,
                    # Internal-only handle; the core reuses this exact
                    # device/session/cookie context for Checkout.
                    "_chatgpt_session": chatgpt,
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
            if not session_kept:
                close(chatgpt)

    raise MomoEligibilityError(
        409,
        "Momo trial eligibility rejected on all VN proxies",
        retryable=True,
    )
