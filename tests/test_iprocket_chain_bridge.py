from __future__ import annotations

import base64
from unittest.mock import Mock, call

import pytest

import iprocket_chain_bridge as bridge


@pytest.mark.parametrize("protocol", ["socks5", "auto"])
def test_two_socks_hops_keep_remote_authentication_and_dns_in_order(monkeypatch, protocol):
    sock = Mock()
    sock.recv.side_effect = [
        b"\x05\x00", b"\x05\x00\x00\x01", bytes(6),
        b"\x05\x02", b"\x01\x00", b"\x05\x00\x00\x01", bytes(6),
    ]
    connect = Mock(return_value=sock)
    monkeypatch.setattr(bridge.socket, "create_connection", connect)
    result = bridge.open_chain("target.example", 443, (protocol, "proxy.example", 3010, "USER", "PASS"))
    connect.assert_called_once_with((bridge.LOCAL_SOCKS_HOST, bridge.LOCAL_SOCKS_PORT), timeout=15)
    assert sock.sendall.call_args_list == [
        call(b"\x05\x01\x00"),
        call(b"\x05\x01\x00\x03\x0dproxy.example" + (3010).to_bytes(2, "big")),
        call(b"\x05\x01\x02"),
        call(b"\x01\x04USER\x04PASS"),
        call(b"\x05\x01\x00\x03\x0etarget.example\x01\xbb"),
    ]
    assert result is sock
    sock.settimeout.assert_has_calls([call(30), call(None)])


@pytest.mark.parametrize("protocol", ["http", "https"])
def test_http_upstream_connect_follows_first_socks_hop(monkeypatch, protocol):
    events = []
    sock = Mock()
    sock.sendall.side_effect = events.append
    sock.recv.side_effect = [
        b"\x05\x00", b"\x05\x00\x00\x01", bytes(6),
        b"HTTP/1.1 200 Connection Established\r\n\r\n",
    ]
    connect = Mock(return_value=sock)
    monkeypatch.setattr(bridge.socket, "create_connection", connect)
    context = Mock()
    context.wrap_socket.side_effect = lambda source, **kwargs: events.append("TLS") or source
    monkeypatch.setattr(bridge.ssl, "create_default_context", Mock(return_value=context))
    assert bridge.open_chain("target.example", 443, (protocol, "proxy.example", 3010, "USER", "PASS")) is sock
    connect.assert_called_once()
    assert events[:2] == [b"\x05\x01\x00", b"\x05\x01\x00\x03\x0dproxy.example" + (3010).to_bytes(2, "big")]
    if protocol == "https":
        assert events[2] == "TLS"
        context.wrap_socket.assert_called_once_with(sock, server_hostname="proxy.example")
    else:
        context.wrap_socket.assert_not_called()
    assert events[-1].startswith(b"CONNECT target.example:443 HTTP/1.1\r\n")
    assert b"Proxy-Authorization: Basic VVNFUjpQQVNT\r\n" in events[-1]


@pytest.mark.parametrize("stage", ["connect", "handshake", "authentication"])
def test_failed_hop_never_falls_back_to_direct_connection(monkeypatch, stage):
    sock = Mock()
    sock.recv.side_effect = ([b"\x05\xff"] if stage == "handshake" else [
        b"\x05\x00", b"\x05\x00\x00\x01", bytes(6), b"\x05\x02", b"\x01\x01",
    ])
    connect = Mock(side_effect=ConnectionError("first hop closed")) if stage == "connect" else Mock(return_value=sock)
    monkeypatch.setattr(bridge.socket, "create_connection", connect)
    with pytest.raises(ConnectionError):
        bridge.open_chain("target.example", 443, ("socks5", "proxy.example", 3010, "USER", "PASS"))
    connect.assert_called_once_with((bridge.LOCAL_SOCKS_HOST, bridge.LOCAL_SOCKS_PORT), timeout=15)
    if stage != "connect":
        sock.close.assert_called_once()


