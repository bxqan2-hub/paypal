from __future__ import annotations

"""Momo OAICS Checkout requests derived from the VN HAR state machine."""

import json
import re
from typing import Any

from ..config import processor_entity_for_country
from ..errors import ProtocolError
from .momo_transport import momo_request_headers, record_momo_pending_updates

MOMO_COUNTRY = "VN"
MOMO_CURRENCY = "VND"
SESSION_RE = re.compile(r"(?:oaics_|cs_)[A-Za-z0-9_]+")
CHECKOUT_DATA_QUERY = "_routes=routes%2Fcheckout.%24entity.%24checkoutId"


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
    record_momo_pending_updates(session, response)
    if int(getattr(response, "status_code", 0) or 0) >= 400:
        status = int(response.status_code)
        raise ProtocolError(status, f"{stage} failed (HTTP {status})")
    return json_payload(response, stage)


def create_checkout(session: Any, *, account_email: str = "") -> dict[str, Any]:
    path = "/backend-api/payments/checkout"
    body: dict[str, Any] = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": MOMO_COUNTRY, "currency": MOMO_CURRENCY},
        # The VN browser flow carries the trial campaign in the initial
        # custom-checkout request.  Eligibility is checked separately, but
        # omitting this object makes the server price a normal paid plan.
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "custom",
    }
    payload = request(
        session,
        "POST",
        "https://chatgpt.com" + path,
        "Momo checkout",
        json=body,
        sentinel_flow="chatgpt_checkout",
        headers={"Referer": "https://chatgpt.com/?promo_campaign=plus-1-month-free", "x-openai-target-path": path, "x-openai-target-route": path},
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
        "payment_method_types": (
            "payment_method_types",
            "paymentMethodTypes",
        ),
        "confirm_return_url": ("confirm_return_url", "confirmReturnUrl"),
    }
    for target, keys in aliases.items():
        value = _walk(payload, keys)
        if value not in (None, "", [], {}):
            checkout[target] = value
    return checkout


def hydrate_checkout_route(session: Any, checkout: dict[str, Any]) -> dict[str, Any]:
    """Fetch the Remix checkout route-data document used by the VN browser.

    The ``.data`` response is a route-hydration artifact rather than another
    Checkout mutation.  It is still part of the browser sequence and can
    advance runtime cookies/receipt state, so failures are recorded and the
    main protocol response remains authoritative.
    """
    processor = str(checkout.get("processor_entity") or "openai_llc")
    checkout_id = str(checkout.get("cs_id") or "").strip()
    if not checkout_id:
        checkout["momo_checkout_hydration"] = {"status": 0, "body_length": 0}
        return {}
    url = (
        f"https://chatgpt.com/checkout/{processor}/{checkout_id}.data?"
        f"{CHECKOUT_DATA_QUERY}"
    )
    # The canonical route-data request is a same-origin fetch driven by the
    # checkout page.  Suppress platform API headers while retaining the
    # session cookie jar and coherent browser identity.
    headers = {
        "Accept": "*/*",
        "Referer": "https://chatgpt.com/?promo_campaign=plus-1-month-free",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        "Authorization": None,
        "Content-Type": None,
        "Origin": None,
        "oai-device-id": None,
        "oai-session-id": None,
        "oai-language": None,
        "oai-client-build-number": None,
        "oai-client-version": None,
        "x-oai-is-client-observation": None,
        "x-oai-is-pending-updates": None,
        "x-openai-target-path": None,
        "x-openai-target-route": None,
    }
    # Keep explicit ``None`` values in the per-request mapping.  requests and
    # curl_cffi use them as deletion markers while merging session defaults;
    # filtering them here would silently re-add Authorization/Origin/JSON
    # headers to the canonical route-data document.
    wire_headers = headers
    try:
        response = session.request("GET", url, headers=wire_headers, timeout=30)
    except Exception:
        checkout["momo_checkout_hydration"] = {"status": 0, "body_length": 0}
        return {}
    record_momo_pending_updates(session, response)
    status = int(getattr(response, "status_code", 0) or 0)
    attempts = 1
    # A logged-in browser relies on cookies for this document, while the
    # AT-only route may require the same Bearer AT on a fresh profile. Retry
    # only the two auth-style responses with the current runtime header.
    body_probe = str(getattr(response, "text", "") or "").lower()
    if status in {401, 403} or "/auth/login" in body_probe:
        auth = str(
            getattr(session, "headers", {}).get("Authorization", "")
            if getattr(session, "headers", None) is not None
            else ""
        ).strip()
        if auth:
            fallback_headers = dict(wire_headers)
            fallback_headers["Authorization"] = auth
            device = str(
                getattr(session, "openai_device_id", "") or ""
            ).strip()
            session_id = str(
                getattr(session, "openai_session_id", "") or ""
            ).strip()
            account = str(
                getattr(session, "openai_account_id", "") or ""
            ).strip()
            if device:
                fallback_headers["oai-device-id"] = device
            if session_id:
                fallback_headers["oai-session-id"] = session_id
            if account:
                fallback_headers["chatgpt-account-id"] = account
            response = session.request(
                "GET", url, headers=fallback_headers, timeout=30
            )
            record_momo_pending_updates(session, response)
            status = int(getattr(response, "status_code", 0) or 0)
            attempts = 2
    body = str(getattr(response, "text", "") or "")
    # Remix may answer a document request with a successful-looking 202 while
    # embedding a login redirect.  Keep that distinction explicit: the route
    # was reached, but it was not hydrated for the current AT context.
    redirect_to_login = "/auth/login" in body.lower()
    checkout["momo_checkout_hydration"] = {
        "status": status,
        "body_length": len(body),
        "ok": status < 400 and not redirect_to_login,
        "redirect_to_login": redirect_to_login,
        "attempts": attempts,
    }
    # Route data is serialized as a compact array in current deployments;
    # retain only a small structural marker and never copy opaque values.
    checkout["momo_checkout_hydration_format"] = (
        "devalue_array" if body.lstrip().startswith("[") else "other"
    )
    return {"status": status, "body_length": len(body)}


