from __future__ import annotations

"""Read-only integrity checks for directly called upstream projects."""

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import ProtocolError


def verify_upstream_project(
    *,
    project_dir: Path,
    manifest_path: Path,
    expected_commit: str,
    provider: str,
) -> None:
    """Verify the vendored files without changing or wrapping upstream logic."""
    try:
        manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(500, f"{provider} 上游源码清单无法读取: {manifest_path}") from exc

    commit = str(manifest.get("commit") or "")
    hashes = manifest.get("sha256")
    if commit != expected_commit or not isinstance(hashes, dict) or not hashes:
        raise ProtocolError(500, f"{provider} 上游源码清单与固定版本不匹配")

    mismatches: list[str] = []
    for relative, expected_hash in hashes.items():
        relative_path = Path(str(relative))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            mismatches.append(str(relative))
            continue
        source_path = project_dir / relative_path
        try:
            actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        except OSError:
            actual_hash = "missing"
        if actual_hash != str(expected_hash).lower():
            mismatches.append(str(relative))
    if mismatches:
        preview = ", ".join(mismatches[:5])
        raise ProtocolError(500, f"{provider} 上游源码完整性校验失败: {preview}")
