from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from payment_link_extractor.application import (
    _normalize_config,
    _should_apply_checkout_update,
    extract_payment_link,
)
from payment_link_extractor.auth import (
    account_email,
    account_id,
    extract_access_token,
    extract_session_token,
    normalize_access_token,
)
from payment_link_extractor.checkout import create_checkout
from payment_link_extractor.config import billing_for_country, country_for_payment_method
from payment_link_extractor.errors import ProtocolError
from payment_link_extractor.flows.oaics import (
    _custom_action_url,
    continue_custom_checkout_method,
    extract_oaics_provider,
    fetch_custom_checkout_data_state,
)
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor import transport
from payment_link_extractor.web.app import create_app
from payment_link_extractor.web.routes import _config_from_payload, _credential_value


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


def test_session_token_is_preserved_from_account_export() -> None:
    session = "JWE.fixture.part.one.two"
    payload = {
        "accessToken": "access-fixture",
        "sessionToken": session,
        "checkout_proxy": "http://proxy.example:8080",
        "payment_method": "gcash",
    }
    assert extract_session_token(payload) == session
    config = _config_from_payload(payload)
    assert config.session_token == session


def test_session_token_can_be_read_from_cookie_header() -> None:
    session = "JWE.cookie.part.one.two"
    assert extract_session_token({"cookies": f"oai-did=x; __Secure-next-auth.session-token={session}"}) == session


def test_gcash_embedded_promo_skips_legacy_update_requirement() -> None:
    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="US",
        payment_method="gcash",
        apply_checkout_update=True,
    )
    normalized = _normalize_config(config)
    assert normalized.country == "PH"
    assert _should_apply_checkout_update(normalized) is False
    route_config = _config_from_payload(
        {
            "access_token": "token",
            "checkout_proxy": "http://proxy.example:8080",
            "update_proxy": "",
            "country": "US",
            "payment_method": "gcash",
            "apply_checkout_update": True,
        }
    )
    assert route_config.country == "PH"


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
        self.headers: dict[str, str] = {}

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


class _ChatGPTRouteData(_ChatGPT):
    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        if ".data?_routes=routes%2Fcheckout.%24entity.%24checkoutId" in url:
            self.calls.append((method, url, kwargs))
            return _Response(
                200,
                {"loaderData": {"checkout_session": {"custom_payment_methods": [{"id": "cpmt_gcash", "name": "GCash"}]}}},
            )
        return super().request(method, url, **kwargs)


class _ScriptResponse(_Response):
    def __init__(self, status_code: int, text: str) -> None:
        super().__init__(status_code, {})
        self.text = text

    def json(self) -> dict[str, object]:
        raise ValueError("text/x-script fixture")


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
        "/payments/checkout/taxes",
        "/payments/checkout/confirm",
        "/payments/checkout/custom_payment_method/start",
    ]
    taxes = next(call for call in chatgpt.calls if call[1].endswith("/payments/checkout/taxes"))
    assert taxes[2]["json"]["tax_id"] is None
    assert taxes[2]["json"]["billing_address"]["line2"] == ""


def test_application_gcash_does_not_call_legacy_coupon_or_update() -> None:
    chatgpt = _ChatGPTRouteData()

    class Factory:
        def chatgpt(self, config: ExtractionConfig, proxy: str) -> _ChatGPT:
            return chatgpt

        def stripe(self, config: ExtractionConfig) -> SimpleNamespace:
            return SimpleNamespace()

    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="US",
        payment_method="gcash",
        apply_checkout_update=True,
    )
    result = extract_payment_link(config, transport_factory=Factory())
    assert result.payment_method == "gcash"
    paths = [call[1].split("chatgpt.com", 1)[-1] for call in chatgpt.calls]
    assert "/backend-api/promo_campaign/check_coupon" not in " ".join(paths)
    assert "/backend-api/payments/checkout/update" not in paths
    assert chatgpt.calls[0][2]["json"]["promo_campaign"]["promo_campaign_id"] == "plus-1-month-free"


