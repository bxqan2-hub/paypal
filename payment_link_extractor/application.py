from __future__ import annotations

from dataclasses import replace
import threading
from typing import Any, Callable

from .auth import account_email, normalize_access_token
from .checkout import check_coupon_eligibility, create_checkout, require_country_currency, update_checkout
from .config import (
    billing_for_country,
    country_config,
    country_for_payment_method,
    currency_minor_scale,
    normalize_payment_method,
)
from .errors import ConfigurationError, ExtractionCancelled
from .flows.cs_live import extract_cs_live_provider
from .flows.oaics import extract_oaics_provider
from .logging_utils import stage_logger
from .models import ExtractionConfig, PaymentLinkResult
from .transport import DefaultTransportFactory, TransportFactory, safe_close
from .stripe_common import checkout_payable_amount


def _normalize_config(config: ExtractionConfig) -> ExtractionConfig:
    token = normalize_access_token(config.access_token)
    if not token:
        raise ConfigurationError("AT is required")
    if not str(config.checkout_proxy or "").strip():
        raise ConfigurationError("checkout proxy is required")
    payment_method = normalize_payment_method(config.payment_method)
    # GCash's current browser flow embeds the promo in the initial checkout
    # request and never calls the legacy coupon-check/checkout-update pair.
    # Do not reject a valid GCash task merely because that unused proxy field
    # is empty.
    if (
        config.apply_checkout_update
        and payment_method != "gcash"
        and not str(config.update_proxy or "").strip()
    ):
        raise ConfigurationError("update proxy is required")
    country = country_for_payment_method(payment_method, config.country)
    country, *_ = country_config(country)
    proxy_pool = tuple(
        dict.fromkeys(
            str(value).strip()
            for value in (
                config.proxy_pool
                or config.checkout_proxy_attempts
                or (config.checkout_proxy,)
            )
            if str(value).strip()
        )
    )
    return replace(
        config,
        access_token=token,
        checkout_proxy=str(config.checkout_proxy).strip(),
        update_proxy=str(config.update_proxy).strip(),
        stripe_hcaptcha_token=str(config.stripe_hcaptcha_token or "").strip(),
        country=country,
        payment_method=payment_method,
        proxy_pool=proxy_pool,
    )


def _should_apply_checkout_update(config: ExtractionConfig) -> bool:
    """Return whether the legacy promo update flow applies to this method."""
    return bool(config.apply_checkout_update and config.payment_method != "gcash")


def extract_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    def checkpoint(stage: str) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise ExtractionCancelled("task cancellation requested")
        if stage_callback is not None:
            stage_callback(stage)

    config = _normalize_config(config)
    # GCash is a direct integration of MK-GCash-Link-OpenSource.  Dispatch it
    # before constructing any legacy checkout/Stripe transports so none of the
    # site's previous GCash implementation participates in this branch.
    if config.payment_method == "gcash":
        from .mk_gcash import extract_mk_gcash_payment_link

        return extract_mk_gcash_payment_link(
            config,
            cancel_event=cancel_event,
            stage_callback=checkpoint,
        )
    apply_checkout_update = _should_apply_checkout_update(config)
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
                config,
                chatgpt,
                stripe,
                checkout,
                billing,
                log,
                stage_callback=checkpoint,
            )
        elif checkout["session_kind"] == "openai_custom_checkout":
            checkpoint("stripe_init")
            provider = extract_oaics_provider(
                config,
                chatgpt,
                stripe,
                checkout,
                billing,
                log,
                stage_callback=checkpoint,
            )
        else:
            raise ConfigurationError(f"unsupported checkout session: {checkout.get('cs_id')}")
        amount_due_minor, amount_currency = checkout_payable_amount(checkout)
        scale = currency_minor_scale(amount_currency)
        amount_due = amount_due_minor / (10**scale)
        provider_field = f"{config.payment_method}_url"
        provider_value = str(provider.get(provider_field) or provider.get("provider_url") or "")
        result = PaymentLinkResult(
            checkout_session_id=str(checkout["cs_id"]),
            session_kind=str(checkout["session_kind"]),
            payment_method=config.payment_method,
            billing_country=config.country,
            currency=amount_currency,
            amount_due=amount_due,
            amount_due_minor=amount_due_minor,
            billing=billing_for_country(config.country),
            account_email=account_email(config.access_token),
            payment_method_id=str(provider.get("payment_method_id") or ""),
            stripe_redirect_url=str(provider.get("stripe_redirect_url") or ""),
            provider_url=str(provider.get("provider_url") or provider_value),
            provider_field=provider_field,
            provider_value=provider_value,
        )
        checkpoint("completed")
        return result
    finally:
        safe_close(stripe)
        safe_close(chatgpt)
