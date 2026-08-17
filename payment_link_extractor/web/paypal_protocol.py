"""Mount the upstream PayPal agreement protocol inside the existing Flask app.

The upstream protocol package is copied unchanged under ``paypal_agreement_protocol``.
This module is only an HTTP adapter: it translates a Flask request into the
upstream ``WebHandler`` contract and translates the captured response back into
Werkzeug's response object.  No protocol flow code is changed here.
"""
from __future__ import annotations

import importlib
import io
import json
import math
from pathlib import Path
import sys
import threading
import time
from typing import Any

from flask import Response, jsonify, request

from paypal_agreement_protocol.herosms import HeroSMSClient, HeroSMSError


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROTOCOL_ROOT = _PROJECT_ROOT / "paypal_agreement_protocol"
if str(_PROTOCOL_ROOT) not in sys.path:
    # The upstream source intentionally keeps its ``config`` and ``paypal``
    # imports absolute.  Put only the copied integration directory on the
    # import path; the extraction package remains untouched.
    sys.path.insert(0, str(_PROTOCOL_ROOT))

_protocol = importlib.import_module("paypal_agreement_protocol.web")
_SMS_CLIENT: HeroSMSClient | None = None
_SMS_WATCHERS: dict[str, threading.Thread] = {}
_SMS_WATCHERS_LOCK = threading.RLock()


def _parse_sms_max_price(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        price = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_price must be numeric") from exc
    if not math.isfinite(price) or price < 0:
        raise ValueError("max_price must be a finite non-negative number")
    return round(price, 2)


def _sms_client() -> HeroSMSClient:
    global _SMS_CLIENT
    if _SMS_CLIENT is None:
        _SMS_CLIENT = HeroSMSClient()
    return _SMS_CLIENT


class _HeaderMap:
    def __init__(self, values: dict[str, str]) -> None:
        self._values = {str(key).lower(): str(value) for key, value in values.items()}

    def get(self, key: str, default: str = "") -> str:
        return self._values.get(str(key).lower(), default)


class _CaptureHandler(_protocol.WebHandler):
    """A socket-free WebHandler used by the Flask bridge."""

    def send_response(self, code: int, message: str | None = None) -> None:  # noqa: D401
        self._response_status = int(code)

    def send_header(self, keyword: str, value: Any) -> None:
        self._response_headers[str(keyword)] = str(value)

    def end_headers(self) -> None:
        return None

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.send_response(code)
        body = (message or "HTTP error").encode("utf-8")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def dispatch_protocol_request(inner_path: str, method: str, body: bytes = b"") -> Response:
    """Dispatch one ``/paypal-pay`` request without opening another port."""

    query = request.query_string.decode("utf-8", errors="replace")
    path = inner_path or "/"
    if query:
        path = f"{path}?{query}"
    headers = {key: value for key, value in request.headers.items()}
    headers.setdefault("Host", request.host)
    headers.setdefault("Content-Length", str(len(body)))
    headers.setdefault("Content-Type", "application/json" if body else "")

    handler = object.__new__(_CaptureHandler)
    handler.path = path
    handler.headers = _HeaderMap(headers)
    handler.client_address = (request.remote_addr or "127.0.0.1", 0)
    handler.rfile = io.BytesIO(body)
    handler.wfile = io.BytesIO()
    handler._response_status = 200
    handler._response_headers = {}
    handler._set_device_cookie = ""

    if method.upper() == "GET":
        handler.do_GET()
    elif method.upper() == "POST":
        handler.do_POST()
    else:
        handler.send_error(405, "Method Not Allowed")

    payload = handler.wfile.getvalue()
    response_headers = dict(handler._response_headers)
    # WebHandler already emits a Content-Length.  Ensure Flask can return a
    # valid response even when a future upstream branch omits it.
    response_headers.setdefault("Content-Length", str(len(payload)))
    return Response(payload, status=handler._response_status, headers=response_headers)


def _watch_sms_job(job_id: str, activation: dict[str, Any]) -> None:
    activation_id = str(activation.get("activation_id") or "")
    client = _sms_client()
    submitted = False
    deadline = time.monotonic() + client.timeout
    try:
        while time.monotonic() < deadline:
            job = _protocol.get_job(job_id)
            if job is None or job.status in {"completed", "failed", "cancelled"}:
                break
            if job.status == "awaiting_otp":
                if not submitted:
                    try:
                        code = client.wait_for_code(activation_id)
                        job.submit_input(code)
                        submitted = True
                    except (HeroSMSError, ValueError):
                        break
            else:
                submitted = False
            time.sleep(client.poll_interval)
    finally:
        client.finish(activation_id, 6)
        with _SMS_WATCHERS_LOCK:
            _SMS_WATCHERS.pop(job_id, None)


def _start_sms_watchers(payload: dict[str, Any], activations: list[dict[str, Any]]) -> None:
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), list) else [payload.get("job")]
    activation_by_index = {
        int(item.get("index") or position): item
        for position, item in enumerate(activations, start=1)
        if isinstance(item, dict) and str(item.get("activation_id") or "").strip()
    }
    for position, item in enumerate(jobs, start=1):
        if not isinstance(item, dict) or not item.get("id"):
            continue
        activation = activation_by_index.get(int(item.get("batch_index") or position))
        if activation is None:
            continue
        job_id = str(item["id"])
        with _SMS_WATCHERS_LOCK:
            if job_id in _SMS_WATCHERS:
                continue
            thread = threading.Thread(target=_watch_sms_job, args=(job_id, dict(activation)), name=f"herosms-{job_id}", daemon=True)
            _SMS_WATCHERS[job_id] = thread
            thread.start()


