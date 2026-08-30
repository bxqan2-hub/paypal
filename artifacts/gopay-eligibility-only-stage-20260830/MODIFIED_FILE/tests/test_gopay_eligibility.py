from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from payment_link_extractor import gopay_eligibility
from payment_link_extractor.errors import ConfigurationError
from payment_link_extractor.models import ExtractionConfig


def _config() -> ExtractionConfig:
    pool = tuple(f"http://proxy-{index}.example:8080" for index in range(1, 5))
    return ExtractionConfig(
        access_token="fixture-token",
        checkout_proxy=pool[0],
        update_proxy=pool[0],
        country="ID",
        payment_method="gopay",
        proxy_pool=pool,
        verbose=False,
    )


def test_standalone_gopay_eligibility_stops_at_first_eligible_proxy(monkeypatch) -> None:
    config = _config()
    plan = config.proxy_pool
    sessions: list[SimpleNamespace] = []
    configs: list[ExtractionConfig] = []

    class Factory:
        def chatgpt(self, attempt_config, proxy):
            assert attempt_config.checkout_proxy == proxy
            assert attempt_config.update_proxy == proxy
            assert attempt_config.proxy_pool == (proxy,)
            configs.append(attempt_config)
            session = SimpleNamespace(proxy=proxy, closed=False)
            session.close = lambda: setattr(session, "closed", True)
            sessions.append(session)
            return session

        def stripe(self, _config):
            raise AssertionError("eligibility-only flow must not initialize Stripe")

    states = iter(("not_eligible", "ineligible", "eligible", "eligible"))

    def fake_probe(_config, _session, _log):
        state = next(states)
        return {
            "coupon": "plus-1-month-free",
            "http_status": 200,
            "state": state,
            "eligible": state == "eligible",
        }

    monkeypatch.setattr(gopay_eligibility, "_randomized_proxy_plan", lambda _config: plan)
    monkeypatch.setattr(gopay_eligibility, "probe_coupon_eligibility", fake_probe)
    stages: list[str] = []
    result = gopay_eligibility.probe_gopay_zero_trial_eligibility(
        config,
        transport_factory=Factory(),
        stage_callback=stages.append,
    )

    assert result == {
        "ok": True,
        "eligible": True,
        "state": "eligible",
        "coupon": "plus-1-month-free",
        "country": "ID",
        "currency": "IDR",
        "attempt": 3,
        "max_attempts": 4,
        "proxy_slot": 3,
        "source": "chatgpt_check_coupon",
        "probes": [
            {"attempt": 1, "proxy_slot": 1, "http_status": 200, "state": "not_eligible", "eligible": False, "coupon": "plus-1-month-free"},
            {"attempt": 2, "proxy_slot": 2, "http_status": 200, "state": "ineligible", "eligible": False, "coupon": "plus-1-month-free"},
            {"attempt": 3, "proxy_slot": 3, "http_status": 200, "state": "eligible", "eligible": True, "coupon": "plus-1-month-free"},
        ],
    }
    assert len(configs) == 3
    assert all(session.closed for session in sessions)
    assert stages == [
        "eligibility_proxy:1",
        "eligibility_proxy:2",
        "eligibility_proxy:3",
        "eligibility_confirmed",
    ]


def test_standalone_gopay_eligibility_returns_verified_ineligible_without_checkout(monkeypatch) -> None:
    config = _config()

    class Factory:
        def chatgpt(self, _config, proxy):
            return SimpleNamespace(proxy=proxy, close=lambda: None)

        def stripe(self, _config):
            raise AssertionError("eligibility-only flow must not initialize Stripe")

    monkeypatch.setattr(
        gopay_eligibility,
        "_randomized_proxy_plan",
        lambda _config: config.proxy_pool[:2],
    )
    monkeypatch.setattr(
        gopay_eligibility,
        "probe_coupon_eligibility",
        lambda *_args: {
            "coupon": "plus-1-month-free",
            "http_status": 200,
            "state": "ineligible",
            "eligible": False,
        },
    )

    result = gopay_eligibility.probe_gopay_zero_trial_eligibility(
        config, transport_factory=Factory()
    )
    assert result["ok"] is True
    assert result["eligible"] is False
    assert result["state"] == "ineligible"
    assert result["attempt"] == 2
    assert result["proxy_slot"] is None
    assert len(result["probes"]) == 2


def test_standalone_gopay_eligibility_rejects_malformed_jwt_before_using_proxy() -> None:
    config = _config()
    config = replace(config, access_token="header.eA.signature")

    class ForbiddenFactory:
        def chatgpt(self, _config, _proxy):
            raise AssertionError("malformed AT must stop before opening a proxy session")

    with pytest.raises(ConfigurationError, match="AT payload is invalid"):
        gopay_eligibility.probe_gopay_zero_trial_eligibility(
            config, transport_factory=ForbiddenFactory()
        )
