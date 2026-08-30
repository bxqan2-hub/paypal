from __future__ import annotations

"""GoPay-only copy of the PayPal legacy Checkout core.

The implementation intentionally owns its Checkout, Stripe flow, and
transport imports so GoPay optimizations cannot change PayPal behavior.
"""

import threading
from dataclasses import replace
from typing import Any, Callable

from .auth import account_email
from .config import billing_for_country, currency_minor_scale
from .errors import ConfigurationError, ExtractionCancelled, ProtocolError
from .gopay_checkout import (
    check_coupon_eligibility,
    create_checkout,
    require_country_currency,
    update_checkout,
)
from .gopay_cs_live import extract_cs_live_provider
from .gopay_oaics import extract_oaics_provider
from .gopay_stripe_common import extract_checkout_totals
from .gopay_transport import GoPayTransportFactory, TransportFactory, safe_close
from .logging_utils import stage_logger
from .models import ExtractionConfig, PaymentLinkResult


GOPAY_COUNTRY = "ID"
GOPAY_CURRENCY = "IDR"
GOPAY_RESULT_FIELD = "gopay_url"


def pin_gopay_attempt_proxy(config: ExtractionConfig) -> ExtractionConfig:
    """Bind every request in one GoPay attempt to its selected proxy.

    The task manager may select a different proxy only when it starts a new
    full attempt.  Downstream eligibility, Checkout, Stripe and provider code
    receives a single-entry view so it cannot switch mid-attempt.
    """
    proxy = str(config.checkout_proxy or "").strip()
    return replace(
        config,
        checkout_proxy=proxy,
        update_proxy=proxy,
        checkout_proxy_attempts=(proxy,),
        update_proxy_attempts=(proxy,),
        proxy_pool=(proxy,),
    )


def _amount_and_currency(checkout: dict[str, Any]) -> tuple[Any, str]:
    state = checkout.get("checkout_state")
    state = state if isinstance(state, dict) else {}
    total = state.get("total")
    total = total if isinstance(total, dict) else {}
    due = total.get("total")
    due = due if isinstance(due, dict) else {}
    raw_amount: Any = due.get("minorUnitsAmount")
    if raw_amount in (None, ""):
        raw_amount = checkout.get("payable_amount_minor")
    if raw_amount in (None, ""):
        from .gopay_oaics import openai_checkout_init_payload

        totals = extract_checkout_totals(openai_checkout_init_payload(checkout))
        raw_amount = totals.get("due")
    currency = str(
        state.get("currency") or checkout.get("currency") or GOPAY_CURRENCY
    ).upper()
    return raw_amount, currency


def checkout_payable_amount_with_presence(
    checkout: dict[str, Any],
) -> tuple[int | None, str]:
    raw_amount, currency = _amount_and_currency(checkout)
    if raw_amount in (None, ""):
        return None, currency
    try:
        return int(raw_amount), currency
    except (TypeError, ValueError):
        return None, currency


def validate_gopay_amount(
    amount_due_minor: int | None,
    *,
    promotion_applied: bool,
) -> None:
    """Fail closed unless the GoPay promotion produces zero due."""
    if not promotion_applied:
        return
    if amount_due_minor is None:
        raise ProtocolError(409, "expected zero amount, got missing")
    try:
        amount = int(amount_due_minor)
    except (TypeError, ValueError) as exc:
        raise ProtocolError(409, "expected zero amount, got invalid") from exc
    if amount != 0:
        raise ProtocolError(409, f"expected zero amount, got {amount_due_minor}")


def extract_gopay_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Run the isolated optimized GoPay Checkout flow."""
    if str(config.payment_method or "").strip().lower() != "gopay":
        raise ConfigurationError("GoPay core requires payment_method=gopay")
    if str(config.country or "").strip().upper() != GOPAY_COUNTRY:
        raise ConfigurationError("GoPay core requires country=ID")

    config = pin_gopay_attempt_proxy(config)

    def checkpoint(stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(stage)

    apply_checkout_update = bool(config.apply_checkout_update)
    zero_trial_validation = bool(config.gopay_zero_trial_validation)
    log = stage_logger(config.verbose)
    billing = billing_for_country(GOPAY_COUNTRY).to_dict()
    factory = transport_factory or GoPayTransportFactory()
    chatgpt = factory.chatgpt(config, config.checkout_proxy)
    stripe = None
    try:
        if zero_trial_validation:
            # Probe the account offer with the same ID proxy/session that will
            # be retained for Checkout. A negative result stops this attempt;
            # the task retry layer rebuilds state on another random ID proxy.
            checkpoint("eligibility_check")
            check_coupon_eligibility(config, chatgpt, log)
            checkpoint("eligibility_confirmed")
        checkpoint("checkout")
        checkout = create_checkout(config, chatgpt, log)
        checkpoint(f"checkout_kind:{checkout['session_kind']}")
        if config.oaics_only and checkout["session_kind"] == "stripe_checkout":
            raise ConfigurationError("仅 OAICS 模式下检测到 CS Checkout，任务已失败")
        require_country_currency(checkout, config)
        if apply_checkout_update:
            checkpoint("checkout_update")
            update_checkout(config, chatgpt, checkout, log)
            # The local GoPay reference project treats promotion application
            # followed by the provider's amount response as the eligibility
            # check; there is no standalone check_coupon request in that flow.
            checkpoint("promotion_applied")
            require_country_currency(checkout, config)
        stripe = factory.stripe(config)
        checkpoint("stripe_init")
        if checkout["session_kind"] == "stripe_checkout":
            provider = extract_cs_live_provider(
                config, chatgpt, stripe, checkout, billing, log, stage_callback=checkpoint
            )
        elif checkout["session_kind"] == "openai_custom_checkout":
            provider = extract_oaics_provider(
                config, chatgpt, stripe, checkout, billing, log, stage_callback=checkpoint
            )
        else:
            raise ConfigurationError(
                f"unsupported checkout session: {checkout.get('cs_id')}"
            )
        amount_due_minor, amount_currency = checkout_payable_amount_with_presence(checkout)
        if zero_trial_validation:
            checkpoint("zero_amount_validation")
            validate_gopay_amount(amount_due_minor, promotion_applied=True)
            checkpoint("zero_amount_confirmed")
        amount_currency = amount_currency or GOPAY_CURRENCY
        amount_due = amount_due_minor / (10 ** currency_minor_scale(amount_currency))
        provider_value = str(
            provider.get(GOPAY_RESULT_FIELD) or provider.get("provider_url") or ""
        )
        if not provider_value:
            raise ConfigurationError("GoPay core did not return gopay_url")
        result = PaymentLinkResult(
            checkout_session_id=str(checkout["cs_id"]),
            session_kind=str(checkout["session_kind"]),
            payment_method="gopay",
            billing_country=GOPAY_COUNTRY,
            currency=amount_currency,
            amount_due=amount_due,
            amount_due_minor=amount_due_minor,
            billing=billing_for_country(GOPAY_COUNTRY),
            account_email=account_email(config.access_token),
            payment_method_id=str(provider.get("payment_method_id") or ""),
            stripe_redirect_url=str(provider.get("stripe_redirect_url") or ""),
            provider_url=str(provider.get("provider_url") or provider_value),
            provider_field=GOPAY_RESULT_FIELD,
            provider_value=provider_value,
            extra={
                "payment_route": "gopay_paypal_core_copy",
                "gopay_optimization_base": "78c1357",
                "gopay_zero_trial_validation": zero_trial_validation,
            },
        )
        checkpoint("completed")
        return result
    finally:
        safe_close(stripe)
        safe_close(chatgpt)
