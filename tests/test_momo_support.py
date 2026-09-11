"""Lean MoMo checks for the 9999-aligned, Chrome-146-coherent core.

The heavy per-internal tests were retired together with the old fat MoMo
modules (gateway querySession polling, proxy-pool eligibility, agent-browser
Sentinel, browser-profile rotation).  These tests cover the surface that the
lean core actually exposes and guard the two fixes that unblocked it:
a single coherent Chrome 146 identity and no post-link gateway gate.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote, unquote, urlsplit

import pytest

from payment_link_extractor.channels import payment_channel
from payment_link_extractor.errors import ProtocolError
from payment_link_extractor.models import ExtractionConfig


def _cfg() -> ExtractionConfig:
    return ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="http://proxy.example:8080",
        update_proxy="http://proxy.example:8080",
        country="VN",
        payment_method="momo",
    )


def test_momo_registration() -> None:
    channel = payment_channel("momo")
    assert channel.adapter_module == "payment_link_extractor.momo"
    assert channel.adapter_callable == "extract_momo_payment_link"
    assert channel.result_field == "momo_url"
    assert (channel.country, channel.currency) == ("VN", "VND")

    from payment_link_extractor.momo import MOMO_RESULT_FIELD, extract_momo_payment_link

    assert MOMO_RESULT_FIELD == "momo_url"
    assert callable(extract_momo_payment_link)


def test_momo_is_pinned_to_a_coherent_chrome146_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import iprocket_chain_bridge as bridge
    from payment_link_extractor.momo import _transport as transport

    monkeypatch.setattr(bridge, "ensure_background_server", Mock(return_value=True))
    # The HTTP fingerprint, UA and client hints are all Chrome 146.  The
    # Sentinel proof browser (._sentinel_runner) is likewise 146; presenting a
    # <=150 request while minting the proof in system Chrome 151/152 is what
    # produced status=blocked before.
    assert transport.PAYMENT_BROWSER_IMPERSONATE == "chrome146"
    assert "Chrome/146." in transport.PAYMENT_BROWSER_USER_AGENT
    assert '"146"' in transport.PAYMENT_BROWSER_SEC_CH_UA

    session = transport.DefaultTransportFactory().chatgpt(_cfg(), "http://proxy.example:8080")
    try:
        headers = {str(k).lower(): str(v) for k, v in session.headers.items()}
        assert "Chrome/146." in headers.get("user-agent", "")
        assert '"146"' in headers.get("sec-ch-ua", "")
        assert headers.get("authorization") == "Bearer fixture-token"
        assert headers.get("oai-language") == "vi-VN"
        assert headers.get("oai-client-build-number") == "9999461"
    finally:
        transport.safe_close(session)


@pytest.mark.parametrize("scheme, protocol", [
    ("http", "http"), ("https", "https"), ("socks5", "socks5"), ("socks5h", "socks5"),
])
@pytest.mark.parametrize("authenticated", [True, False])
def test_momo_generic_proxy_and_sentinel_share_one_bridge(monkeypatch, scheme, protocol, authenticated):
    import iprocket_chain_bridge as bridge
    from payment_link_extractor.momo import _sentinel_client as sentinel, _transport as transport

    ensure = Mock(return_value=True)
    monkeypatch.setattr(bridge, "ensure_background_server", ensure)
    monkeypatch.setenv("IPROCKET_CHAIN_PROXY", "http://127.0.0.1:18796")
    password = "PASS_SECRET@:%40" if authenticated else ""
    credentials = f"USER_SECRET:{quote(password, safe='')}@" if authenticated else ""
    session = SimpleNamespace(proxies={})
    transport.set_proxy_url(session, f"{scheme}://{credentials}proxy.example:3010")

    mapped = session.proxies["https"]
    parsed = urlsplit(mapped)
    assert (parsed.scheme, parsed.hostname, parsed.port) == ("http", "127.0.0.1", 18796)
    encoded = parsed.username.removeprefix("iprb_")
    metadata = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    assert metadata == f"{protocol}|proxy.example|3010|{'USER_SECRET' if authenticated else ''}"
    assert unquote(parsed.password) == password
    assert session.proxies["http"] == mapped
    assert sentinel._current_proxy(session) == mapped
    assert transport.normalize_proxy_url(mapped) == mapped
    ensure.assert_called_once_with()


def test_momo_arxlabs_transport_and_shared_probe_use_identical_bridge(monkeypatch):
    import iprocket_chain_bridge as bridge
    from payment_link_extractor.momo import _sentinel_client as sentinel, _transport as transport
    from payment_link_extractor.web import proxy_probe

    ensure = Mock(return_value=True)
    monkeypatch.setattr(bridge, "ensure_background_server", ensure)
    monkeypatch.setenv("IPROCKET_CHAIN_PROXY", "http://127.0.0.1:18796")
    raw = "sg.arxlabs.io:3010:USER_SECRET:PASS_SECRET@:%40"
    session = SimpleNamespace(proxies={})
    transport.set_proxy_url(session, raw)
    request_get = Mock(return_value=SimpleNamespace(
        status_code=200, json=lambda: {"ip": "192.0.2.5", "country_code": "VN"},
    ))
    location = proxy_probe.probe_proxy(raw, request_get=request_get)

    assert location.ip == "192.0.2.5"
    assert request_get.call_args.kwargs["proxies"] == session.proxies
    assert sentinel._current_proxy(session) == session.proxies["https"]
    assert unquote(urlsplit(session.proxies["https"]).password) == "PASS_SECRET@:%40"
    assert ensure.call_count == 2


def test_momo_empty_proxy_does_not_start_bridge(monkeypatch):
    import iprocket_chain_bridge as bridge
    from payment_link_extractor.momo import _transport as transport

    ensure = Mock(return_value=True)
    monkeypatch.setattr(bridge, "ensure_background_server", ensure)
    session = SimpleNamespace(proxies={"http": "previous"})
    transport.set_proxy_url(session, "")
    assert session.proxies == {}
    ensure.assert_not_called()


def test_momo_config_helpers() -> None:
    from payment_link_extractor.momo import _config as config

    assert config.normalize_payment_method("momo") == "momo"
    with pytest.raises(Exception):
        config.normalize_payment_method("paypal")
    assert config.currency_minor_scale("VND") == 0
    assert config.processor_entity_for_country("VN") == "openai_ie"

    billing = config.billing_for_country("VN", "momo")
    assert billing.country == "VN"
    assert billing.email


def test_sentinel_build_and_flow_guard() -> None:
    from payment_link_extractor.momo import _sentinel_client as sentinel

    assert sentinel.uses_current_sentinel("momo") is True
    assert sentinel.SDK_BUILD == "20260810913b"
    # An unsupported flow is rejected before any browser subprocess is spawned.
    with pytest.raises(ProtocolError):
        sentinel.mint_sentinel_token(object(), "unsupported_flow")


def test_sentinel_runner_script_is_spawnable() -> None:
    from payment_link_extractor.momo import _sentinel_client as sentinel

    # Every protected MoMo call spawns this script.  A wrong filename here makes
    # checkout fail with a generic 502 that no other test can see, because the
    # flow tests stub payment_sentinel_headers out.
    assert sentinel.RUNNER_SCRIPT.is_file(), sentinel.RUNNER_SCRIPT


def test_checkout_carries_vn_trial_campaign(monkeypatch: pytest.MonkeyPatch) -> None:
    from payment_link_extractor.momo import _flow as flow

    # The checkout POST is Sentinel-protected; stub the proof so the test is
    # offline and browser-free.
    monkeypatch.setattr(flow, "payment_sentinel_headers", lambda *a, **k: {})

    calls: list[tuple[str, str, dict]] = []

    class FakeSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}
            self.proxies: dict[str, str] = {}

        def request(self, method: str, url: str, **kwargs):
            calls.append((method, url, kwargs))
            return SimpleNamespace(
                status_code=200,
                text="",
                json=lambda: {"checkout_session_id": "oaics_fixture"},
            )

    checkout = flow._momo_checkout(_cfg(), FakeSession(), None)
    assert checkout["cs_id"] == "oaics_fixture"

    posts = [c for c in calls if c[0] == "POST" and c[1].endswith("/backend-api/payments/checkout")]
    assert posts, "checkout POST was not issued"
    body = posts[0][2]["json"]
    assert body["promo_campaign"] == {
        "promo_campaign_id": "plus-1-month-free",
        "is_coupon_from_query_param": False,
    }
    assert body["subscription_data"]["trial_period_days"] == 30
    assert body["checkout_ui_mode"] == "custom"
    assert body["billing_details"] == {"country": "VN", "currency": "VND"}
    assert body["price_interval"] == "month"


def test_lean_flow_has_no_gateway_poll_or_eligibility_storm() -> None:
    import importlib
    import inspect

    from payment_link_extractor.momo import _flow

    source = inspect.getsource(_flow)
    # The fatal post-link gate and its retry knob must be gone.
    assert "querySession" not in source
    assert "require_redirect" not in source
    # The old proxy-pool eligibility module must no longer exist.
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("payment_link_extractor.momo.momo_eligibility")
