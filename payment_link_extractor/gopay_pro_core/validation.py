from __future__ import annotations

"""Offline GoPay Checkout response validation and batch diagnostics."""

import json
from collections import Counter
from typing import Any, Iterable

from ..checkout import (
    all_values_by_key,
    checkout_session_kind,
    classify_checkout_create_failure,
    extract_checkout_session_id,
    merge_payment_method_values,
)


def _payload(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def summarize_checkout_sample(status_code: int, payload: Any) -> dict[str, Any]:
    """Normalize one recorded response without retaining credentials."""
    decoded = _payload(payload)
    session_id = extract_checkout_session_id(decoded)
    methods = merge_payment_method_values(
        *all_values_by_key(decoded, "payment_methods"),
        *all_values_by_key(decoded, "payment_method_types"),
        *all_values_by_key(decoded, "custom_payment_methods"),
    )
    failure_mode = ""
    retryable = False
    if int(status_code) >= 400:
        failure_mode, retryable = classify_checkout_create_failure(
            int(status_code), payload if isinstance(payload, str) else json.dumps(decoded, default=str)
        )
    return {
        "status_code": int(status_code),
        "ok": int(status_code) < 400 and bool(session_id),
        "checkout_session_id": session_id,
        "session_kind": checkout_session_kind(session_id),
        "payment_methods": methods,
        "failure_mode": failure_mode,
        "retryable": retryable,
    }


def validate_checkout_batch(samples: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate sanitized success families, methods, and failure modes."""
    rows = [
        summarize_checkout_sample(
            int(sample.get("status_code", 0)), sample.get("payload")
        )
        for sample in samples
    ]
    return {
        "sample_count": len(rows),
        "success_count": sum(bool(row["ok"]) for row in rows),
        "session_kinds": dict(Counter(row["session_kind"] for row in rows if row["session_kind"])),
        "failure_modes": dict(Counter(row["failure_mode"] for row in rows if row["failure_mode"])),
        "payment_methods": merge_payment_method_values(
            *(row["payment_methods"] for row in rows)
        ),
        "rows": rows,
    }
