from __future__ import annotations

import os
import hashlib
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from flask import Flask, jsonify, request

from ..channels import PAYMENT_CHANNELS, payment_channel, public_payment_channels
from ..config import (
    SUPPORTED_COUNTRIES,
    billing_dict_for_country,
    country_config,
    country_for_payment_method,
    normalize_payment_method,
)
from ..errors import ConfigurationError
from ..auth import extract_access_token, extract_session_token
from ..models import ExtractionConfig
from .proxy_probe import ProxyProbeError, probe_proxy
from .tasks import TaskManager, TaskNotFoundError, TaskStateError


def register_routes(app: Flask, manager: TaskManager) -> None:
    @app.get("/api/health")
    def health() -> Any:
        return jsonify({"ok": True, "service": "payment-link-extractor"})

    @app.get("/api/defaults")
    def defaults() -> Any:
        proxy_pool = _configured_proxy_pool()
        billing_profiles = {}
        for code in SUPPORTED_COUNTRIES:
            _, currency, locale, timezone = country_config(code)
            billing_profiles[code] = {
                **billing_dict_for_country(code),
                "currency": currency,
                "locale": locale,
                "timezone": timezone,
            }
        return jsonify(
            {
                "ok": True,
                "country": os.getenv("OPLL_COUNTRY", "DE"),
                "force_country": "",
                "payment_method": "paypal",
                "payment_methods": public_payment_channels(),
                "payment_method_countries": {
                    name: channel.country
                    for name, channel in PAYMENT_CHANNELS.items()
                    if channel.country
                },
                "checkout_proxy": proxy_pool or os.getenv("OPLL_CHECKOUT_PROXY", ""),
                "update_proxy": proxy_pool or os.getenv("OPLL_UPDATE_PROXY", ""),
                "proxy_pool": proxy_pool,
                "proxy_pool_id": hashlib.sha256(proxy_pool.encode("utf-8")).hexdigest()[:16] if proxy_pool else "",
                "proxy_source_url": os.getenv("OPLL_PROXY_SOURCE_URL", ""),
                "apply_checkout_update": _env_bool("OPLL_UPDATE_CHECKOUT", True),
                "retry_count": _retry_count_value(os.getenv("OPLL_EXTRACTION_RETRY_COUNT", "2")),
                "billing_profiles": billing_profiles,
            }
        )

    @app.get("/api/proxy/source")
    def proxy_source() -> Any:
        source_url = str(request.args.get("url") or os.getenv("OPLL_PROXY_SOURCE_URL", "")).strip()
        parsed = urlparse(source_url)
        if parsed.scheme != "https" or parsed.hostname not in {"app.iprocket.io"}:
            return _error("仅支持 IPRocket HTTPS 代理订阅链接", 400)
        try:
            req = Request(source_url, headers={"User-Agent": "Mozilla/5.0"})
            with urlopen(req, timeout=15) as response:
                body = response.read(1024 * 1024).decode("utf-8", errors="replace")
        except Exception:
            return _error("IPRocket 代理订阅读取失败", 502)
        proxies = [line.strip() for line in body.splitlines() if line.strip()]
        if not proxies:
            return _error("IPRocket 代理订阅没有返回代理", 502)
        return jsonify({"ok": True, "proxies": proxies, "count": len(proxies), "unique_count": len(set(proxies))})

    @app.get("/api/tasks")
    def list_tasks() -> Any:
        return jsonify({"ok": True, "tasks": manager.list()})

    @app.get("/api/tasks/concurrency")
    def get_task_concurrency() -> Any:
        return jsonify({"ok": True, **manager.concurrency_snapshot()})

    @app.post("/api/tasks/concurrency")
    def set_task_concurrency() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        try:
            value = int(payload.get("concurrency"))
        except (TypeError, ValueError):
            return _error("concurrency must be an integer", 400)
        manager.set_concurrency(value)
        return jsonify({"ok": True, **manager.concurrency_snapshot()})

    @app.post("/api/tasks")
    def create_task() -> Any:
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        try:
            config = _config_from_payload(payload)
            snapshot = manager.create(config)
        except (ConfigurationError, ValueError) as exc:
            return _error(str(exc), 400)
        task_id = snapshot["task_id"]
        snapshot.update(
            {
                "status_url": f"/api/tasks/{task_id}",
                "websocket_url": "/ws/tasks",
            }
        )
        return jsonify(snapshot), 202

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str) -> Any:
        snapshot = manager.get(task_id)
        if snapshot is None:
            return _error("task not found", 404)
        return jsonify(snapshot)

    @app.post("/api/proxy/test")
    def test_proxy() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        checkout_proxy = payload.get("checkout_proxy")
        if not isinstance(checkout_proxy, str):
            return _error("checkout_proxy must be a string", 400)
        try:
            location = probe_proxy(checkout_proxy)
        except ProxyProbeError as exc:
            return _error(str(exc), exc.status_code)
        return jsonify({"ok": True, **location.to_dict()})

    @app.post("/api/tasks/<task_id>/cancel")
    def cancel_task(task_id: str) -> Any:
        try:
            return jsonify(manager.cancel(task_id))
        except TaskNotFoundError:
            return _error("task not found", 404)
        except TaskStateError as exc:
            return _error(str(exc), 409)

    @app.post("/api/tasks/<task_id>/retry")
    def retry_task(task_id: str) -> Any:
        payload = request.get_json(silent=True)
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        checkout_proxy = payload.get("checkout_proxy")
        if checkout_proxy is not None and not isinstance(checkout_proxy, str):
            return _error("checkout_proxy must be a string", 400)
        update_proxy = payload.get("update_proxy")
        if update_proxy is not None and not isinstance(update_proxy, str):
            return _error("update_proxy must be a string", 400)
        try:
            proxy_pool = (
                _single_proxy_pool_values(payload.get("proxy_pool"))
                if "proxy_pool" in payload
                else None
            )
            retry_count = (
                _max_attempts_value(payload.get("max_attempts")) - 1
                if "max_attempts" in payload
                else _retry_count_value(payload.get("retry_count"))
                if "retry_count" in payload
                else None
            )
            checkout_proxy_attempts = (
                _proxy_attempt_values(payload.get("checkout_proxy_attempts"), "checkout_proxy_attempts")
                if "checkout_proxy_attempts" in payload
                else None
            )
            update_proxy_attempts = (
                _proxy_attempt_values(payload.get("update_proxy_attempts"), "update_proxy_attempts")
                if "update_proxy_attempts" in payload
                else None
            )
        except ConfigurationError as exc:
            return _error(str(exc), 400)
        if proxy_pool:
            checkout_proxy = proxy_pool[0]
            update_proxy = proxy_pool[0]
            checkout_proxy_attempts = proxy_pool
            update_proxy_attempts = proxy_pool
        try:
            snapshot = manager.retry(
                task_id,
                checkout_proxy=checkout_proxy,
                update_proxy=update_proxy,
                retry_count=retry_count,
                checkout_proxy_attempts=checkout_proxy_attempts,
                update_proxy_attempts=update_proxy_attempts,
                proxy_pool=proxy_pool,
            )
        except TaskNotFoundError:
            return _error("task not found", 404)
        except TaskStateError as exc:
            return _error(str(exc), 409)
        new_task_id = snapshot["task_id"]
        snapshot.update(
            {
                "status_url": f"/api/tasks/{new_task_id}",
                "websocket_url": "/ws/tasks",
            }
        )
        return jsonify(snapshot), 202

    @app.post("/api/tasks/<task_id>/resolve-paypal")
    def resolve_paypal_task(task_id: str) -> Any:
        try:
            return jsonify(manager.resolve_paypal(task_id))
        except TaskNotFoundError:
            return _error("task not found", 404)
        except TaskStateError as exc:
            return _error(str(exc), 409)

    @app.delete("/api/tasks/<task_id>")
    def delete_task(task_id: str) -> Any:
        try:
            return jsonify(manager.delete(task_id))
        except TaskNotFoundError:
            return _error("task not found", 404)
        except TaskStateError as exc:
            return _error(str(exc), 409)

    @app.post("/api/tasks/bulk-delete")
    def bulk_delete_tasks() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("request body must be a JSON object", 400)
        target = payload.get("target")
        status_groups = {
            "failed": {"failed", "cancelled"},
            "succeeded": {"succeeded"},
        }
        if not isinstance(target, str) or target not in status_groups:
            return _error("target must be failed or succeeded", 400)
        statuses = status_groups[target]
        return jsonify(manager.delete_by_statuses(statuses))


