from __future__ import annotations

import json

from payment_link_extractor import gopay_transport
from payment_link_extractor.models import ExtractionConfig


class Session:
    def __init__(self, *_args, **_kwargs) -> None:
        self.headers: dict[str, str] = {}
        self.proxies: dict[str, str] = {}


def gopay_session(monkeypatch) -> Session:
    monkeypatch.setattr(gopay_transport, "new_session", Session)
    monkeypatch.setenv("OPLL_SENTINEL_BROWSER", "off")
    config = ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy="",
        update_proxy="",
        country="ID",
        payment_method="gopay",
    )
    return gopay_transport.GoPayTransportFactory().chatgpt(config, "")


def test_empty_gopay_observation_env_generates_initial_value(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_GOPAY_OAI_IS_CLIENT_OBSERVATION", "")

    session = gopay_session(monkeypatch)

    assert session.headers["x-oai-is-client-observation"].startswith("v1.r.p.")


def test_empty_gopay_checkout_telemetry_env_uses_runtime_value(monkeypatch) -> None:
    monkeypatch.setenv("OPLL_GOPAY_OAI_CHECKOUT_TELEMETRY", "")
    session = gopay_session(monkeypatch)
    session.openai_checkout_telemetry = "[1,627.5,21,23,28,2,0,631]"

    headers = session.refresh_openai_request_headers(
        "POST", "https://chatgpt.com/backend-api/payments/checkout"
    )

    assert json.loads(headers["oai-telemetry"]) == [1, 627.5, 21, 23, 28, 2, 0, 631]