def test_gcash_route_data_hydrates_method_before_tax_refresh() -> None:
    chatgpt = _ChatGPTRouteData()
    checkout = {
        "cs_id": "oaics_fixture",
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
    assert result["payment_method_id"] == "cpmt_gcash"
    assert "/checkout/openai_ie/oaics_fixture.data?_routes=routes%2Fcheckout.%24entity.%24checkoutId" in chatgpt.calls[0][1]
    assert chatgpt.calls[0][2]["headers"]["Referer"] == "https://chatgpt.com/?promo_campaign=plus-1-month-free"
    assert [call[1].rsplit("/backend-api", 1)[-1] for call in chatgpt.calls[1:]] == [
        "/payments/checkout/taxes",
        "/payments/checkout/taxes",
        "/payments/checkout/confirm",
        "/payments/checkout/custom_payment_method/start",
    ]


def test_gcash_route_data_parser_accepts_script_embedded_json() -> None:
    class ScriptTransport:
        def request(self, method: str, url: str, **kwargs: object) -> _ScriptResponse:
            assert method == "GET"
            assert ".data?_routes=routes%2Fcheckout.%24entity.%24checkoutId" in url
            return _ScriptResponse(
                200,
                'self.__next_f.push([1,"{\\"checkout_session\\":{\\"custom_payment_methods\\":[{\\"id\\":\\"cpmt_gcash\\"}]}}"] )',
            )

    checkout = {"cs_id": "oaics_script", "billing_country": "PH", "processor_entity": "openai_ie"}
    payload = fetch_custom_checkout_data_state(ScriptTransport(), checkout, None)
    assert payload["checkout_session"]["custom_payment_methods"][0]["id"] == "cpmt_gcash"
    assert checkout["custom_payment_methods"][0]["id"] == "cpmt_gcash"


class _ChatGPTLegacyOnly(_ChatGPT):
    def request(self, method: str, url: str, **kwargs: object) -> _Response:
        self.calls.append((method, url, kwargs))
        if ".data?_routes=routes%2Fcheckout.%24entity.%24checkoutId" in url:
            return _Response(404, {})
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
    assert chatgpt.calls[0][1].endswith(
        "/checkout/openai_ie/oaics_legacy.data?_routes=routes%2Fcheckout.%24entity.%24checkoutId"
    )
    assert [call[1].rsplit("/backend-api", 1)[-1] for call in chatgpt.calls[1:]] == [
        "/payments/checkout/openai_ie/oaics_legacy",
        "/payments/checkout/taxes",
        "/payments/checkout/taxes",
        "/payments/checkout/confirm",
        "/payments/checkout/custom_payment_method/start",
    ]


def test_account_id_extracts_browser_auth_claim() -> None:
    payload = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-fixture"}}
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    assert account_id(f"header.{encoded}.signature") == "acct-fixture"


def test_import_recognizes_opaque_at_and_session_export_metadata() -> None:
    opaque = "OpaqueATFixture_abcdefghijklmnopqrstuvwxyz0123456789"
    session_export = f'{opaque}","rumViewTags":{{"light_account":{{"fetched":false}}}}}}'
    escaped_session_export = f'{opaque.replace("_", "\\_")}","rumViewTags":{{"light_account":{{"fetched":false}}}}}}'

    assert extract_access_token(session_export) == opaque
    assert extract_access_token(escaped_session_export) == opaque
    assert normalize_access_token({"AT": opaque}) == opaque
    assert _credential_value({"credential": {"accessToken": opaque}}) == opaque


def test_import_extracts_jwt_before_trailing_session_metadata() -> None:
    payload = base64.urlsafe_b64encode(json.dumps({"email": "fixture@example.com"}).encode()).decode().rstrip("=")
    jwt = f"header.{payload}.signature_fixture"
    session_export = f'{jwt.replace("_", "\\_")}","authProvider":"openai'

    assert extract_access_token(session_export) == jwt
    assert account_email(session_export) == "fixture@example.com"


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
    assert "chatgpt-account-id" not in session.headers
    assert "x-oai-is-pending-updates" not in session.headers
    assert session.openai_account_id == "acct-fixture"
    assert session.headers["oai-client-build-number"] == "9723596"
    assert session.headers["x-oai-is-client-observation"] == "v1.r.p.observation-fixture"
    assert session.openai_sentinel_token == "sentinel-fixture"
    assert session.openai_sentinel_so_token == "so-fixture"
    initial = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout"
    )
    assert "chatgpt-account-id" not in initial
    dynamic = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout/taxes"
    )
    assert dynamic["chatgpt-account-id"] == "acct-fixture"


