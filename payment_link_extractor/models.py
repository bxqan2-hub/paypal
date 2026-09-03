from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ExtractionConfig:
    access_token: str
    checkout_proxy: str
    update_proxy: str
    stripe_hcaptcha_token: str = ""
    country: str = "GB"
    payment_method: str = "paypal"
    apply_checkout_update: bool = True
    # GoPay-only gate: when enabled, run the zero-trial eligibility probe and
    # reject a generated link unless the authoritative payable amount is zero.
    # Other channels ignore this field.
    gopay_zero_trial_validation: bool = True
    momo_zero_trial_validation: bool = True
    verbose: bool = True
    oaics_only: bool = False
    retry_count: int = 0
    checkout_proxy_attempts: tuple[str, ...] = ()
    update_proxy_attempts: tuple[str, ...] = ()
    # MK-style single proxy pool. Legacy checkout/update fields remain as
    # transport aliases, but new web tasks populate both from this one pool.
    proxy_pool: tuple[str, ...] = ()
    # Optional account metadata accepted by the upstream MK app.py account
    # object. GCash uses these before falling back to JWT profile claims.
    account_name: str = ""
    account_email: str = ""
    # Kept for API compatibility with older task payloads; the shared legacy
    # PayPal/GoPay core does not require a separate provider session.
    session_token: str = ""


@dataclass(frozen=True)
class BillingProfile:
    name: str
    email: str
    phone: str
    country: str
    line1: str
    city: str
    state: str
    postal_code: str

    def to_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "email": self.email,
            "phone": self.phone,
            "country": self.country,
            "line1": self.line1,
            "city": self.city,
            "state": self.state,
            "postal_code": self.postal_code,
        }


@dataclass
class PaymentLinkResult:
    checkout_session_id: str
    session_kind: str
    payment_method: str
    billing_country: str
    currency: str
    amount_due: float
    amount_due_minor: int
    billing: BillingProfile
    account_email: str = ""
    payment_method_id: str = ""
    stripe_redirect_url: str = ""
    provider_url: str = ""
    provider_field: str = ""
    provider_value: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "checkout_session_id": self.checkout_session_id,
            "session_kind": self.session_kind,
            "payment_method": self.payment_method,
            "billing_country": self.billing_country,
            "currency": self.currency,
            "amount_due": self.amount_due,
            "amount_due_minor": self.amount_due_minor,
            "billing": self.billing.to_dict(),
            "account_email": self.account_email,
            "payment_method_id": self.payment_method_id,
            "stripe_redirect_url": self.stripe_redirect_url,
            "provider_url": self.provider_url,
        }
        if self.provider_field and self.provider_value:
            result[self.provider_field] = self.provider_value
        result.update(self.extra)
        return result


# Internal payloads remain JSON-shaped because the upstream APIs are dynamic.
CheckoutData = dict[str, Any]
StripeContext = dict[str, Any]
