from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

from payment_link_extractor.application import _normalize_config
from payment_link_extractor.auth import account_id
from payment_link_extractor.checkout import create_checkout
from payment_link_extractor.config import billing_for_country, country_for_payment_method
from payment_link_extractor.flows.oaics import (
    _custom_action_url,
    continue_custom_checkout_method,
    extract_oaics_provider,
)
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor import transport
from payment_link_extractor.web.app import create_app
from payment_link_extractor.web.routes import _config_from_payload


ROOT = Path(__file__).resolve().parents[1]


def test_gcash_forces_ph_country_in_route_and_worker_config() -> None:
    payload = {
        "access_token": "token",
        "checkout_proxy": "http://proxy.example:8080",
        "update_proxy": "http://proxy.example:8081",
        "country": "US",
        "payment_method": "gcash",
        "apply_checkout_update": False,
    }
    route_config = _config_from_payload(payload)
    assert route_config.country == "PH"
    assert country_for_payment_method("gcash", "US") == "PH"

    normalized = _normalize_config(route_config)
    assert normalized.country == "PH"
    assert billing_for_country(normalized.country).country == "PH"


def test_defaults_expose_paypal_and_gcash_payment_choices() -> None:
    app = create_app({"TESTING": True})
    response = app.test_client().get("/api/defaults", headers={"X-Workbench-Password": "test-password"})
    assert response.status_code == 200
    data = response.get_json()
    assert [(item["value"], item["label"]) for item in data["payment_methods"]] == [
        ("paypal", "PayPal"),
        ("gcash", "GCash"),
    ]
    assert data["payment_method_countries"] == {"gcash": "PH"}


class _Response:
    def __init__(self, status_code: int, payload: dict[str, object]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload


class _ChatGPT:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url, kwargs))
        if url.endswith("/payments/checkout"):
            return _Response(
                200,
                {
                    "checkout_session_id": "oaics_fixture",
                    "processor_entity": "openai_ie",
                },
            )
        if url.endswith("/payments/checkout/taxes"):
            return _Response(200, {"checkout_session": {"custom_payment_methods": [{"id": "cpmt_gcash"}]}})
        if "/payments/checkout/openai_ie/oaics_fixture" in url:
            return _Response(200, {"custom_payment_methods": [{"id": "cpmt_gcash", "name": "GCash"}]})
        if url.endswith("/payments/checkout/confirm"):
            return _Response(200, {"status": "success", "confirm_return_url": "https://chatgpt.com/checkout/verify"})
        if url.endswith("/payments/checkout/custom_payment_method/start"):
            return _Response(200, {"status": "requires_action", "next_action": {"url": "https://gcash.com/pay/fixture", "paymentMethodType": "gcash"}})
        raise AssertionError(f"unexpected request: {method} {url}")


class _ChatGPTWithSentinel(_ChatGPT):
    def __init__(self) -> None:
        super().__init__()
        self.headers = {
            "OpenAI-Sentinel-Token": "sentinel-fixture",
            "OpenAI-Sentinel-SO-Token": "so-fixture",
        }


def test_oaics_gcash_uses_custom_method_confirm_and_start() -> None:
    chatgpt = _ChatGPT()
    checkout = {
        "cs_id": "oaics_fixture",
        "session_kind": "openai_custom_checkout",
        "billing_country": "PH",
        "currency": "PHP",
        "processor_entity": "openai_ie",
        "custom_payment_methods": [{"id": "cpmt_gcash", "name": "GCash"}],
    }
    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="PH",
        payment_method="gcash",
        apply_checkout_update=False,
    )
    result = extract_oaics_provider(
        config,
        chatgpt,
        SimpleNamespace(),
        checkout,
        billing_for_country("PH").to_dict(),
        None,
    )
    assert result["gcash_url"] == "https://gcash.com/pay/fixture"
    assert result["payment_method_id"] == "cpmt_gcash"
    assert [call[1].rsplit("/backend-api", 1)[-1] for call in chatgpt.calls] == [
        "/payments/checkout/taxes",
        "/payments/checkout/confirm",
        "/payments/checkout/custom_payment_method/start",
    ]
    taxes = next(call for call in chatgpt.calls if call[1].endswith("/payments/checkout/taxes"))
    assert taxes[2]["json"]["tax_id"] is None
    assert taxes[2]["json"]["billing_address"]["line2"] == ""


class _ChatGPTLegacyOnly(_ChatGPT):
    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url, kwargs))
        if url.endswith("/payments/checkout/taxes"):
            return _Response(200, {"checkout_session": {"currency": "PHP"}})
        if "/payments/checkout/openai_ie/oaics_legacy" in url:
            return _Response(200, {"custom_payment_methods": [{"id": "cpmt_gcash"}]})
        if url.endswith("/payments/checkout/confirm"):
            return _Response(200, {"status": "success"})
        if url.endswith("/payments/checkout/custom_payment_method/start"):
            return _Response(
                200,
                {
                    "status": "requires_action",
                    "next_action": {"url": "https://gcash.com/pay/legacy"},
                },
            )
        raise AssertionError(f"unexpected request: {method} {url}")


def test_gcash_legacy_state_read_is_fallback_only() -> None:
    chatgpt = _ChatGPTLegacyOnly()
    checkout = {
        "cs_id": "oaics_legacy",
        "session_kind": "openai_custom_checkout",
        "billing_country": "PH",
        "currency": "PHP",
        "processor_entity": "openai_ie",
    }
    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="PH",
        payment_method="gcash",
        apply_checkout_update=False,
    )
    result = extract_oaics_provider(
        config,
        chatgpt,
        SimpleNamespace(),
        checkout,
        billing_for_country("PH").to_dict(),
        None,
    )
    assert result["gcash_url"].endswith("/legacy")
    assert [call[1].rsplit("/backend-api", 1)[-1] for call in chatgpt.calls] == [
        "/payments/checkout/openai_ie/oaics_legacy",
        "/payments/checkout/taxes",
        "/payments/checkout/confirm",
        "/payments/checkout/custom_payment_method/start",
    ]