def test_transport_matches_current_gcash_locale_and_rotates_observation(monkeypatch) -> None:
    class Session:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}
            self.calls: list[dict[str, object]] = []

        def request(self, method: str, url: str, **kwargs: object) -> _Response:
            self.calls.append(kwargs)
            return _Response(200, {"ok": True})

    session = Session()
    monkeypatch.setattr(transport, "new_session", lambda: session)
    monkeypatch.setenv("OPLL_GCASH_SENTINEL_BROWSER", "off")
    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="PH",
        payment_method="gcash",
        apply_checkout_update=False,
    )
    built = transport.DefaultTransportFactory().chatgpt(config, config.checkout_proxy)
    assert built is session
    assert session.headers["oai-language"] == "en-US"
    assert "Chrome/151" in session.headers["User-Agent"]
    transport.stage_http_request(
        session,
        "fixture checkout",
        "POST",
        "https://chatgpt.com/backend-api/payments/checkout",
        None,
        headers={},
    )
    first = session.calls[-1]["headers"]["x-oai-is-client-observation"]
    assert session.calls[-1]["headers"]["oai-telemetry"] == "[1,null]"
    transport.stage_http_request(
        session,
        "fixture taxes",
        "POST",
        "https://chatgpt.com/backend-api/payments/checkout/taxes",
        None,
        headers={},
    )
    second = session.calls[-1]["headers"]["x-oai-is-client-observation"]
    assert first != second
    assert "oai-telemetry" not in session.calls[-1]["headers"]


def test_sentinel_headers_prefer_fresh_provider_flow() -> None:
    class Provider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def headers(self, flow: str, *, referer: str = "") -> dict[str, str]:
            self.calls.append((flow, referer))
            return {
                "OpenAI-Sentinel-Token": "fresh-token",
                "oai-web-deployment-attestation": "fresh-attestation",
            }

    provider = Provider()
    session = SimpleNamespace(openai_sentinel_provider=provider)
    headers = transport.openai_sentinel_headers(
        session,
        flow="checkout_session_approval",
        referer="https://chatgpt.com/checkout/fixture",
    )
    assert headers["OpenAI-Sentinel-Token"] == "fresh-token"
    assert headers["oai-web-deployment-attestation"] == "fresh-attestation"
    assert provider.calls == [("checkout_session_approval", "https://chatgpt.com/checkout/fixture")]


def test_sentinel_version_loader_and_frame_url_are_pinned() -> None:
    loader = "export{a};const u='/sentinel/20260810913b/sdk.js';"
    assert transport._sentinel_version_from_loader(loader) == "20260810913b"
    assert transport._sentinel_frame_url("20260810913b").endswith(
        "/backend-api/sentinel/frame.html?sv=20260810913b"
    )
    assert transport._sentinel_version_from_loader("no sdk here") == ""
    assert transport._sentinel_frame_url("bad?sv=inject").endswith("/sentinel/frame.html")


def test_browser_sentinel_captures_only_bootstrap_metadata() -> None:
    provider = object.__new__(transport.BrowserSentinelProvider)
    provider._attestation = ""
    provider._bootstrap_session_id = ""
    expressions: list[str] = []

    def fake_eval(expression: str, timeout: float = 75.0) -> object:
        expressions.append(expression)
        return {
            "attestation": "fixture-attestation",
            "locale": "en-US",
            "sessionId": "fixture-bootstrap-session",
        }

    provider._eval = fake_eval
    assert provider._capture_bootstrap(1.0) is True
    assert provider._attestation == "fixture-attestation"
    assert provider._bootstrap_session_id == "fixture-bootstrap-session"
    assert "accessToken" not in expressions[0]


