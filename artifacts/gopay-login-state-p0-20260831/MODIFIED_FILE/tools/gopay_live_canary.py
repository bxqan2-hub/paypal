from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from payment_link_extractor.gopay_core import extract_gopay_payment_link
from payment_link_extractor.gopay_transport import normalize_proxy_url
from payment_link_extractor.auth import is_nextauth_session_cookie_name
from payment_link_extractor.models import ExtractionConfig


JWT_RE = re.compile(r"eyJ[A-Za-z0-9_\\-]+(?:\.[A-Za-z0-9_\\-]+){2}")
PROXY_RE = re.compile(
    r"(?:[A-Za-z0-9.-]+\.)+[A-Za-z]{2,}:\d{2,5}:[^:\s]+:[^:\s]+"
)
AUTH_COOKIE_NAMES = {
    "__Host-next-auth.csrf-token",
    "__Secure-next-auth.callback-url",
    "__Secure-oai-is",
    "oai-client-auth-info",
    "oai-client-session-epoch",
    "_account",
    "oai-did",
    "__stripe_mid",
    "__stripe_sid",
}


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _rollout_user_texts(path: Path) -> list[str]:
    texts: list[str] = []
    for line in path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        payload = item.get("payload") if isinstance(item, dict) else None
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "message" or payload.get("role") != "user":
            continue
        for content in payload.get("content") or []:
            if isinstance(content, dict) and content.get("type") in {
                "input_text",
                "text",
            }:
                texts.append(str(content.get("text") or ""))
    return texts


def load_tokens(path: Path) -> list[str]:
    rollout_texts = _rollout_user_texts(path)
    text = "\n".join(rollout_texts) if rollout_texts else path.read_text(
        encoding="utf-8-sig"
    )
    return _unique([item.replace("\\", "") for item in JWT_RE.findall(text)])


def load_rollout_proxies(path: Path) -> list[str]:
    texts = _rollout_user_texts(path)
    raw = _unique([item.rstrip("),.;") for item in PROXY_RE.findall("\n".join(texts))])
    return [normalize_proxy_url(item) for item in raw]


def load_browser_state(path: Path | None) -> tuple[tuple[tuple[str, str], ...], str]:
    if path is None:
        return (), ""
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("browser state must be a JSON object")
    selected: dict[str, str] = {}
    for item in payload.get("cookies") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        value = str(item.get("value") or "").strip()
        if (
            is_nextauth_session_cookie_name(name)
            or name in AUTH_COOKIE_NAMES
        ) and value:
            selected[name] = value
    attestation = str(
        payload.get("oai-web-deployment-attestation")
        or payload.get("gopay_deployment_attestation")
        or ""
    ).strip()
    cookies = tuple(sorted(selected.items()))
    if len(attestation) < 64:
        raise ValueError("browser state is missing deployment attestation")
    return cookies, attestation


def _safe_error(exc: BaseException) -> dict[str, Any]:
    status = getattr(exc, "status_code", None)
    try:
        status = int(status) if status is not None else None
    except (TypeError, ValueError):
        status = None
    message = str(exc).lower()
    if "browser session account does not match" in message:
        category = "browser_session_account_mismatch"
    elif "browser session account verification is unavailable" in message:
        category = "browser_session_binding_unavailable"
    elif "access token is missing the browser user identity" in message:
        category = "access_token_identity_missing"
    elif "browser checkout readiness" in message:
        category = "browser_session_incomplete"
    elif "attestation" in message:
        category = "attestation_missing"
    elif "sentinel proof generation" in message or "sentinel provider" in message:
        category = "sentinel_browser"
    elif any(item in message for item in ("timeout", "connect", "proxy", "ssl")):
        category = "network"
    elif "not_eligible" in message or "not eligible" in message:
        category = "promo_not_eligible"
    elif "blocked" in message:
        category = "approve_blocked"
    else:
        category = "protocol"
    result = {
        "type": type(exc).__name__,
        "status_code": status,
        "failure_mode": str(getattr(exc, "failure_mode", "") or ""),
        "category": category,
    }
    safe_context = getattr(exc, "safe_context", None)
    if isinstance(safe_context, dict):
        result["safe_context"] = safe_context
    causes: list[str] = []
    current = exc.__cause__ or exc.__context__
    while current is not None and len(causes) < 5:
        causes.append(type(current).__name__)
        current = current.__cause__ or current.__context__
    if causes:
        result["cause_types"] = causes
    return result


