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

# HeroSMS uses short service identifiers on the SMS-Activate-compatible
# endpoint.  The website label is "PayPal", but the API identifier is `ts`.
# Keep accepting the human-readable value from existing settings and normalize
# it before a purchase request is sent.
_SERVICE_ALIASES = {
    "paypal": "ts",
    "pay pal": "ts",
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
        self._services_by_country: dict[int, list[dict[str, str]]] = {}

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
        except httpx.HTTPStatusError as exc:
            body = exc.response.text.strip().replace("\n", " ")[:300]
            detail = f"HTTP {exc.response.status_code}"
            if body:
                detail += f": {body}"
            raise HeroSMSError(f"HeroSMS request failed: {detail}") from exc
        except httpx.RequestError as exc:
            raise HeroSMSError("HeroSMS request failed: network error") from exc
        except httpx.HTTPError as exc:
            raise HeroSMSError("HeroSMS request failed: HTTP client error") from exc
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

    def get_services(self, country_id: int, *, force: bool = False) -> list[dict[str, str]]:
        """Return the current service catalogue for one HeroSMS country."""
        country_key = int(country_id)
        if not force and country_key in self._services_by_country:
            return self._services_by_country[country_key]
        payload = self._request("getServicesList", country=country_key, lang="en")
        services = _service_entries(payload)
        if not services:
            raise HeroSMSError(f"HeroSMS returned no services for country {country_key}")
        self._services_by_country[country_key] = services
        return services

    def resolve_service_code(self, service: str | None, country_id: int) -> str:
        """Resolve a UI service label/code to the identifier accepted by HeroSMS."""
        requested = str(service or self.default_service or "").strip()
        if not requested:
            raise HeroSMSError("HeroSMS service is not configured")
        alias = _SERVICE_ALIASES.get(requested.casefold())
        if alias:
            return alias
        requested_key = _normalise_service_name(requested)
        try:
            services = self.get_services(country_id)
        except HeroSMSError:
            # Preserve support for an explicitly supplied API code if the
            # catalogue endpoint is temporarily unavailable; the purchase
            # request will still receive a useful sanitized API error.
            return requested
        for item in services:
            code = str(item.get("code") or "").strip()
            name = str(item.get("name") or "").strip()
            if requested.casefold() == code.casefold() or requested_key == _normalise_service_name(name):
                return code
        raise HeroSMSError(
            f"HeroSMS service unavailable for country {country_id}: {requested}"
        )

    def acquire_number(
        self, country: str, *, max_price: float | None = None, service: str | None = None
    ) -> dict[str, Any]:
        country_code = str(country or "").strip().upper()
        country_id = self.resolve_country_id(country_code)
        service_code = self.resolve_service_code(service, country_id)
        price = self.default_max_price if max_price is None else max_price
        number_params: dict[str, Any] = {
            "service": service_code,
            "country": country_id,
        }
        if price is not None:
            # HeroSMS documents fixedPrice as an alias used only when
            # maxPrice is absent. Sending both makes getNumberV2 reject the
            # request with HTTP 422.
            number_params["maxPrice"] = price
        payload = self._request(
            "getNumberV2", **number_params,
        )
        activation_id, phone, cost = _parse_activation(payload)
        if not activation_id or not phone:
            raise HeroSMSError("HeroSMS returned no activation number")
        return {
            "activation_id": str(activation_id), "phone": _normalise_phone(phone),
            "country": country_code, "country_id": country_id,
            "service": service_code, "price": cost,
        }

    def get_status(self, activation_id: str) -> dict[str, Any]:
        payload = self._request("getStatusV2", id=str(activation_id))
        if isinstance(payload, str):
            parts = payload.split(":", 1)
            return {"status": parts[0], "code": parts[1] if len(parts) == 2 else "", "raw": payload}
        if isinstance(payload, dict):
            result = dict(payload)
            result.setdefault("status", result.get("state") or result.get("statusCode") or "")
            code = _find_sms_code(result)
            result.setdefault("code", code)
            if not result.get("code"):
                result["code"] = code
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


def _normalise_service_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _service_entries(payload: Any) -> list[dict[str, str]]:
    values = payload.get("services") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        return []
    entries: list[dict[str, str]] = []
    for item in values:
        if not isinstance(item, dict):
            continue
        code = str(item.get("code") or item.get("service") or "").strip()
        name = str(item.get("name") or item.get("title") or "").strip()
        if code:
            entries.append({"code": code, "name": name})
    return entries


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


def _find_sms_code(payload: Any) -> str:
    """Extract a verification code from all HeroSMS response variants."""
    if isinstance(payload, dict):
        for key in (
            "code", "smsCode", "verificationCode", "sms_code", "verification_code",
            "messageCode", "message_code", "otp", "pin",
        ):
            value = payload.get(key)
            if value not in (None, ""):
                found = _find_sms_code(value)
                if found:
                    return found
        for key in (
            "sms", "call", "data", "message", "messages", "activation",
            "activations", "result", "results", "items",
        ):
            value = payload.get(key)
            if value in (None, ""):
                continue
            found = _find_sms_code(value)
            if found:
                return found
        return ""
    if isinstance(payload, (list, tuple)):
        for value in payload:
            found = _find_sms_code(value)
            if found:
                return found
        return ""
    text = str(payload or "").strip()
    if not text or text.casefold() in {"none", "null"}:
        return ""
    if ":" in text:
        text = text.rsplit(":", 1)[-1].strip()
    return text if re.fullmatch(r"\d{4,12}", text) else ""


def _normalise_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", str(phone or ""))
    return f"+{digits}" if digits else ""