def test_browser_sentinel_opens_authenticated_page_before_versioned_frame(monkeypatch) -> None:
    provider = object.__new__(transport.BrowserSentinelProvider)
    provider.binary = "fixture-agent-browser"
    provider.access_token = "fixture-token"
    provider.device_id = "fixture-device"
    provider.session_id = "fixture-session"
    provider._started = False
    provider._failed = False
    provider._launch_args_used = False
    provider._attestation = ""
    provider.session_token = "fixture-session-cookie"
    events: list[object] = []
    monkeypatch.setenv("OPLL_GCASH_SENTINEL_BROWSER", "auto")

    def fake_run(args: list[str], timeout: float = 75.0) -> object:
        events.append(tuple(args))
        return {}

    def fake_capture(wait_seconds: float = 0.0) -> bool:
        provider._attestation = "fixture-attestation"
        events.append("bootstrap")
        return True

    provider._run = fake_run
    provider._capture_bootstrap = fake_capture
    provider._resolve_sentinel_version = lambda: "20260810913b"
    provider._wait_for_sentinel_sdk = lambda wait_seconds=10.0: True
    provider._sync_cookies = lambda: events.append("cookies")
    provider._start()

    auth_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple)
        and "--headers" in event
        and "https://chatgpt.com/?promo_campaign=plus-1-month-free" in event
    )
    frame_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, tuple) and any("frame.html?sv=20260810913b" in item for item in event)
    )
    assert auth_index < frame_index


def test_browser_sentinel_sets_nextauth_cookie_before_bootstrap(monkeypatch) -> None:
    provider = object.__new__(transport.BrowserSentinelProvider)
    provider.binary = "fixture-agent-browser"
    provider.access_token = "fixture-token"
    provider.session_token = "fixture-session-cookie"
    provider.device_id = "fixture-device"
    provider.session_id = "fixture-session"
    provider._started = False
    provider._failed = False
    provider._launch_args_used = False
    provider._attestation = ""
    events: list[tuple[object, ...]] = []
    monkeypatch.setenv("OPLL_GCASH_SENTINEL_BROWSER", "auto")
    provider._run = lambda args, timeout=75.0: events.append(tuple(args)) or {}
    provider._capture_bootstrap = lambda wait_seconds=0.0: setattr(provider, "_attestation", "att") or True
    provider._resolve_sentinel_version = lambda: "20260810913b"
    provider._wait_for_sentinel_sdk = lambda wait_seconds=10.0: True
    provider._sync_cookies = lambda: None
    provider._start()
    cookie_index = next(i for i, event in enumerate(events) if "__Secure-next-auth.session-token" in event)
    promo_index = next(
        i
        for i, event in enumerate(events)
        if any("promo_campaign=plus-1-month-free" in str(item) for item in event)
    )
    assert cookie_index < promo_index


def test_browser_sentinel_resolves_version_from_live_loader_shape() -> None:
    provider = object.__new__(transport.BrowserSentinelProvider)
    provider._sentinel_sdk_version = ""
    provider._eval = lambda expression, timeout=75.0: (
        '(()=>import("/sentinel/20260810913b/sdk.js"))()'
    )
    assert provider._resolve_sentinel_version() == "20260810913b"
    assert provider._sentinel_sdk_version == "20260810913b"


def test_strict_gcash_sentinel_does_not_silently_fall_back(monkeypatch) -> None:
    class Provider:
        def headers(self, flow: str, *, referer: str = "") -> dict[str, str]:
            raise RuntimeError("fixture browser startup failed")

    monkeypatch.delenv("OPLL_GCASH_SENTINEL_ALLOW_FALLBACK", raising=False)
    session = SimpleNamespace(
        openai_sentinel_provider=Provider(),
        openai_sentinel_strict=True,
    )
    with pytest.raises(transport.ConfigurationError, match="GCash Sentinel bootstrap failed"):
        transport.openai_sentinel_headers(session, flow="chatgpt_checkout")