def test_account_id_extracts_browser_auth_claim() -> None:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-fixture"}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    assert account_id(f"header.{encoded}.signature") == "acct-fixture"


def test_transport_builds_har_identity_and_browser_headers(monkeypatch) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}

    session = Session()
    monkeypatch.setattr(transport, "new_session", lambda: session)
    monkeypatch.setenv("OPLL_OPENAI_SENTINEL_TOKEN", "sentinel-fixture")
    monkeypatch.setenv("OPLL_OPENAI_SENTINEL_SO_TOKEN", "so-fixture")
    monkeypatch.setenv("OPLL_OAI_IS_CLIENT_OBSERVATION", "v1.r.p.observation-fixture")
    token_payload = {
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "acct-fixture",
        }
    }
    encoded = base64.urlsafe_b64encode(json.dumps(token_payload).encode()).decode().rstrip("=")
    config = ExtractionConfig(
        access_token=f"header.{encoded}.signature",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="PH",
        payment_method="gcash",
        apply_checkout_update=False,
    )
    built = transport.DefaultTransportFactory().chatgpt(config, config.checkout_proxy)
    assert built is session
    assert session.headers["chatgpt-account-id"] == "acct-fixture"
    assert session.headers["oai-client-build-number"] == "9723596"
    assert session.headers["x-oai-is-client-observation"] == "v1.r.p.observation-fixture"
    assert session.openai_sentinel_token == "sentinel-fixture"
    assert session.openai_sentinel_so_token == "so-fixture"


def test_gcash_confirm_attaches_optional_har_sentinel_header() -> None:
    chatgpt = _ChatGPTWithSentinel()
    checkout = {
        "cs_id": "oaics_fixture",
        "billing_country": "PH",
        "processor_entity": "openai_ie",
    }
    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="PH",
        payment_method="gcash",
        apply_checkout_update=False,
    )
    # Reuse the public flow so the fake records the exact header contract.
    result = extract_oaics_provider(
        config,
        chatgpt,
        SimpleNamespace(),
        {
            **checkout,
            "session_kind": "openai_custom_checkout",
            "currency": "PHP",
            "custom_payment_methods": [{"id": "cpmt_gcash", "name": "GCash"}],
        },
        billing_for_country("PH").to_dict(),
        None,
    )
    assert result["gcash_url"].endswith("/fixture")
    confirm = next(call for call in chatgpt.calls if call[1].endswith("/payments/checkout/confirm"))
    assert confirm[2]["headers"]["OpenAI-Sentinel-Token"] == "sentinel-fixture"
    assert confirm[2]["headers"]["OpenAI-Sentinel-SO-Token"] == "so-fixture"


def test_gcash_initial_checkout_matches_har_sentinel_contract() -> None:
    chatgpt = _ChatGPTWithSentinel()
    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="PH",
        payment_method="gcash",
        apply_checkout_update=False,
    )
    checkout = create_checkout(config, chatgpt, None)
    assert checkout["cs_id"] == "oaics_fixture"
    call = chatgpt.calls[0]
    assert call[2]["json"] == {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": "PH", "currency": "PHP"},
        "cancel_url": "https://chatgpt.com/",
        "checkout_ui_mode": "custom",
        "check_card_proxy": True,
    }
    assert call[2]["headers"]["OpenAI-Sentinel-Token"] == "sentinel-fixture"
    assert call[2]["headers"]["OpenAI-Sentinel-SO-Token"] == "so-fixture"


def test_gcash_action_url_accepts_redirect_to_url_shape() -> None:
    assert _custom_action_url({"next_action": {"redirect_to_url": {"url": "https://adyen.test/pay"}}}) == "https://adyen.test/pay"


def test_gcash_continue_callback_adds_checkout_session_id() -> None:
    chatgpt = _ChatGPT()
    checkout = {"cs_id": "oaics_fixture", "billing_country": "PH", "processor_entity": "openai_ie"}
    # The fake does not have a continue response in the old fixture, so add a
    # narrow request-only transport that mirrors the captured JSON endpoint.
    original = chatgpt.request

    def request(method: str, url: str, **kwargs: object) -> _Response:
        if url.endswith("/custom_payment_method/continue"):
            assert kwargs["json"] == {"redirect_result": "fixture", "checkout_session_id": "oaics_fixture"}
            return _Response(200, {"status": "succeeded"})
        return original(method, url, **kwargs)

    chatgpt.request = request  # type: ignore[method-assign]
    payload = continue_custom_checkout_method(
        chatgpt,
        checkout,
        {"redirect_result": "fixture"},
        None,
    )
    assert payload["status"] == "succeeded"


def test_extractor_ui_contains_gcash_fixed_country_behavior() -> None:
    html = (ROOT / "payment_link_extractor/web/templates/index.html").read_text(encoding="utf-8")
    javascript = (ROOT / "payment_link_extractor/web/static/app.js").read_text(encoding="utf-8")
    assert '<option value="gcash">GCash（菲律宾）</option>' in html
    assert 'id="country-field"' in html
    assert 'id="gcash-country-note"' in html
    assert 'country.value = "PH"' in javascript
    assert 'paymentMethod === "gcash" ? "PH"' in javascript
