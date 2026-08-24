from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from har_utils import analyze_har, markdown_report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect HAR traffic and emit a reusable optimization report.")
    parser.add_argument("har", type=Path, help="input HAR file")
    parser.add_argument("--output", "-o", type=Path, help="report output path; stdout when omitted")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    parser.add_argument("--host", default="", help="only entries whose host contains this text")
    parser.add_argument("--path", dest="path_contains", default="", help="only entries whose path contains this text")
    parser.add_argument("--method", default="", help="filter by HTTP method")
    parser.add_argument("--status", type=int, help="filter by response status")
    parser.add_argument("--contains", default="", help="filter entries whose serialized entry contains this text")
    parser.add_argument("--limit", type=int, default=0, help="maximum selected entries; 0 means all")
    parser.add_argument("--include-sensitive", action="store_true", help="include raw headers and payload values in the report")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report = analyze_har(
            args.har,
            host=args.host,
            path_contains=args.path_contains,
            method=args.method,
            status=args.status,
            contains=args.contains,
            limit=max(args.limit, 0),
            redact=not args.include_sensitive,
        )
        text = json.dumps(report, ensure_ascii=False, indent=2) if args.format == "json" else markdown_report(report)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(text, encoding="utf-8")
            print(f"HAR_REPORT={args.output.resolve()}")
        else:
            sys.stdout.write(text)
        return 0
    except (OSError, ValueError) as exc:
        print(f"HAR_ERROR={exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
