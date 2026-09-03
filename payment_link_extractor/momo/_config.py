from __future__ import annotations

from dataclasses import asdict
import os
import random
import re
from pathlib import Path

from ..errors import ConfigurationError
from ..models import BillingProfile


DEFAULT_TIMEOUT = 30
PROVIDER_POLL_TIMEOUT_SECONDS = 5
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_STRIPE_PK = (
    "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRac"
    "ViovU3kLKvpkjh7IqkW00iXQsjo3n"
)
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_VERSION_BASE = "2025-03-31.basil"
STRIPE_RUNTIME_VERSION = "692f102a8f"
OPENAI_CUSTOM_STRIPE_RUNTIME_VERSION = STRIPE_RUNTIME_VERSION

COUNTRY_PROFILES = {
    "VN": {"currency": "VND", "locale": "vi-VN", "timezone": "Asia/Ho_Chi_Minh"},
}
SUPPORTED_COUNTRIES = ("VN",)

_VN_BILLING_FILE = Path(__file__).resolve().parents[1] / "data" / "vn_billing_addresses.txt"
_VN_BILLING_NAMES = (
    ("Nguyen", "Van An"),
    ("Tran", "Thi Binh"),
    ("Le", "Minh Chau"),
    ("Pham", "Hoang Duc"),
    ("Hoang", "Thi Em"),
    ("Vu", "Quoc Huy"),
)
_VN_FALLBACK_ADDRESSES = (
    ("12 Nguyen Hue", "Ho Chi Minh City", "SG", "700000"),
    ("88 Hang Bai", "Hanoi", "HN", "100000"),
)


def _load_vn_billing_addresses(path: str | os.PathLike[str] | None = None) -> list[tuple[str, str, str, str]]:
    source = Path(path or os.environ.get("OPLL_VN_BILLING_FILE") or _VN_BILLING_FILE)
    try:
        lines = [line.strip() for line in source.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except OSError:
        return []
    addresses: list[tuple[str, str, str, str]] = []
    for index in range(0, len(lines) - 2, 3):
        line1, city_line, country = lines[index : index + 3]
        if country.strip().lower() not in {"越南", "vn", "vietnam"}:
            continue
        match = re.fullmatch(r"([^,]+),\s*(.+?)\s+(\d{5,6})", city_line)
        if match:
            addresses.append((line1, match.group(1).strip(), match.group(2).strip(), match.group(3)))
    return list(dict.fromkeys(addresses))[:10]


_VN_BILLING_ADDRESSES = tuple(
    (_load_vn_billing_addresses() or list(_VN_FALLBACK_ADDRESSES))[:10]
)


def country_config(country: str) -> tuple[str, str, str, str]:
    code = str(country or "VN").upper()
    profile = COUNTRY_PROFILES.get(code)
    if not profile:
        raise ConfigurationError("country must be VN")
    return code, profile["currency"], profile["locale"], profile["timezone"]


def billing_for_country(country: str, payment_method: str = "momo") -> BillingProfile:
    country_config(country)
    first_name, last_name = random.choice(_VN_BILLING_NAMES)
    line1, city, state, postal_code = random.choice(_VN_BILLING_ADDRESSES)
    suffix = random.randint(1000, 9999)
    return BillingProfile(
        name=f"{first_name} {last_name}",
        email=f"{first_name.lower()}.{last_name.lower().replace(' ', '')}{suffix}@example.com",
        phone=f"+84{random.randint(300000000, 999999999)}",
        country="VN",
        line1=line1,
        city=city,
        state=state,
        postal_code=postal_code,
    )


def billing_dict_for_country(country: str, payment_method: str = "momo") -> dict[str, str]:
    return billing_for_country(country, payment_method).to_dict()


def currency_minor_scale(currency: str) -> int:
    return 0 if str(currency or "").upper() in {"VND"} else 2


def normalize_payment_method(value: str) -> str:
    method = str(value or "momo").strip().lower() or "momo"
    if method != "momo":
        raise ConfigurationError("payment_method must be momo")
    return method


def processor_entity_for_country(country: str, existing: str = "") -> str:
    return str(existing or "").strip() or "openai_ie"