def register_paypal_protocol(app: Any) -> None:
    """Register ``/paypal-pay`` and delegate all protocol work to the source."""

    @app.route("/paypal-pay", methods=["GET", "POST"])
    @app.route("/paypal-pay/", defaults={"protocol_path": ""}, methods=["GET", "POST"])
    @app.route("/paypal-pay/<path:protocol_path>", methods=["GET", "POST"])
    def paypal_protocol(protocol_path: str = "") -> Response:
        if protocol_path == "api/sms/number" and request.method == "POST":
            data = request.get_json(silent=True) or {}
            try:
                max_price = _parse_sms_max_price(data.get("max_price"))
                client = _sms_client()
                activation = client.acquire_number(
                    str(data.get("country") or ""),
                    max_price=max_price,
                    service=str(data.get("service") or "") or None,
                )
                returned_price = activation.get("price")
                if max_price is not None and returned_price not in (None, ""):
                    actual_price = float(returned_price)
                    if not math.isfinite(actual_price) or actual_price > max_price + 1e-9:
                        client.finish(str(activation.get("activation_id") or ""), 6)
                        return jsonify({
                            "ok": False,
                            "error": f"HeroSMS price {actual_price:.2f} exceeds max_price {max_price:.2f}",
                            "max_price": max_price,
                            "price": actual_price,
                        }), 422
                return jsonify({"ok": True, "max_price": max_price, **activation})
            except (HeroSMSError, ValueError) as exc:
                return jsonify({"ok": False, "error": str(exc)}), 503
        if protocol_path == "api/sms/status" and request.method == "POST":
            data = request.get_json(silent=True) or {}
            raw_ids = data.get("activation_ids")
            if raw_ids is None:
                raw_ids = [data.get("activation_id")]
            if not isinstance(raw_ids, list):
                return jsonify({"ok": False, "error": "activation_ids must be a list"}), 400
            activation_ids = [str(value or "").strip() for value in raw_ids if str(value or "").strip()]
            if not activation_ids:
                return jsonify({"ok": False, "error": "activation_ids is required"}), 400
            if len(activation_ids) > 20:
                return jsonify({"ok": False, "error": "activation_ids supports at most 20 items"}), 400
            client = _sms_client()
            terminal_statuses = {"STATUS_CANCEL", "STATUS_CANCELLED", "STATUS_FINISH", "6", "8"}
            results = []
            for activation_id in activation_ids:
                try:
                    status = client.get_status(activation_id)
                    status_name = str(status.get("status") or "").strip().upper()
                    results.append({
                        "activation_id": activation_id,
                        "status": status_name,
                        "code": str(status.get("code") or "").strip(),
                        "active": status_name not in terminal_statuses,
                    })
                except HeroSMSError as exc:
                    results.append({
                        "activation_id": activation_id,
                        "status": "UNKNOWN",
                        "code": "",
                        "active": False,
                        "error": str(exc),
                    })
            return jsonify({"ok": True, "activations": results})
        if protocol_path == "api/sms/cancel" and request.method == "POST":
            data = request.get_json(silent=True) or {}
            activation_id = str(data.get("activation_id") or data.get("id") or "").strip()
            if not activation_id:
                return jsonify({"ok": False, "error": "activation_id is required"}), 400
            _sms_client().finish(activation_id, 6)
            return jsonify({"ok": True, "activation_id": activation_id, "status": 6})
        inner_path = "/" + protocol_path if protocol_path else "/"
        body = request.get_data(cache=False)
        activations: list[dict[str, Any]] = []
        if protocol_path == "api/jobs" and request.method == "POST":
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
                raw_activations = parsed.get("sms_activations")
                if isinstance(raw_activations, list):
                    activations = [
                        {
                            "index": int(item.get("index") or index),
                            "activation_id": str(item.get("activation_id") or "").strip(),
                        }
                        for index, item in enumerate(raw_activations, start=1)
                        if isinstance(item, dict) and str(item.get("activation_id") or "").strip()
                    ]
                activation_id = str(parsed.get("sms_activation_id") or "").strip()
                if activation_id and not activations:
                    activations = [{"index": 1, "activation_id": activation_id}]
            except (TypeError, ValueError, UnicodeDecodeError):
                activations = []
        response = dispatch_protocol_request(inner_path, request.method, body)
        if activations and response.status_code in {200, 201}:
            try:
                _start_sms_watchers(json.loads(response.get_data(as_text=True)), activations)
            except (ValueError, TypeError):
                pass
        return response
