from __future__ import annotations

"""Isolated payment-channel registry and dispatch boundary."""

from dataclasses import dataclass
from importlib import import_module
import threading
from typing import Any, Callable

from .errors import ConfigurationError
from .models import ExtractionConfig, PaymentLinkResult


@dataclass(frozen=True)
class PaymentChannel:
    name: str
    label: str
    adapter_module: str
    adapter_callable: str
    result_field: str
    country: str = ""
    currency: str = ""
    uses_legacy_transport: bool = False
    uses_checkout_update: bool = False


PAYMENT_CHANNELS: dict[str, PaymentChannel] = {
    "paypal": PaymentChannel(
        name="paypal",
        label="PayPal",
        adapter_module="payment_link_extractor.paypal_channel",
        adapter_callable="extract_paypal_payment_link",
        result_field="paypal_url",
        uses_legacy_transport=True,
        uses_checkout_update=True,
    ),
    "gopay": PaymentChannel(
        name="gopay",
        label="GoPay",
        adapter_module="payment_link_extractor.gopay_channel",
        adapter_callable="extract_gopay_payment_link",
        result_field="gopay_url",
        country="ID",
        currency="IDR",
        uses_legacy_transport=False,
        uses_checkout_update=True,
    ),
    "gcash": PaymentChannel(
        name="gcash",
        label="GCash",
        adapter_module="payment_link_extractor.mk_gcash",
        adapter_callable="extract_mk_gcash_payment_link",
        result_field="gcash_url",
        country="PH",
        currency="PHP",
    ),
}
PAYMENT_CHANNEL_NAMES = tuple(PAYMENT_CHANNELS)


def payment_channel(name: str) -> PaymentChannel:
    key = str(name or "paypal").strip().lower() or "paypal"
    try:
        return PAYMENT_CHANNELS[key]
    except KeyError as exc:
        raise ConfigurationError(
            "payment_method must be one of " + ", ".join(PAYMENT_CHANNEL_NAMES)
        ) from exc


def public_payment_channels() -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for channel in PAYMENT_CHANNELS.values():
        item = {"value": channel.name, "label": channel.label}
        if channel.country:
            item.update(country=channel.country, currency=channel.currency)
        result.append(item)
    return result


def invoke_payment_channel(
    channel: PaymentChannel,
    config: ExtractionConfig,
    *,
    transport_factory: Any = None,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    module = import_module(channel.adapter_module)
    extractor = getattr(module, channel.adapter_callable, None)
    if not callable(extractor):
        raise ConfigurationError(
            f"payment channel adapter is unavailable: {channel.name}"
        )
    kwargs: dict[str, Any] = {
        "cancel_event": cancel_event,
        "stage_callback": stage_callback,
    }
    if transport_factory is not None and channel.name in {"paypal", "gopay"}:
        kwargs["transport_factory"] = transport_factory
    return extractor(config, **kwargs)
