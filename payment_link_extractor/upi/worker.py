"""Private JSON-lines bridge; all payment logic remains in core.protocol."""
from __future__ import annotations

import json
import sys

from .core.protocol import NetworkDown, extract

_STAGES = {
    "checkout": "checkout", "stripe_init": "stripe_init",
    "tax_update": "taxes", "payment_method": "elements_session",
    "stripe_confirm": "payment_confirmation", "approve": "payment_confirmation",
    "instructions": "redirect_resolution",
}


def run(payload, send):
    committed = False

    def emit(message):
        nonlocal committed
        # Only known stage codes cross the boundary, never raw protocol logs.
        if str(message).startswith("stage "):
            stage = str(message)[6:].strip()
            if stage == "stripe_confirm":
                committed = True
                send({"type": "stage", "stage": "checkout_committed"})
            if stage in _STAGES:
                send({"type": "stage", "stage": _STAGES[stage]})

    try:
        result = extract(payload["account"], payload["proxy"], tries=2, emit=emit)
    except NetworkDown:
        send({"type": "result", "ok": False, "error": "network_down", "network": True, "retryable": not committed})
        return
    except Exception:
        send({"type": "result", "ok": False, "error": "runtime_failure", "retryable": False})
        return
    if not result.get("ok"):
        send({"type": "result", "ok": False, "error": result.get("error") or "no_link", "retryable": False})
        return
    send({"type": "stage", "stage": "zero_amount_confirmed"})
    send({"type": "result", "ok": True, "result": result})


def main():
    def send(message):
        print(json.dumps(message, ensure_ascii=False), flush=True)

    try:
        payload = json.loads(sys.stdin.readline())
        if not isinstance(payload, dict) or not isinstance(payload.get("account"), dict) or not payload.get("proxy"):
            raise ValueError("invalid worker input")
    except (ValueError, TypeError):
        send({"type": "result", "ok": False, "error": "invalid_input", "retryable": False})
        return 2
    run(payload, send)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
