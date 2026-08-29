"""Stripe.js-v3-style browser risk identifiers for OAICS and CS checkouts.

The Stripe confirmation requests used by both checkout flows carry ``guid``,
``muid``, ``sid`` and ``stripe_js_id`` browser-session fields.  These helpers
centralize their construction so a checkout context reuses one internally
consistent set instead of mixing unrelated ad-hoc UUID formats.
"""

from __future__ import annotations

import random
import secrets
import string
import uuid


# URL-safe base64url alphabet: no padding and no ``+`` or ``/``.
_BASE64URL = string.ascii_letters + string.digits + "-_"


def _machine_id(prefix: str, size: int = 30) -> str:
    """Build ``prefix`` plus 29-33 URL-safe characters."""
    length = size + random.randint(-1, 3)
    return prefix + "".join(secrets.choice(_BASE64URL) for _ in range(length))


def stripe_guid() -> str:
    """Return a per-visit browser identifier in ``guid_...`` form."""
    return _machine_id("guid_")


def stripe_muid() -> str:
    """Return a merchant-user identifier in ``muid_...`` form."""
    return _machine_id("muid_")


def stripe_sid() -> str:
    """Return a session identifier in ``sid_...`` form."""
    return _machine_id("sid_")


def stripe_js_id() -> str:
    """Return the UUID-form Stripe.js client-session identifier."""
    return str(uuid.uuid4())


def time_on_page_ms(lo: int = 45000, hi: int = 85000) -> int:
    """Return a bounded, midpoint-weighted time-on-page value in milliseconds."""
    if hi <= lo:
        return lo
    midpoint = (lo + hi) / 2
    value = round(random.triangular(lo, hi, midpoint))
    return max(lo, min(hi, value))
