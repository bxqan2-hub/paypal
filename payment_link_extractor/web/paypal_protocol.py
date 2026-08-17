"""Mount the upstream PayPal agreement protocol inside the existing Flask app.

The upstream protocol package is copied unchanged under ``paypal_agreement_protocol``.
This module is only an HTTP adapter: it translates a Flask request into the
upstream ``WebHandler`` contract and translates the captured response back into
Werkzeug's response object.  No protocol flow code is changed here.
"""
from __future__ import annotations

import importlib
import io
from pathlib import Path
import sys
from typing import Any

from flask import Response, request


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROTOCOL_ROOT = _PROJECT_ROOT / "paypal_agreement_protocol"
if str(_PROTOCOL_ROOT) not in sys.path:
    # The upstream source intentionally keeps its ``config`` and ``paypal``
    # imports absolute.  Put only the copied integration directory on the
    # import path; the extraction package remains untouched.
    sys.path.insert(0, str(_PROTOCOL_ROOT))

_protocol = importlib.import_module("paypal_agreement_protocol.web")


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


def register_paypal_protocol(app: Any) -> None:
    """Register ``/paypal-pay`` and delegate all protocol work to the source."""

    @app.route("/paypal-pay", methods=["GET", "POST"])
    @app.route("/paypal-pay/", defaults={"protocol_path": ""}, methods=["GET", "POST"])
    @app.route("/paypal-pay/<path:protocol_path>", methods=["GET", "POST"])
    def paypal_protocol(protocol_path: str = "") -> Response:
        inner_path = "/" + protocol_path if protocol_path else "/"
        return dispatch_protocol_request(inner_path, request.method, request.get_data(cache=False))
