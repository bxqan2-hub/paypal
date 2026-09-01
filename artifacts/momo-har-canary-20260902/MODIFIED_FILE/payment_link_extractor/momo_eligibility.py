from __future__ import annotations

"""Standalone VN MoMo trial-eligibility probe.

The probe is deliberately separate from Checkout: it can rotate a VN proxy
pool and only the first eligible proxy is allowed to create an ``oaics_*``
session.
"""

from dataclasses import replace
import threading
import time
from typing import Any, Callable
from urllib.parse import quote

from .auth import account_id, normalize_access_token
from .config import normalize_payment_method
from .errors import ConfigurationError, ExtractionCancelled, ProtocolError
from .momo_transport import MomoTransportFactory, close, momo_request_headers


MOMO_TRIAL_COUPON = "plus-1-month-free"
MOMO_ELIGIBILITY_PATH = "/backend-api/promo_campaign/check_coupon"


def _catalog_campaign_match(
    payload: Any, campaign_id: str, *, account_key: str = ""
) -> tuple[bool, bool]:
    """Inspect the account catalog without trusting display-only flags.

    The account endpoint returns a mapping keyed by account UUID (and often a
    ``default`` entry).  ``eligible_promo_campaigns.plus.id`` is the upstream
    project's eligibility hint; the coupon endpoint remains the final check.
    The first return value says that a catalog was present, the second says
    that at least one catalog entry named the requested campaign.
    """
    present = False
    matched = False

    def visit(value: Any) -> None:
        nonlocal present, matched
        if isinstance(value, dict):
            campaigns = value.get("eligible_promo_campaigns")
            if isinstance(campaigns, dict) and "plus" in campaigns:
                present = True
                plus = campaigns.get("plus")
                candidates: list[Any] = []
                if isinstance(plus, dict):
                    candidates.extend(
                        plus.get(key)
                        for key in ("id", "promo_campaign_id", "campaign_id", "code")
                    )
                elif isinstance(plus, list):
                    candidates.extend(plus)
                for candidate in candidates:
                    if isinstance(candidate, dict):
                        candidate = next(
                            (
                                candidate.get(key)
                                for key in ("id", "promo_campaign_id", "campaign_id", "code")
                                if candidate.get(key)
                            ),
                            "",
                        )
                    if str(candidate or "").strip() == campaign_id:
                        matched = True
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    target = payload
    if isinstance(payload, dict) and isinstance(payload.get("accounts"), dict):
        accounts = payload["accounts"]
        # Prefer the account selected by the AT; the API also supplies a
        # ``default`` view for single-account sessions.
        target = accounts.get(account_key)
        if target is None and not account_key:
            target = accounts.get("default")
        if target is None:
            return True, False
    visit(target)
    return present, matched