def test_local_vendor_bridge_is_started_and_verified(monkeypatch) -> None:
    import iprocket_chain_bridge

    calls: list[str] = []

    class Probe:
        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(
        iprocket_chain_bridge,
        "ensure_background_server",
        lambda: calls.append("ensure") or True,
    )
    monkeypatch.setattr(
        transport.socket,
        "create_connection",
        lambda address, timeout=0.25: calls.append(f"probe:{address[1]}") or Probe(),
    )
    transport._ensure_iprocket_bridge_listener(
        "http://iprb_fixture:fixture@127.0.0.1:18796"
    )
    transport._ensure_iprocket_bridge_listener("http://proxy.example:8080")
    assert calls == ["ensure", "probe:18796", "close"]


def test_iprocket_61999_uses_socks5_gateway_protocol() -> None:
    assert transport._iprocket_protocol(61999) == "socks5"


def test_browser_sentinel_generates_token_before_ping() -> None:
    provider = object.__new__(transport.BrowserSentinelProvider)
    provider._lock = transport.threading.RLock()
    provider._failed = False
    provider._started = True
    provider._attestation = ""
    provider._cookies = ""
    provider.transport_session = SimpleNamespace(headers={})
    events: list[str] = []

    def fake_eval(expression: str, timeout: float = 75.0) -> object:
        if "SentinelSDK.token" in expression:
            events.append("token")
            return {"flow": "checkout_session_approval", "id": "fixture-device"}
        events.append("ping")
        return 200

    provider._eval = fake_eval
    headers = provider.headers(
        "checkout_session_approval",
        referer="https://chatgpt.com/checkout/fixture",
    )
    assert events == ["token", "ping"]
    assert json.loads(headers["OpenAI-Sentinel-Token"])["flow"] == "checkout_session_approval"


def test_browser_sentinel_syncs_only_first_party_deduplicated_cookies() -> None:
    provider = object.__new__(transport.BrowserSentinelProvider)
    provider.device_id = "fixture-device"
    provider.transport_session = SimpleNamespace(headers={})
    provider._cookies = ""
    provider._run = lambda args: {
        "data": {
            "cookies": [
                {"name": "oai-did", "value": "stale-device", "domain": ".chatgpt.com"},
                {"name": "cf_clearance", "value": "old", "domain": ".chatgpt.com"},
                {"name": "cf_clearance", "value": "fresh", "domain": "chatgpt.com"},
                {"name": "stripe_mid", "value": "foreign", "domain": ".stripe.com"},
            ]
        }
    }
    provider._sync_cookies()
    assert provider._cookies == "oai-did=fixture-device; cf_clearance=fresh"
    assert provider.transport_session.headers["Cookie"] == provider._cookies


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
        "promo_campaign": {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        },
        "checkout_ui_mode": "custom",
    }
    assert call[2]["headers"]["Referer"] == "https://chatgpt.com/?promo_campaign=plus-1-month-free"
    assert len(json.dumps(call[2]["json"], separators=(",", ":"))) == 245
    assert call[2]["headers"]["OpenAI-Sentinel-Token"] == "sentinel-fixture"
    assert call[2]["headers"]["OpenAI-Sentinel-SO-Token"] == "so-fixture"


def test_gcash_checkout_failure_records_status_edge_and_proof_state() -> None:
    class FailingChatGPT(_ChatGPTWithSentinel):
        def request(self, method: str, url: str, **kwargs: object) -> _Response:
            response = _Response(403, {"detail": "unusual activity"})
            response.headers["cf-ray"] = "fixture-ray-MNL"
            return response

    config = ExtractionConfig(
        access_token="token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="",
        country="PH",
        payment_method="gcash",
        apply_checkout_update=False,
    )
    with pytest.raises(ProtocolError) as caught:
        create_checkout(config, FailingChatGPT(), None)
    message = str(caught.value)
    assert "HTTP 403" in message
    assert "edge=MNL" in message
    assert "sentinel=yes" in message
    assert "attestation=no" in message


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