def refresh_momo_customer_balance(
    session: Any, checkout: dict[str, Any]
) -> dict[str, Any]:
    """Run the read-only customer-balance bootstrap seen after hydration."""
    account = str(getattr(session, "openai_account_id", "") or "").strip()
    if not account:
        checkout["momo_customer_balance"] = {"status": 0}
        return {}
    path = f"/backend-api/accounts/{account}/customer-balance"
    url = "https://chatgpt.com" + path
    headers = momo_request_headers(
        session,
        "GET",
        url,
        {
            "Accept": "*/*",
            "Referer": (
                f"https://chatgpt.com/checkout/"
                f"{checkout.get('processor_entity') or 'openai_llc'}/"
                f"{checkout.get('cs_id') or ''}"
            ),
            "chatgpt-account-id": account,
            "x-openai-target-path": path,
            "x-openai-target-route": path,
        },
    )
    try:
        response = session.request("GET", url, headers=headers, timeout=30)
    except Exception:
        checkout["momo_customer_balance"] = {"status": 0}
        return {}
    record_momo_pending_updates(session, response)
    status = int(getattr(response, "status_code", 0) or 0)
    checkout["momo_customer_balance"] = {"status": status, "ok": status < 400}
    try:
        payload = json_payload(response, "Momo customer balance")
    except ProtocolError:
        payload = {}
    return payload if isinstance(payload, dict) else {}

def taxes(
    session: Any,
    checkout: dict[str, Any],
    billing: dict[str, str],
    *,
    tax_iteration: int | None = None,
) -> dict[str, Any]:
    path = "/backend-api/payments/checkout/taxes"
    processor = processor_entity_for_country(MOMO_COUNTRY, str(checkout.get("processor_entity") or ""))
    # The VN browser progressively fills the address across the three tax
    # refreshes: blank state/postal, state only, then the complete postal
    # code.  Preserve that same session-local progression when requested by
    # the full MoMo route; direct callers retain the complete-address default.
    address_state = billing["state"]
    address_postal = billing["postal_code"]
    if tax_iteration == 1:
        address_state = ""
        address_postal = ""
    elif tax_iteration == 2:
        address_postal = ""
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
            "billing_address": {"line1": billing["line1"], "city": billing["city"], "country": MOMO_COUNTRY, "postal_code": address_postal, "state": address_state},
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
                "customer_session_client_secret",
            }
        }
    )
    # Current responses wrap the refreshed state under checkout_session;
    # flatten the protocol fields consumed by the next Stripe step while
    # retaining the raw wrapper for diagnostics.
    nested = payload.get("checkout_session")
    if isinstance(nested, dict):
        for key in (
            "checkout_state",
            "custom_payment_methods",
            "confirm_return_url",
            "customer_session_client_secret",
            "publishable_key",
            "processor_entity",
            "payment_method_types",
        ):
            value = nested.get(key)
            if value not in (None, "", [], {}):
                checkout[key] = value
        amount_total = nested.get("amount_total")
        if amount_total not in (None, ""):
            try:
                checkout["payable_amount_minor"] = int(amount_total)
            except (TypeError, ValueError):
                pass
    return payload

