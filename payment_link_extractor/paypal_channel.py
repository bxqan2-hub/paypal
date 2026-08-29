from __future__ import annotations

"""Shared legacy Checkout/Stripe extraction core for PayPal-family methods."""

import threading
from typing import Callable

from .auth import account_email
from .channels import payment_channel
from .checkout import check_coupon_eligibility, create_checkout, require_country_currency, update_checkout
from .config import billing_for_country, currency_minor_scale
from .errors import ConfigurationError, ExtractionCancelled
from .flows.cs_live import extract_cs_live_provider
from .flows.oaics import extract_oaics_provider
from .logging_utils import stage_logger
from .models import ExtractionConfig, PaymentLinkResult
from .stripe_common import checkout_payable_amount
from .transport import DefaultTransportFactory, TransportFactory, safe_close


def extract_legacy_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    method = str(config.payment_method or "paypal").strip().lower() or "paypal"
    channel = payment_channel(method)
    if method not in {"paypal", "gopay", "gopay_pro"}:
        raise ConfigurationError(f"legacy Checkout core does not support {method}")

    def checkpoint(stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(stage)

    apply_checkout_update = bool(config.apply_checkout_update and channel.uses_checkout_update)
    log = stage_logger(config.verbose)
    billing = billing_for_country(config.country).to_dict()
    factory = transport_factory or DefaultTransportFactory()
    chatgpt = factory.chatgpt(config, config.checkout_proxy)
    stripe = None
    try:
        if apply_checkout_update:
            checkpoint("eligibility_check")
            check_coupon_eligibility(config, chatgpt, log)
        checkpoint("checkout")
        checkout = create_checkout(config, chatgpt, log)
        checkpoint(f"checkout_kind:{checkout['session_kind']}")
        if config.oaics_only and checkout["session_kind"] == "stripe_checkout":
            raise ConfigurationError("仅 OAICS 模式下检测到 CS Checkout，任务已失败")
        require_country_currency(checkout, config)
        if apply_checkout_update:
            checkpoint("checkout_update")
            update_checkout(config, chatgpt, checkout, log)
            require_country_currency(checkout, config)
        stripe = factory.stripe(config)
        if checkout["session_kind"] == "stripe_checkout":
            checkpoint("stripe_init")
            provider = extract_cs_live_provider(
                config, chatgpt, stripe, checkout, billing, log, stage_callback=checkpoint
            )
        elif checkout["session_kind"] == "openai_custom_checkout":
            checkpoint("stripe_init")
            provider = extract_oaics_provider(
                config, chatgpt, stripe, checkout, billing, log, stage_callback=checkpoint
            )
        else:
            raise ConfigurationError(
                f"unsupported checkout session: {checkout.get('cs_id')}"
            )
        amount_due_minor, amount_currency = checkout_payable_amount(checkout)
        scale = currency_minor_scale(amount_currency)
        amount_due = amount_due_minor / (10**scale)
        provider_value = str(
            provider.get(channel.result_field) or provider.get("provider_url") or ""
        )
        result = PaymentLinkResult(
            checkout_session_id=str(checkout["cs_id"]),
            session_kind=str(checkout["session_kind"]),
            payment_method=channel.name,
            billing_country=config.country,
            currency=amount_currency,
            amount_due=amount_due,
            amount_due_minor=amount_due_minor,
            billing=billing_for_country(config.country),
            account_email=account_email(config.access_token),
            payment_method_id=str(provider.get("payment_method_id") or ""),
            stripe_redirect_url=str(provider.get("stripe_redirect_url") or ""),
            provider_url=str(provider.get("provider_url") or provider_value),
            provider_field=channel.result_field,
            provider_value=provider_value,
        )
        checkpoint("completed")
        return result
    finally:
        safe_close(stripe)
        safe_close(chatgpt)


def extract_paypal_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Extract a PayPal link through the canonical shared core."""
    return extract_legacy_payment_link(
        config,
        transport_factory=transport_factory,
        cancel_event=cancel_event,
        stage_callback=stage_callback,
    )