class MomoEligibilityError(ProtocolError):
    """Eligibility failure that occurred before Checkout creation."""

    def __init__(self, status_code: int, message: str, *, retryable: bool = True) -> None:
        super().__init__(status_code, message)
        self.retryable = retryable
        self.probes: tuple[dict[str, Any], ...] = ()


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
        campaign_url = (
            "https://chatgpt.com/?promo_campaign=" + MOMO_TRIAL_COUPON
        )
        url = (
            f"https://chatgpt.com{MOMO_ELIGIBILITY_PATH}"
            f"?coupon={MOMO_TRIAL_COUPON}&is_coupon_from_query_param=true"
        )
        eligibility_headers = {
            "Accept": "*/*",
            "Referer": campaign_url,
            "x-openai-target-path": MOMO_ELIGIBILITY_PATH,
            "x-openai-target-route": MOMO_ELIGIBILITY_PATH,
        }
        session_kept = False
        try:
            setattr(chatgpt, "momo_flow_started_at", time.perf_counter())
            # Establish the same promo landing-page context used by the
            # successful zero-due HAR before asking for coupon eligibility.
            try:
                landing_response = chatgpt.request(
                    "GET",
                    campaign_url,
                    headers={"Accept": "text/html", "Referer": "https://chatgpt.com/"},
                    timeout=30,
                )
                if 200 <= int(getattr(landing_response, "status_code", 0) or 0) < 400:
                    setattr(chatgpt, "momo_promo_context_ready", True)
            except Exception:
                pass
            # Prime the browser proof on the same promo origin before the
            # eligibility/Checkout pair.  This is best-effort for installs
            # without Chromium; the coupon response remains authoritative.
            provider = getattr(chatgpt, "openai_sentinel_provider", None)
            prepare_flow = getattr(provider, "prepare_flow", None)
            if callable(prepare_flow):
                try:
                    prepare_flow("chatgpt_checkout", referer=campaign_url)
                    setattr(chatgpt, "momo_promo_context_ready", True)
                except Exception:
                    pass
            # The complete browser flow warms the account catalog immediately
            # before coupon evaluation. Keep both calls on the same session;
            # their responses provide the account-scoped eligibility signal;
            # check_coupon below must independently echo the same campaign.
            selected_account = account_id(token)
            account_headers = {
                "Accept": "*/*",
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": "/backend-api/accounts/optimized/check",
                "x-openai-target-route": "/backend-api/accounts/optimized/check",
            }
            if selected_account:
                account_headers["chatgpt-account-id"] = selected_account
            try:
                chatgpt.request(
                    "GET",
                    "https://chatgpt.com/backend-api/accounts/optimized/check",
                    headers=momo_request_headers(
                        chatgpt,
                        "GET",
                        "https://chatgpt.com/backend-api/accounts/optimized/check",
                        account_headers,
                    ),
                    timeout=30,
                )
            except Exception:
                pass
            account_path = "/backend-api/accounts/check/v4-2023-04-27"
            account_url = "https://chatgpt.com" + account_path + "?timezone_offset_min=-420"
            account_headers["x-openai-target-path"] = account_path
            account_headers["x-openai-target-route"] = "/backend-api/accounts/check/{version}"
            catalog_present = False
            catalog_match = False
            catalog_request_ok = False
            try:
                account_response = chatgpt.request(
                    "GET",
                    account_url,
                    headers=momo_request_headers(
                        chatgpt, "GET", account_url, account_headers
                    ),
                    timeout=30,
                )
                if int(getattr(account_response, "status_code", 0) or 0) < 400:
                    catalog_request_ok = True
                    try:
                        account_payload = account_response.json() or {}
                    except Exception:
                        account_payload = None
                        catalog_request_ok = False
                    if account_payload is None:
                        catalog_present = True
                        catalog_match = False
                    else:
                        catalog_present, catalog_match = _catalog_campaign_match(
                            account_payload,
                            MOMO_TRIAL_COUPON,
                            account_key=selected_account,
                        )
            except Exception:
                catalog_request_ok = False
            # Keep the pricing and saved-payment context warm on the same
            # device/session.  The account/catalog result is required for a
            # selected AT; a coupon-only response must not green-light a
            # checkout when the account catalog was unavailable.
            pricing_path = "/backend-api/checkout_pricing_config/configs/VN"
            pricing_url = "https://chatgpt.com" + pricing_path
            pricing_headers = {
                "Accept": "*/*",
                "Referer": "https://chatgpt.com/",
                "x-openai-target-path": pricing_path,
                "x-openai-target-route": "/backend-api/checkout_pricing_config/configs/{country_code}",
            }
            try:
                chatgpt.request(
                    "GET",
                    pricing_url,
                    headers=momo_request_headers(
                        chatgpt, "GET", pricing_url, pricing_headers
                    ),
                    timeout=30,
                )
            except Exception:
                pass
            if selected_account:
                methods_path = "/backend-api/payments/payment_methods"
                methods_url = (
                    "https://chatgpt.com"
                    + methods_path
                    + "?account_id="
                    + quote(selected_account, safe="")
                )
                methods_headers = {
                    "Accept": "*/*",
                    "Referer": "https://chatgpt.com/",
                    "x-openai-target-path": methods_path,
                    "x-openai-target-route": methods_path,
                    "chatgpt-account-id": selected_account,
                }
                try:
                    chatgpt.request(
                        "GET",
                        methods_url,
                        headers=momo_request_headers(
                            chatgpt, "GET", methods_url, methods_headers
                        ),
                        timeout=30,
                    )
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
            # The browser reloads pricing after the coupon route resolves;
            # keep the promo referer on this second read before Checkout.
            promo_pricing_headers = dict(pricing_headers)
            promo_pricing_headers["Referer"] = campaign_url
            try:
                chatgpt.request(
                    "GET",
                    pricing_url,
                    headers=momo_request_headers(
                        chatgpt, "GET", pricing_url, promo_pricing_headers
                    ),
                    timeout=30,
                )
            except Exception:
                pass
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
                    error = MomoEligibilityError(
                        status,
                        "Momo eligibility authentication failed (HTTP 401)",
                        retryable=False,
                    )
                    error.probes = tuple(probes)
                    raise error
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
            coupon_match = False
            redemption_valid = False
            if not isinstance(payload, dict):
                state = ""
            else:
                state = str(payload.get("state") or "").strip().lower()
                coupon_match = str(payload.get("coupon") or "").strip() == MOMO_TRIAL_COUPON
                redemption = payload.get("redemption")
                redemption_valid = isinstance(redemption, dict) and "redeemed" in redemption
                redemption = redemption if isinstance(redemption, dict) else {}
                redeemed = bool(
                    redemption.get("redeemed")
                    or redemption.get("redeemed_by_user")
                    or redemption.get("redeemed_by_workspace")
                )
                # Fail closed when the account catalog explicitly lacks the
                # campaign.  If that optional endpoint is unavailable, the
                # coupon response can still decide eligibility.
                eligible = (
                    state == "eligible"
                    and coupon_match
                    and redemption_valid
                    and not redeemed
                    and (
                        (catalog_present and catalog_match)
                        if selected_account
                        else (not catalog_present or catalog_match)
                    )
                    and (not selected_account or catalog_request_ok)
                )
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
                    "coupon_match": coupon_match,
                    "redemption_valid": redemption_valid,
                    "catalog_present": catalog_present,
                    "catalog_match": catalog_match if catalog_present else None,
                    "catalog_request_ok": catalog_request_ok,
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

    error = MomoEligibilityError(
        409,
        "Momo trial eligibility rejected on all VN proxies",
        retryable=True,
    )
    error.probes = tuple(probes)
    raise error