def _provider_shape(value: str) -> dict[str, Any]:
    parsed = urlsplit(str(value or ""))
    return {
        "present": bool(parsed.scheme in {"http", "https"} and parsed.netloc),
        "host": parsed.hostname or "",
        "path_prefix": "/".join(parsed.path.split("/")[:3]),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one GoPay account canary without retrying after Checkout commit"
    )
    parser.add_argument("--tokens-file", type=Path, required=True)
    parser.add_argument("--proxy-rollout", type=Path, required=True)
    parser.add_argument("--token-index", type=int, required=True, help="one-based token slot")
    parser.add_argument("--start-proxy-slot", type=int, default=1)
    parser.add_argument(
        "--browser-state-file",
        type=Path,
        help="runtime-only JSON containing browser auth cookies and attestation",
    )
    parser.add_argument(
        "--precommit-retries",
        type=int,
        default=3,
        help="same-proxy retries for browser/network failures before Checkout POST",
    )
    args = parser.parse_args()

    tokens = load_tokens(args.tokens_file)
    proxies = load_rollout_proxies(args.proxy_rollout)
    try:
        browser_cookies, browser_attestation = load_browser_state(
            args.browser_state_file
        )
    except (OSError, ValueError, json.JSONDecodeError):
        print(json.dumps({"status": "input_error", "browser_state": False}))
        return 2
    if not 1 <= args.token_index <= len(tokens):
        print(json.dumps({"status": "input_error", "token_count": len(tokens)}))
        return 2
    if not proxies:
        print(json.dumps({"status": "input_error", "proxy_count": 0}))
        return 2

    token = tokens[args.token_index - 1]
    start = max(1, int(args.start_proxy_slot)) - 1
    attempts: list[dict[str, Any]] = []
    for proxy_index in range(start, len(proxies)):
        proxy = proxies[proxy_index]
        config = ExtractionConfig(
            access_token=token,
            checkout_proxy=proxy,
            update_proxy=proxy,
            country="ID",
            payment_method="gopay",
            apply_checkout_update=True,
            gopay_zero_trial_validation=True,
            verbose=False,
            retry_count=0,
            checkout_proxy_attempts=(proxy,),
            update_proxy_attempts=(proxy,),
            proxy_pool=(proxy,),
            gopay_session_cookies=browser_cookies,
            gopay_deployment_attestation=browser_attestation,
        )
        for precommit_try in range(1, max(1, args.precommit_retries) + 1):
            stages: list[str] = []
            committed = False

            def stage_callback(stage: str) -> None:
                nonlocal committed
                normalized = str(stage)
                stages.append(normalized)
                if normalized == "checkout_committed":
                    committed = True
                print(
                    f"CANARY_STAGE token_slot={args.token_index} "
                    f"proxy_slot={proxy_index + 1} precommit_try={precommit_try} "
                    f"stage={normalized}",
                    flush=True,
                )

            try:
                result = extract_gopay_payment_link(
                    config, stage_callback=stage_callback
                )
            except Exception as exc:
                error = _safe_error(exc)
                attempt = {
                    "proxy_slot": proxy_index + 1,
                    "precommit_try": precommit_try,
                    "committed": committed,
                    "last_stage": stages[-1] if stages else "not_started",
                    "error": error,
                }
                attempts.append(attempt)
                print(
                    "CANARY_ATTEMPT="
                    + json.dumps(attempt, separators=(",", ":")),
                    flush=True,
                )
                if committed or error["status_code"] == 401:
                    final = {
                        "status": (
                            "consumed_failure" if committed else "auth_failure"
                        ),
                        "token_slot": args.token_index,
                        "attempts": attempts,
                    }
                    print(
                        "CANARY_RESULT="
                        + json.dumps(final, separators=(",", ":")),
                        flush=True,
                    )
                    return 20 if committed else 21
                retry_same_proxy = (
                    attempt["last_stage"] == "checkout"
                    and error["category"] in {"sentinel_browser", "network"}
                    and precommit_try < max(1, args.precommit_retries)
                )
                if retry_same_proxy:
                    continue
                break

            data = result.to_dict()
            provider = str(
                data.get("gopay_url") or data.get("provider_url") or ""
            )
            final = {
                "status": "success",
                "token_slot": args.token_index,
                "proxy_slot": proxy_index + 1,
                "checkout_committed": committed,
                "amount_due_minor": data.get("amount_due_minor"),
                "currency": data.get("currency"),
                "provider": _provider_shape(provider),
                "stages": stages,
            }
            print(
                "CANARY_RESULT=" + json.dumps(final, separators=(",", ":")),
                flush=True,
            )
            return 0

    final = {
        "status": "no_checkout_committed",
        "token_slot": args.token_index,
        "attempts": attempts,
    }
    print("CANARY_RESULT=" + json.dumps(final, separators=(",", ":")), flush=True)
    return 22


if __name__ == "__main__":
    raise SystemExit(main())
