from __future__ import annotations

import base64
import json
from types import SimpleNamespace

from payment_link_extractor.application import _normalize_config
from payment_link_extractor.channels import PAYMENT_CHANNELS
from payment_link_extractor.config import billing_for_country
from payment_link_extractor.models import ExtractionConfig
from payment_link_extractor.momo_core import _gateway_session_id, query_gateway, validate_momo_amount
from payment_link_extractor.momo_stripe import validate_momo_url
from payment_link_extractor.momo_transport import _set_proxy
from payment_link_extractor.web.app import create_app
from payment_link_extractor.web.routes import _config_from_payload


def test_momo_registry_and_fixed_country() -> None:
    channel = PAYMENT_CHANNELS["momo"]
    assert channel.adapter_module == "payment_link_extractor.momo_channel"
    assert channel.result_field == "momo_url"
    assert (channel.country, channel.currency) == ("VN", "VND")
    assert channel.uses_legacy_transport is False
    assert channel.uses_checkout_update is False
    config = _normalize_config(ExtractionConfig("token", "http://proxy", "", country="US", payment_method="momo"))
    assert (config.country, config.payment_method) == ("VN", "momo")
    assert config.momo_zero_trial_validation is True


def test_momo_route_and_ui_defaults() -> None:
    config = _config_from_payload({"access_token": "token", "proxy_pool": ["http://proxy"], "payment_method": "momo", "country": "US"})
    assert (config.country, config.payment_method, config.update_proxy) == ("VN", "momo", "http://proxy")
    app = create_app({"TESTING": True})
    data = app.test_client().get("/api/defaults", headers={"X-Workbench-Password": "test-password"}).get_json()
    methods = {item["value"]: item for item in data["payment_methods"]}
    assert methods["momo"]["label"] == "MoMo"
    assert data["payment_method_countries"]["momo"] == "VN"


def test_momo_url_and_gateway_session_contract() -> None:
    url = "https://payment.momo.vn/v2/gateway/pay?t=" + base64.urlsafe_b64encode(b"MOMO_SESSION|opaque").decode().rstrip("=") + "&s=signature"
    assert validate_momo_url(url)
    assert not validate_momo_url(url.replace("payment.momo.vn", "example.com"))
    assert _gateway_session_id(url) == "MOMO_SESSION"

    class Session:
        def request(self, method, endpoint, **kwargs):
            assert method == "POST"
            assert endpoint.endswith("/querySession")
            assert kwargs["json"] == {"sessionId": "MOMO_SESSION"}
            return SimpleNamespace(status_code=200, json=lambda: {"sessionId": "MOMO_SESSION", "status_code": 1000, "redirect": False})

    assert query_gateway(Session(), url)["status_code"] == 1000


def test_momo_transport_and_result_field_are_isolated() -> None:
    source = (PAYMENT_CHANNELS["momo"].adapter_module, PAYMENT_CHANNELS["momo"].result_field)
    assert source == ("payment_link_extractor.momo_channel", "momo_url")
    assert billing_for_country("VN").country == "VN"


def test_momo_transport_normalizes_host_port_user_password() -> None:
    session = SimpleNamespace(proxies={})
    _set_proxy(session, "proxy.example:3000:user:p@ss")
    assert session.proxies["https"] == "http://user:p%40ss@proxy.example:3000"


def test_momo_zero_amount_gate_matches_gopay_behavior() -> None:
    validate_momo_amount(0)
    for value in (None, 1, -1):
        try:
            validate_momo_amount(value)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
        else:
            raise AssertionError("non-zero or missing Momo amount was accepted")
