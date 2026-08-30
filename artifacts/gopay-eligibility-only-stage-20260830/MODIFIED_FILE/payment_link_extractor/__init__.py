"""Typed service API for extracting payment-provider checkout links."""

from .application import extract_payment_link
from .gopay_eligibility import probe_gopay_zero_trial_eligibility
from .errors import (
    ConfigurationError,
    ExtractionCancelled,
    ExtractionError,
    NetworkError,
    ProtocolError,
    ProviderRequiresApproval,
)
from .models import BillingProfile, ExtractionConfig, PaymentLinkResult

__all__ = [
    "BillingProfile",
    "ConfigurationError",
    "ExtractionCancelled",
    "ExtractionConfig",
    "ExtractionError",
    "NetworkError",
    "PaymentLinkResult",
    "ProtocolError",
    "ProviderRequiresApproval",
    "extract_payment_link",
    "probe_gopay_zero_trial_eligibility",
]
