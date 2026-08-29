from __future__ import annotations

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REFERENCE_DIR = ROOT / "docs" / "reference" / "payment-orchestration"
DOCX = REFERENCE_DIR / "半自动支付编排系统_从账号结账到支付结果确认_紧凑版.docx"
MARKDOWN = REFERENCE_DIR / "半自动支付编排系统_从账号结账到支付结果确认_紧凑版.md"
PARSED_JSON = REFERENCE_DIR / "半自动支付编排系统_从账号结账到支付结果确认_紧凑版.json"
EXPECTED_SOURCE_SHA256 = "fc774f040bbea12a2c0f7b8f6cb686439e941c898f56de86709a9785e6ffb506"


def test_reference_bundle_contains_original_and_searchable_parse() -> None:
    assert DOCX.is_file()
    assert MARKDOWN.is_file()
    assert PARSED_JSON.is_file()
    parsed = json.loads(PARSED_JSON.read_text(encoding="utf-8"))
    assert hashlib.sha256(DOCX.read_bytes()).hexdigest() == EXPECTED_SOURCE_SHA256
    assert parsed["metadata"]["source_sha256"] == EXPECTED_SOURCE_SHA256
    assert parsed["metadata"]["paragraph_count"] == 220
    assert parsed["metadata"]["table_count"] == 12
    assert parsed["metadata"]["comments_present"] is False
    assert parsed["metadata"]["tracked_insertions"] == 0
    assert parsed["metadata"]["tracked_deletions"] == 0


def test_reference_parse_preserves_architecture_headings_and_tables() -> None:
    parsed = json.loads(PARSED_JSON.read_text(encoding="utf-8"))
    headings = {item["text"] for item in parsed["headings"]}
    assert "一、系统整体架构与设计理念" in headings
    assert "七、完整端到端流程时序" in headings
    assert "十、安全边界与最佳实践" in headings
    headers = [table["rows"][0][0] for table in parsed["tables"]]
    assert "模块" in headers
    assert "异常类型" in headers
    assert "日志字段" in headers
    markdown = MARKDOWN.read_text(encoding="utf-8")
    assert "## Full Ordered Content" in markdown
    assert "### Table 11" in markdown
