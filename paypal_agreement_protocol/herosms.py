"""Small synchronous HeroSMS adapter used by the protocol-payment bridge.

The copied PayPal flow is deliberately not modified.  This module only owns
number allocation and SMS polling, and keeps the API key in the process
environment instead of source control.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

import httpx


class HeroSMSError(RuntimeError):
    """Raised when HeroSMS cannot allocate or read an activation."""


_FALLBACK_COUNTRY_IDS = {
    "BR": 73, "GB": 16, "US": 187, "JP": 114, "TH": 52, "ID": 6,
    "PH": 4, "TW": 201, "MX": 54, "AE": 182, "AU": 175, "CA": 36,
}


class HeroSMSClient:
    def __init__(self) -> None:
        self.api_key = os.getenv("HEROSMS_API_KEY", "").strip()
        self.base_url = os.getenv(
            "HEROSMS_BASE_URL", "https://hero-sms.com/stubs/handler_api.php"
        ).strip()
        self.default_service = os.getenv("HEROSMS_SERVICE", "paypal").strip() or "paypal"
        self.default_max_price = _optional_float(os.getenv("HEROSMS_MAX_PRICE", ""))
        self.poll_interval = max(2.0, _optional_float(os.getenv("HEROSMS_POLL_INTERVAL", "5")) or 5.0)
        self.timeout = max(30.0, _optional_float(os.getenv("HEROSMS_TIMEOUT", "1800")) or 1800.0)
        self._countries: Any = None

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def _request(self, action: str, **params: Any) -> Any:
        if not self.configured:
            raise HeroSMSError("HEROSMS_API_KEY is not configured")
        query = {"api_key": self.api_key, "action": action}
        query.update({key: value for key, value in params.items() if value is not None})
        try:
            response = httpx.get(self.base_url, params=query, timeout=30.0)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise HeroSMSError(f"HeroSMS request failed: {exc}") from exc
        try:
            return response.json()
        except ValueError:
            return response.text.strip()

    def get_countries(self, *, force: bool = False) -> Any:
        if self._countries is None or force:
            self._countries = self._request("getCountries")
        return self._countries

    def resolve_country_id(self, country: str) -> int:
        code = str(country or "").strip().upper()
        if code.isdigit():
            return int(code)
        try:
            payload = self.get_countries()
            for country_id, item in _country_entries(payload):
                text = " ".join(str(item.get(key, "")) for key in (
                    "iso", "isoCode", "countryCode", "code", "name", "name_en", "name_zh", "country",
                    "countryName", "country_name", "nameEn", "nameCn"
                )).upper()
                if code in text or _country_alias_match(code, text):
                    return int(country_id)
        except (HeroSMSError, ValueError, TypeError):
            pass
        if code in _FALLBACK_COUNTRY_IDS:
            return _FALLBACK_COUNTRY_IDS[code]
        raise HeroSMSError(f"HeroSMS country is unavailable: {code}")

    def acquire_number(
        self, country: str, *, max_price: float | None = None, service: str | None = None
    ) -> dict[str, Any]:
        country_code = str(country or "").strip().upper()
        country_id = self.resolve_country_id(country_code)
        price = self.default_max_price if max_price is None else max_price
        payload = self._request(
            "getNumberV2", service=(service or self.default_service), country=country_id,
            maxPrice=price, fixedPrice="1" if price is not None else None,
        )
        activation_id, phone, cost = _parse_activation(payload)
        if not activation_id or not phone:
            raise HeroSMSError("HeroSMS returned no activation number")
        return {
            "activation_id": str(activation_id), "phone": _normalise_phone(phone),
            "country": country_code, "country_id": country_id,
            "service": service or self.default_service, "price": cost,
        }

    def get_status(self, activation_id: str) -> dict[str, Any]:
        payload = self._request("getStatusV2", id=str(activation_id))
        if isinstance(payload, str):
            parts = payload.split(":", 1)
            return {"status": parts[0], "code": parts[1] if len(parts) == 2 else "", "raw": payload}
        if isinstance(payload, dict):
            result = dict(payload)
            result.setdefault("status", result.get("state") or result.get("statusCode") or "")
            result.setdefault("code", result.get("smsCode") or result.get("verificationCode") or "")
            return result
        return {"status": "", "code": ""}

    def finish(self, activation_id: str, status: int = 6) -> None:
        try:
            self._request("setStatus", id=str(activation_id), status=int(status))
        except HeroSMSError:
            return

    def wait_for_code(self, activation_id: str) -> str:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            state = self.get_status(activation_id)
            code = str(state.get("code") or "").strip()
            status = str(state.get("status") or "").upper()
            if code and code.lower() not in {"none", "null"}:
                return code
            if status in {"STATUS_CANCEL", "STATUS_FINISH", "STATUS_CANCELLED", "6", "8"}:
                break
            time.sleep(self.poll_interval)
        raise HeroSMSError("HeroSMS SMS wait timed out")


def _optional_float(value: str) -> float | None:
    try:
        return float(value) if str(value).strip() else None
    except (TypeError, ValueError):
        return None


def _country_entries(payload: Any):
    if isinstance(payload, dict):
        values = payload.get("countries") or payload.get("data") or payload
        if isinstance(values, dict):
            for key, item in values.items():
                if isinstance(item, dict):
                    yield key, item
                elif isinstance(item, str):
                    yield key, {"name": item}
        elif isinstance(values, list):
            for item in values:
                if isinstance(item, dict):
                    yield item.get("id") or item.get("countryCode") or item.get("code"), item
    elif isinstance(payload, list):
        for item in payload:
            if isinstance(item, dict):
                yield item.get("id") or item.get("countryCode") or item.get("code"), item


def _country_alias_match(code: str, text: str) -> bool:
    aliases = {"GB": ("UNITED KINGDOM", "ENGLAND", "英国"), "US": ("UNITED STATES", "美国"),
               "AE": ("UNITED ARAB EMIRATES", "UAE", "阿联酋"), "TW": ("TAIWAN", "台湾")}
    return any(alias in text for alias in aliases.get(code, ()))


def _parse_activation(payload: Any) -> tuple[str, str, Any]:
    if isinstance(payload, str):
        parts = payload.split(":")
        if len(parts) >= 3 and parts[0] in {"ACCESS_NUMBER", "ACCESS_NUMBER_V2"}:
            return parts[1], parts[2], None
        return "", "", None
    if isinstance(payload, dict):
        return (str(payload.get("activationId") or payload.get("activation_id") or payload.get("id") or ""),
                str(payload.get("phoneNumber") or payload.get("phone") or payload.get("number") or ""),
                payload.get("activationCost") or payload.get("cost") or payload.get("price"))
    return "", "", None


def _normalise_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return f"+{digits}" if digits else ""
