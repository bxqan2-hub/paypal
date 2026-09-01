from __future__ import annotations

import socket
from pathlib import Path

import pytest

from tools.mitm_capture import (
    TLS_PASSTHROUGH_HOSTS,
    find_mitm_binary,
    parse_upstream,
    read_devtools_port,
    require_free_port,
    upstream_arguments,
)


def test_read_devtools_port(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("57251\n/devtools/browser/fixture\n", encoding="utf-8")
    assert read_devtools_port(tmp_path) == 57251


def test_find_mitm_binary_accepts_explicit_path(tmp_path: Path) -> None:
    executable = tmp_path / "mitmweb.exe"
    executable.write_bytes(b"")
    assert find_mitm_binary("mitmweb", str(executable)) == executable.resolve()


def test_upstream_arguments_keep_credentials_out_of_mode() -> None:
    args = upstream_arguments("http://user:pass@127.0.0.1:1080")
    assert args[:2] == ["--mode", "upstream:http://127.0.0.1:1080"]
    assert args[2:] == ["--upstream-auth", "user:pass"]


def test_upstream_arguments_accept_four_field_export() -> None:
    parsed, host = parse_upstream("127.0.0.1:1080:user:pass")
    assert parsed.scheme == "socks5"
    assert parsed.username == "user"
    assert host == "127.0.0.1"


def test_upstream_arguments_reject_invalid_scheme() -> None:
    with pytest.raises(ValueError):
        upstream_arguments("ftp://127.0.0.1:21")


def test_roxy_ip_check_passthrough_is_narrow() -> None:
    import re

    assert re.match(TLS_PASSTHROUGH_HOSTS, "ipcheck.roxybrowser.com:443")
    assert re.match(TLS_PASSTHROUGH_HOSTS, "ipcheck.roxybrowser.co:443")
    assert re.match(TLS_PASSTHROUGH_HOSTS, "chatgpt.com:443")
    assert re.match(TLS_PASSTHROUGH_HOSTS, "auth.openai.com:443")
    assert not re.match(TLS_PASSTHROUGH_HOSTS, "paypal.com:443")
    assert not re.match(TLS_PASSTHROUGH_HOSTS, "api.ip2location.io:443")


def test_require_free_port_rejects_invalid_port() -> None:
    with pytest.raises(ValueError):
        require_free_port(70000, "proxy")


def test_require_free_port_rejects_occupied_port() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        with pytest.raises(RuntimeError, match="already in use"):
            require_free_port(listener.getsockname()[1], "proxy")
