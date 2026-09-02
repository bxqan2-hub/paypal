from __future__ import annotations

"""Standalone VN MoMo trial-eligibility probe.

The probe is deliberately separate from Checkout: it can rotate a VN proxy
pool and only the first eligible proxy is allowed to create an ``oaics_*``
session.
"""

from dataclasses import replace
import threading
from typing import Any, Callable

from .auth import account_id, normalize_access_token
from .config import normalize_payment_method
from .errors import ConfigurationError, ExtractionCancelled, ProtocolError
from .momo_transport import MomoTransportFactory, close, momo_request_headers


MOMO_TRIAL_COUPON = "plus-1-month-free"
MOMO_ELIGIBILITY_PATH = "/backend-api/accounts/check/v4-2023-04-27"
MOMO_TIMEZONE_OFFSET_MIN = -420


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
    retain_session: bool = False,
) -> dict[str, Any]:
    """Check the VN trial coupon before creating a Checkout session.

    ``retain_session`` is used by the full MoMo flow so the account probe and
    Checkout share the same device/session IDs, cookie jar and proxy context.
    Standalone callers retain the historical close-after-probe behavior.
    """
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

    factory = transport_factory or MomoTransportFactory(
        str(getattr(config, "momo_fingerprint", "") or "")
    )
    probes: list[dict[str, Any]] = []
    for index, proxy in enumerate(proxies, start=1):
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(f"eligibility_proxy:{index}")
        attempt = _attempt_config(replace(config, access_token=token), proxy)
        chatgpt = factory.chatgpt(attempt, proxy)
        # Bootstrap the browser Sentinel context before the authenticated
        # account/coupon probes.  The captured route establishes the same
        # device, cookies and SDK context before eligibility and reuses it
        # through Checkout Confirm.
        provider = getattr(chatgpt, "openai_sentinel_provider", None)
        prepare = getattr(provider, "prepare", None)
        if callable(prepare):
            if stage_callback is not None:
                stage_callback(f"sentinel_prepare:{index}")
            try:
                prepare()
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
                close(chatgpt)
                continue
        url = (
            f"https://chatgpt.com{MOMO_ELIGIBILITY_PATH}"
            f"?timezone_offset_min={MOMO_TIMEZONE_OFFSET_MIN}"
        )
        selected_account = account_id(token)
        eligibility_headers = {
            "Accept": "application/json",
            "Referer": "https://chatgpt.com/",
            "x-openai-target-path": MOMO_ELIGIBILITY_PATH,
            "x-openai-target-route": MOMO_ELIGIBILITY_PATH,
        }
        # The browser's initial account probe does not send an explicit
        # account header; the bearer token selects the account.  The header is
        # added later by momo_request_headers for taxes/confirm only.
        keep_session = False
        try:
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
            campaign_id = ""
            if not isinstance(payload, dict):
                state = ""
                eligible = False
            else:
                accounts = payload.get("accounts")
                accounts = accounts if isinstance(accounts, dict) else {}
                account = accounts.get(selected_account) or accounts.get("default") or {}
                account = account if isinstance(account, dict) else {}
                campaigns = account.get("eligible_promo_campaigns")
                campaigns = campaigns if isinstance(campaigns, dict) else {}
                plus = campaigns.get("plus")
                plus = plus if isinstance(plus, dict) else {}
                campaign_id = str(
                    plus.get("id") or plus.get("campaign_id") or ""
                ).strip()
                state = "eligible" if campaign_id else "not_eligible"
                eligible = bool(campaign_id)
            # The browser route validates the same offer again through the
            # promo endpoint before opening Checkout.  Keep this probe in the
            # same authenticated session.  Older deployments may omit or
            # reject the endpoint, so an authoritative accounts/check result
            # remains usable when the supplemental request is unavailable.
            coupon_state = ""
            coupon_http_status = 0
            coupon_url = (
                "https://chatgpt.com/backend-api/promo_campaign/check_coupon"
                f"?coupon={MOMO_TRIAL_COUPON}&is_coupon_from_query_param=true"
            )
            try:
                coupon_response = chatgpt.request(
                    "GET",
                    coupon_url,
                    headers=momo_request_headers(
                        chatgpt,
                        "GET",
                        coupon_url,
                        {
                            "Accept": "application/json",
                            "Referer": "https://chatgpt.com/?promo_campaign=plus-1-month-free",
                            "x-openai-target-path": "/backend-api/promo_campaign/check_coupon",
                            "x-openai-target-route": "/backend-api/promo_campaign/check_coupon",
                        },
                    ),
                    timeout=30,
                )
                coupon_http_status = int(
                    getattr(coupon_response, "status_code", 0) or 0
                )
                if coupon_http_status < 400:
                    coupon_payload = coupon_response.json() or {}
                    if isinstance(coupon_payload, dict):
                        coupon_state = str(coupon_payload.get("state") or "").strip().lower()
            except Exception:
                coupon_state = ""
            if coupon_state == "not_eligible":
                eligible = False
                state = "not_eligible"
            elif coupon_state == "eligible" and not campaign_id:
                campaign_id = MOMO_TRIAL_COUPON
                eligible = True
                state = "eligible"
            probes.append(
                {
                    "attempt": index,
                    "proxy_slot": index,
                    "http_status": status,
                    "state": state,
                    "eligible": eligible,
                    "coupon": MOMO_TRIAL_COUPON,
                    "campaign_id": campaign_id if eligible else "",
                    "coupon_state": coupon_state,
                    "coupon_http_status": coupon_http_status,
                }
            )
            if eligible:
                if stage_callback is not None:
                    stage_callback("eligibility_confirmed")
                result = {
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
                }
                if retain_session:
                    # Transfer ownership to the caller; the finally block
                    # must not close the session that carries this context.
                    result["_chatgpt_session"] = chatgpt
                    keep_session = True
                return result
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
            if not keep_session:
                close(chatgpt)

    raise MomoEligibilityError(
        409,
        "Momo trial eligibility rejected on all VN proxies",
        retryable=True,
    )
