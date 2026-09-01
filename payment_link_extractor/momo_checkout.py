from __future__ import annotations

"""Momo OAICS Checkout requests derived from the VN HAR state machine."""

import json
import re
from typing import Any
from urllib.parse import quote

from .config import billing_for_country, processor_entity_for_country
from .errors import ProtocolError
from .momo_transport import momo_request_headers

MOMO_COUNTRY = "VN"
MOMO_CURRENCY = "VND"
SESSION_RE = re.compile(r"(?:oaics_|cs_)[A-Za-z0-9_]+")


def _walk(value: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        for key in keys:
            if value.get(key) not in (None, "", [], {}):
                return value[key]
        for child in value.values():
            found = _walk(child, keys)
            if found not in (None, "", [], {}):
                return found
    elif isinstance(value, list):
        for child in value:
            found = _walk(child, keys)
            if found not in (None, "", [], {}):
                return found
    return None


def session_id(payload: Any) -> str:
    raw = _walk(payload, ("checkout_session_id", "checkoutSessionId", "session_id"))
    match = SESSION_RE.search(str(raw or "")) or SESSION_RE.search(json.dumps(payload, ensure_ascii=False, default=str))
    return match.group(0) if match else ""


def json_payload(response: Any, stage: str) -> dict[str, Any]:
    try:
        value = response.json()
    except Exception as exc:
        try:
            value = json.loads(str(getattr(response, "text", "")))
        except Exception as parse_exc:
            raise ProtocolError(502, f"{stage} returned invalid JSON") from parse_exc
    if not isinstance(value, dict):
        raise ProtocolError(502, f"{stage} returned a non-object payload")
    return value


def request(
    session: Any,
    method: str,
    url: str,
    stage: str,
    *,
    sentinel_flow: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    kwargs["headers"] = momo_request_headers(
        session,
        method,
        url,
        kwargs.get("headers"),
        flow=sentinel_flow,
        referer=str((kwargs.get("headers") or {}).get("Referer") or ""),
    )
    response = session.request(method, url, timeout=30, **kwargs)
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        status = int(response.status_code)
        raise ProtocolError(status, f"{stage} failed (HTTP {status})")
    return json_payload(response, stage)


def create_checkout(
    session: Any,
    *,
    account_email: str = "",
    trial_eligible: bool = False,
    campaign_id: str = "",
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout"
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": MOMO_COUNTRY, "currency": MOMO_CURRENCY},
    }
    referer = "https://chatgpt.com/"
    # The complete zero-due MoMo HAR has this campaign object on the initial
    # Checkout request (body length 245).  It does not use checkout/update.
    if trial_eligible:
        campaign = str(campaign_id or "plus-1-month-free").strip()
        body["promo_campaign"] = {
            "promo_campaign_id": campaign,
            "is_coupon_from_query_param": False,
        }
        referer = (
            "https://chatgpt.com/?promo_campaign=" + quote(campaign, safe="")
        )
        # The browser establishes the promo landing-page context before the
        # POST. Keep this warm-up best-effort so a slow proxy does not hide the
        # Checkout response that remains the source of truth.
        if not bool(getattr(session, "momo_promo_context_ready", False)):
            try:
                session.request(
                    "GET",
                    referer,
                    headers={"Accept": "text/html", "Referer": "https://chatgpt.com/"},
                    timeout=30,
                )
            except Exception:
                pass
    body["checkout_ui_mode"] = "custom"
    payload = request(
        session,
        "POST",
        "https://chatgpt.com" + path,
        "Momo checkout",
        json=body,
        sentinel_flow="chatgpt_checkout",
        headers={
            "Referer": referer,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
    )
    sid = session_id(payload)
    if not sid.startswith("oaics_"):
        raise ProtocolError(502, "Momo checkout did not return an oaics_ session")
    checkout = {
        "cs_id": sid,
        "session_kind": "openai_custom_checkout",
        # The complete VN HAR uses the OpenAI LLC checkout route.  Prefer the
        # server value, while keeping this captured route as the fallback for
        # responses that omit the display-only processor field.
        "processor_entity": str(
            _walk(payload, ("processor_entity", "processorEntity")) or "openai_llc"
        ),
        "billing_country": MOMO_COUNTRY,
        "currency": MOMO_CURRENCY,
        "account_email": account_email,
    }
    aliases = {
        "checkout_session": ("checkout_session", "checkoutSession"),
        "checkout_state": ("checkout_state", "checkoutState"),
        "publishable_key": (
            "publishable_key",
            "stripe_publishable_key",
            "stripePublishableKey",
        ),
        "customer_session_client_secret": (
            "customer_session_client_secret",
            "customerSessionClientSecret",
        ),
        "customer": ("customer", "customer_id", "customerId"),
        "confirm_return_url": ("confirm_return_url", "confirmReturnUrl"),
    }
    for target, keys in aliases.items():
        value = _walk(payload, keys)
        if value not in (None, "", [], {}):
            checkout[target] = value
    checkout["promo_campaign"] = _walk(payload, ("promo_campaign",)) or body.get(
        "promo_campaign", {}
    )
    return checkout


def checkout_amount_minor(checkout: dict[str, Any]) -> int | None:
    raw_total = checkout.get("amount_total")
    if raw_total not in (None, ""):
        try:
            return int(raw_total)
        except (TypeError, ValueError):
            pass
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    due = total.get("total") if isinstance(total.get("total"), dict) else {}
    raw = due.get("minorUnitsAmount")
    if raw in (None, ""):
        raw = checkout.get("payable_amount_minor")
    try:
        return None if raw in (None, "") else int(raw)
    except (TypeError, ValueError):
        return None


def validate_zero_trial_checkout(checkout: dict[str, Any]) -> None:
    """Require the initial Checkout response to carry the 100% discount."""
    campaign = checkout.get("promo_campaign")
    campaign_id = campaign.get("promo_campaign_id") if isinstance(campaign, dict) else ""
    amount = checkout_amount_minor(checkout)
    state = checkout.get("checkout_state") if isinstance(checkout.get("checkout_state"), dict) else {}
    total = state.get("total") if isinstance(state.get("total"), dict) else {}
    subtotal = total.get("subtotal") if isinstance(total.get("subtotal"), dict) else {}
    discount = total.get("discount") if isinstance(total.get("discount"), dict) else {}
    subtotal_minor = subtotal.get("minorUnitsAmount")
    discount_minor = discount.get("minorUnitsAmount")
    if campaign_id != "plus-1-month-free":
        raise ProtocolError(409, "Momo Checkout promo campaign was not attached")
    if amount is None or amount != 0:
        raise ProtocolError(409, f"Momo Checkout zero amount validation failed: {amount}")
    if subtotal_minor not in (None, "") and discount_minor not in (None, ""):
        try:
            if int(discount_minor) != int(subtotal_minor):
                raise ProtocolError(409, "Momo Checkout discount is not 100 percent")
        except (TypeError, ValueError) as exc:
            raise ProtocolError(409, "Momo Checkout discount fields are invalid") from exc

def taxes(
    session: Any,
    checkout: dict[str, Any],
    billing: dict[str, str],
    *,
    phase: int = 2,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/taxes"
    processor = processor_entity_for_country(MOMO_COUNTRY, str(checkout.get("processor_entity") or ""))
    address = {
        "line1": billing["line1"],
        "city": billing["city"],
        "country": MOMO_COUNTRY,
        "postal_code": billing["postal_code"] if int(phase) >= 2 else "",
        "state": billing["state"] if int(phase) >= 1 else "",
    }
    payload = request(
        session,
        "POST",
        "https://chatgpt.com" + path,
        "Momo checkout taxes",
        json={
            "checkout_session_id": checkout["cs_id"],
            "checkout_email": checkout.get("account_email") or billing["email"],
            "billing_country": MOMO_COUNTRY,
            "billing_name": billing["name"],
            "currency": MOMO_CURRENCY.lower(),
            "processor_entity": processor,
            "billing_address": address,
        },
        headers={"Referer": f"https://chatgpt.com/checkout/{processor}/{checkout['cs_id']}", "x-openai-target-path": path, "x-openai-target-route": path},
    )
    checkout.update(
        {
            k: v
            for k, v in payload.items()
            if k
            in {
                "checkout_session",
                "checkout_state",
                "custom_payment_methods",
                "confirm_return_url",
                "amount_total",
                "total_details",
                "payment_method_types",
            }
        }
    )
    nested = payload.get("checkout_session")
    if isinstance(nested, dict):
        # Tax responses wrap the authoritative amount and state one level
        # deeper than the initial Checkout response.
        for key in (
            "checkout_state",
            "amount_total",
            "total_details",
            "payment_method_types",
            "custom_payment_methods",
            "confirm_return_url",
            "processor_entity",
            "publishable_key",
            "customer_session_client_secret",
        ):
            if nested.get(key) not in (None, "", [], {}):
                checkout[key] = nested[key]
    return payload