@pytest.mark.parametrize("response", [b"", b"HTTP/1.1 407 Rejected\r\n\r\n", b"X" * 65536], ids=["eof", "rejected", "oversized"])
def test_http_upstream_eof_rejection_and_oversized_headers_fail_promptly(response):
    sock = Mock()
    sock.recv.return_value = response
    with pytest.raises(ConnectionError):
        bridge.http_proxy_connect(sock, "target.example", 443, "USER", "PASS")
    sock.recv.assert_called_once()


def _dynamic_auth(metadata="socks5|proxy.example|3010|USER", prefix="iprb_"):
    username = prefix + base64.urlsafe_b64encode(metadata.encode()).decode().rstrip("=")
    token = base64.b64encode((username + ":SECRET_FIXTURE").encode()).decode()
    return "Proxy-Authorization: Basic " + token + "\r\n"


@pytest.mark.parametrize("authorization", [
    "Proxy-Authorization: Basic !!!\r\n",
    _dynamic_auth(prefix="wrong_"),
    _dynamic_auth("invalid"),
    _dynamic_auth("ftp|proxy.example|3010|USER"),
    _dynamic_auth("socks5||3010|USER"),
    _dynamic_auth("socks5|proxy.example|0|USER"),
    _dynamic_auth("socks5|proxy.example|65536|USER"),
])
def test_malformed_dynamic_auth_returns_private_502_without_subscription(monkeypatch, capsys, authorization):
    request = Mock()
    request.recv.return_value = ("CONNECT target.example:443 HTTP/1.1\r\n" + authorization + "\r\n").encode()
    open_chain = Mock()
    load_credential = Mock()
    monkeypatch.setattr(bridge, "open_chain", open_chain)
    monkeypatch.setattr(bridge, "load_credential", load_credential)
    monkeypatch.setattr(bridge, "SOURCE_URL", "https://subscription.example/fixture")
    bridge.Handler(request, ("127.0.0.1", 1), None)
    request.sendall.assert_called_once_with(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
    open_chain.assert_not_called()
    load_credential.assert_not_called()
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_chain_failure_returns_private_502(monkeypatch, capsys):
    request = Mock()
    request.recv.return_value = ("CONNECT target.example:443 HTTP/1.1\r\n" + _dynamic_auth() + "\r\n").encode()
    open_chain = Mock(side_effect=RuntimeError("SECRET_FIXTURE"))
    monkeypatch.setattr(bridge, "open_chain", open_chain)
    bridge.Handler(request, ("127.0.0.1", 1), None)
    open_chain.assert_called_once_with("target.example", 443, ("socks5", "proxy.example", 3010, "USER", "SECRET_FIXTURE"))
    request.sendall.assert_called_once_with(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


@pytest.mark.parametrize("subscription", ["", "https://subscription.example/fixture"])
def test_missing_dynamic_auth_challenges_browser_or_preserves_subscription(monkeypatch, subscription):
    request = Mock()
    request.recv.return_value = b"CONNECT target.example:443 HTTP/1.1\r\n\r\n"
    upstream = Mock()
    open_chain = Mock(return_value=upstream)
    monkeypatch.setattr(bridge, "open_chain", open_chain)
    monkeypatch.setattr(bridge, "relay", Mock())
    monkeypatch.setattr(bridge, "SOURCE_URL", subscription)
    bridge.Handler(request, ("127.0.0.1", 1), None)
    if subscription:
        open_chain.assert_called_once_with("target.example", 443, None)
        upstream.close.assert_called_once()
        assert request.sendall.call_args.args[0].startswith(b"HTTP/1.1 200")
    else:
        open_chain.assert_not_called()
        response = request.sendall.call_args.args[0]
        assert response.startswith(b"HTTP/1.1 407")
        assert b"Proxy-Authenticate: Basic" in response


def test_unknown_upstream_protocol_is_rejected_before_network(monkeypatch):
    connect = Mock()
    monkeypatch.setattr(bridge.socket, "create_connection", connect)
    with pytest.raises(ValueError, match="unsupported upstream proxy protocol"):
        bridge.open_chain("target.example", 443, ("ftp", "proxy.example", 3010, "USER", "PASS"))
    connect.assert_not_called()