def _config_from_payload(payload: dict[str, Any]) -> ExtractionConfig:
    access_token = _credential_value(payload) or os.getenv("OPLL_AT", "")
    pool_lines = _configured_proxy_pool().splitlines()
    pool_first = pool_lines[0] if pool_lines else ""
    submitted_pool = _single_proxy_pool_values(payload.get("proxy_pool"))
    if not submitted_pool and pool_lines:
        submitted_pool = tuple(line.strip() for line in pool_lines if line.strip())
    if submitted_pool:
        checkout_proxy = submitted_pool[0]
        update_proxy = submitted_pool[0]
    else:
        # Backward compatibility for API/CLI callers created before the MK UI
        # migration. The browser now submits only proxy_pool.
        checkout_proxy = payload.get("checkout_proxy") or pool_first or os.getenv("OPLL_CHECKOUT_PROXY", "")
        update_proxy = payload.get("update_proxy") or pool_first or os.getenv("OPLL_UPDATE_PROXY", "")
    hcaptcha = _value(payload, "stripe_hcaptcha_token", "OPLL_STRIPE_HCAPTCHA_TOKEN")
    payment_method = str(payload.get("payment_method", os.getenv("OPLL_PAYMENT_METHOD", "paypal")) or "paypal").lower()
    if payment_method == "momo" and not str(hcaptcha or "").strip():
        hcaptcha = os.getenv("OPLL_MOMO_STRIPE_HCAPTCHA_TOKEN", "")
    country = str(_value(payload, "country", "OPLL_COUNTRY", "DE") or "DE").upper()
    apply_update = payload.get("apply_checkout_update", _env_bool("OPLL_UPDATE_CHECKOUT", True))
    gopay_zero_trial_validation = payload.get(
        "gopay_zero_trial_validation",
        _env_bool("OPLL_GOPAY_ZERO_TRIAL_VALIDATION", True),
    )
    momo_zero_trial_validation = payload.get(
        "momo_zero_trial_validation",
        _env_bool("OPLL_MOMO_ZERO_TRIAL_VALIDATION", True),
    )
    if "max_attempts" in payload:
        max_attempts = _max_attempts_value(payload.get("max_attempts"))
        retry_count = max_attempts - 1
    else:
        retry_count = _retry_count_value(
            payload.get("retry_count", os.getenv("OPLL_EXTRACTION_RETRY_COUNT", "2"))
        )
    # Accept both OAICS (oaics_*) and Stripe Checkout (cs_*) PayPal flows.
    # Old browser preferences could keep oaics_only=true and discard most
    # otherwise usable accounts before provider confirmation.
    oaics_only = False
    if not isinstance(apply_update, bool):
        raise ConfigurationError("apply_checkout_update must be boolean")
    if not isinstance(gopay_zero_trial_validation, bool):
        raise ConfigurationError("gopay_zero_trial_validation must be boolean")
    if not isinstance(momo_zero_trial_validation, bool):
        raise ConfigurationError("momo_zero_trial_validation must be boolean")
    if not str(access_token or "").strip():
        raise ConfigurationError("AT is required")
    if not str(checkout_proxy or "").strip():
        raise ConfigurationError("checkout proxy is required")
    payment_method = normalize_payment_method(payment_method)
    channel = payment_channel(payment_method)
    if (
        apply_update
        and channel.uses_checkout_update
        and not str(update_proxy or "").strip()
    ):
        raise ConfigurationError("update proxy is required")
    country = country_for_payment_method(payment_method, country)
    if country not in SUPPORTED_COUNTRIES:
        country_config(country)
    total_attempts = retry_count + 1
    if submitted_pool:
        unified_attempts = _fit_proxy_attempt_values(
            checkout_proxy, submitted_pool, total_attempts
        )
        checkout_proxy_attempts = unified_attempts
        update_proxy_attempts = unified_attempts
    else:
        checkout_proxy_attempts = _fit_proxy_attempt_values(
            checkout_proxy,
            _proxy_attempt_values(payload.get("checkout_proxy_attempts"), "checkout_proxy_attempts"),
            total_attempts,
        )
        update_proxy_attempts = _fit_proxy_attempt_values(
            update_proxy,
            _proxy_attempt_values(payload.get("update_proxy_attempts"), "update_proxy_attempts"),
            total_attempts,
        )
    return ExtractionConfig(
        access_token=str(access_token).strip(),
        checkout_proxy=str(checkout_proxy).strip(),
        update_proxy=str(update_proxy or "").strip(),
        stripe_hcaptcha_token=str(hcaptcha or "").strip(),
        country=country,
        payment_method=payment_method,
        apply_checkout_update=apply_update,
        gopay_zero_trial_validation=gopay_zero_trial_validation,
        momo_zero_trial_validation=momo_zero_trial_validation,
        verbose=False,
        oaics_only=oaics_only,
        retry_count=retry_count,
        checkout_proxy_attempts=checkout_proxy_attempts,
        update_proxy_attempts=update_proxy_attempts,
        proxy_pool=submitted_pool,
        account_name=str(payload.get("name") or "").strip(),
        account_email=str(payload.get("email") or "").strip(),
        session_token=(
            extract_session_token(payload)
            or os.getenv("OPLL_SESSION_TOKEN", "").strip()
        ),
    )


