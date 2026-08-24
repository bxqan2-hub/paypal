from __future__ import annotations

import threading
from typing import Any, Callable

from gcash_chain import GCashChain

from .auth import account_email, account_id
from .config import billing_for_country
from .errors import ExtractionCancelled, ProtocolError
from .models import ExtractionConfig, PaymentLinkResult


MK_GCASH_SOURCE_COMMIT = "2607d879ce2005ef9a9c6cdfa1ec747c6f26d4d5"

_STAGE_MAP = {
    "proxy_test": "eligibility_check",
    "create_checkout": "checkout",
    "configure_taxes": "taxes",
    "confirm_payment": "payment_confirmation",
    "start_payment": "payment_confirmation",
    "follow_redirect": "redirect_resolution",
}


def _as_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def extract_mk_gcash_payment_link(
    config: ExtractionConfig,
    *,
    cancel_event: threading.Event | None = None,
    stage_callback: Callable[[str], None] | None = None,
) -> PaymentLinkResult:
    """Run the vendored MK GCash core and adapt its result to this application."""
    billing = billing_for_country("PH")
    token_account_id = account_id(config.access_token)
    email = account_email(config.access_token) or billing.email
    last_stage = ""

    def cancelled() -> bool:
        return bool(cancel_event is not None and cancel_event.is_set())

    def on_update(snapshot: dict[str, Any]) -> None:
        nonlocal last_stage
        stage = _STAGE_MAP.get(str(snapshot.get("current_step") or ""), "")
        if stage and stage != last_stage and stage_callback is not None:
            last_stage = stage
            stage_callback(stage)

    chain = GCashChain(
        token=config.access_token,
        client_account_id=token_account_id or email,
        account_id=token_account_id,
        billing_email=email,
        billing_name=billing.name,
        proxy=config.checkout_proxy,
        on_update=on_update,
        cancel_check=cancelled,
    )
    result = chain.run()
    if str(result.get("status") or "") != "success":
        detail = str(result.get("error_message") or "GCash extraction failed")
        if cancelled() or "TASK_CANCELLED" in detail:
            raise ExtractionCancelled("task cancellation requested")
        raise ProtocolError(502, detail)

    gcash_url = str(result.get("gcash_url") or "")
    if not gcash_url:
        raise ProtocolError(502, "MK GCash core returned success without gcash_url")

    amount_minor = _as_int(chain.checkout_amount)
    adapted = PaymentLinkResult(
        checkout_session_id=str(chain.cid or ""),
        session_kind="openai_custom_checkout",
        payment_method="gcash",
        billing_country="PH",
        currency="PHP",
        amount_due=amount_minor / 100,
        amount_due_minor=amount_minor,
        billing=billing,
        account_email=email,
        payment_method_id=str(chain.cpmt or ""),
        stripe_redirect_url=str(chain.adyen_url or ""),
        provider_url=gcash_url,
        provider_field="gcash_url",
        provider_value=gcash_url,
        extra={
            "mk_gcash_source_commit": MK_GCASH_SOURCE_COMMIT,
            "payment_route": str(result.get("payment_route") or ""),
            "qr_text": str(result.get("qr_text") or ""),
            "qr_short": str(result.get("qr_short") or ""),
            "net_auth_id": str(result.get("net_auth_id") or ""),
            "qr_expires_at": result.get("qr_expires_at"),
            "monitor_id": str(result.get("monitor_id") or ""),
            "callback_status": str(result.get("callback_status") or ""),
        },
    )
    if stage_callback is not None:
        stage_callback("completed")
    return adapted
