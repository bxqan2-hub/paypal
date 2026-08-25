from __future__ import annotations

"""Normalize a task and dispatch it to one isolated payment channel."""

from dataclasses import replace
import threading
from typing import Callable

from .auth import normalize_access_token
from .channels import invoke_payment_channel, payment_channel
from .config import country_config, country_for_payment_method, normalize_payment_method
from .errors import ConfigurationError
from .models import ExtractionConfig, PaymentLinkResult
from .transport import TransportFactory


def _normalize_config(config: ExtractionConfig) -> ExtractionConfig:
    token = normalize_access_token(config.access_token)
    if not token:
        raise ConfigurationError("AT is required")
    if not str(config.checkout_proxy or "").strip():
        raise ConfigurationError("checkout proxy is required")
    payment_method = normalize_payment_method(config.payment_method)
    channel = payment_channel(payment_method)
    if (
        config.apply_checkout_update
        and channel.uses_checkout_update
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
        session_token=str(config.session_token or "").strip(),
        stripe_hcaptcha_token=str(config.stripe_hcaptcha_token or "").strip(),
        country=country,
        payment_method=payment_method,
        proxy_pool=proxy_pool,
    )


def _should_apply_checkout_update(config: ExtractionConfig) -> bool:
    channel = payment_channel(config.payment_method)
    return bool(config.apply_checkout_update and channel.uses_checkout_update)


def extract_payment_link(
    config: ExtractionConfig,
    *,
    transport_factory: TransportFactory | None = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    normalized = _normalize_config(config)
    channel = payment_channel(normalized.payment_method)
    return invoke_payment_channel(
        channel,
        normalized,
        transport_factory=transport_factory,
        cancel_event=cancel_event,
        stage_callback=stage_callback,
    )