def _max_attempts_value(value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigurationError("max_attempts must be an integer between 1 and 10")
    try:
        attempts = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("max_attempts must be an integer between 1 and 10") from exc
    if attempts < 1 or attempts > 10:
        raise ConfigurationError("max_attempts must be between 1 and 10")
    return attempts


def _single_proxy_pool_values(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_values = value.splitlines()
    elif isinstance(value, list):
        raw_values = value
    else:
        raise ConfigurationError("proxy_pool must be a string or array")
    values: list[str] = []
    seen: set[str] = set()
    for item in raw_values:
        if not isinstance(item, str):
            raise ConfigurationError("proxy_pool must contain only proxy strings")
        proxy = item.strip()
        if not proxy or proxy in seen:
            continue
        if len(proxy) > 1024 or any(character.isspace() for character in proxy):
            raise ConfigurationError("invalid proxy in proxy_pool")
        seen.add(proxy)
        values.append(proxy)
        if len(values) > 100:
            raise ConfigurationError("proxy_pool supports at most 100 proxies")
    return tuple(values)


def _retry_count_value(value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigurationError("retry_count must be an integer between 0 and 10")
    try:
        retry_count = int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("retry_count must be an integer between 0 and 10") from exc
    if retry_count < 0 or retry_count > 10:
        raise ConfigurationError("retry_count must be between 0 and 10")
    return retry_count


def _proxy_attempt_values(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigurationError(f"{field} must be an array of proxy strings")
    values: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ConfigurationError(f"{field} must contain only non-empty proxy strings")
        values.append(item.strip())
    return tuple(values)


def _fit_proxy_attempt_values(
    primary: str,
    candidates: tuple[str, ...],
    total_attempts: int,
) -> tuple[str, ...]:
    primary = str(primary or "").strip()
    values = list(candidates)
    if primary and (not values or values[0] != primary):
        values.insert(0, primary)
    if not values:
        return ()
    seed = tuple(values)
    while len(values) < total_attempts:
        values.append(seed[len(values) % len(seed)])
    return tuple(values[:total_attempts])


def _value(payload: dict[str, Any], key: str, env_key: str, default: str = "") -> Any:
    value = payload.get(key)
    return value if value is not None else os.getenv(env_key, default)


def _credential_value(payload: dict[str, Any]) -> str:
    return extract_access_token(payload)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _configured_proxy_pool() -> str:
    file_name = os.getenv("OPLL_PROXY_POOL_FILE", "").strip()
    if not file_name:
        return ""
    try:
        content = Path(file_name).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        return ""
    return "\n".join(line.strip() for line in content.splitlines() if line.strip())


def _error(message: str, status_code: int) -> Any:
    return jsonify({"ok": False, "error": message}), status_code
