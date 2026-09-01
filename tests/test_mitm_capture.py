from __future__ import annotations

from pathlib import Path

import pytest

from tools.mitm_capture import find_mitm_binary, read_devtools_port, require_free_port, upstream_arguments


def test_read_devtools_port(tmp_path: Path) -> None:
    (tmp_path / "DevToolsActivePort").write_text("57251\n/devtools/browser/fixture\n", encoding="utf-8")
    assert read_devtools_port(tmp_path) == 57251


def test_find_mitm_binary_accepts_explicit_path(tmp_path: Path) -> None:
    executable = tmp_path / "mitmweb.exe"
    executable.write_bytes(b"")
    assert find_mitm_binary("mitmweb", str(executable)) == executable.resolve()


def test_upstream_arguments_keep_credentials_out_of_mode() -> None:
    args = upstream_arguments("socks5://user:pass@127.0.0.1:1080")
    assert args[:2] == ["--mode", "upstream:socks5://127.0.0.1:1080"]
    assert args[2:] == ["--upstream-auth", "user:pass"]


def test_upstream_arguments_accept_four_field_export() -> None:
    args = upstream_arguments("127.0.0.1:1080:user:pass")
    assert args[:2] == ["--mode", "upstream:socks5://127.0.0.1:1080"]


def test_upstream_arguments_reject_invalid_scheme() -> None:
    with pytest.raises(ValueError):
        upstream_arguments("ftp://127.0.0.1:21")


def test_require_free_port_rejects_invalid_port() -> None:
    with pytest.raises(ValueError):
        require_free_port(70000, "proxy")
