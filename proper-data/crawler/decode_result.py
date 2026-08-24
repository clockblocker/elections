from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from elections.ingest.cec_deobfuscate import deobfuscate_cec_html
from elections.ingest.single_member_html import _TableParser

try:
    from .common import json_write
except ImportError:  # Direct script execution.
    from common import json_write


def main() -> int:
    parser = argparse.ArgumentParser(description="Decode a preserved 2021 CEC result page")
    parser.add_argument("html", type=Path)
    parser.add_argument("--font", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    payload = args.html.read_bytes()
    try:
        html = payload.decode("utf-8")
        encoding = "utf-8"
    except UnicodeDecodeError:
        html = payload.decode("windows-1251")
        encoding = "windows-1251"
    decoded = deobfuscate_cec_html(html, args.font.read_bytes())
    table = _TableParser()
    table.feed(decoded)
    result = {
        "source_html": str(args.html),
        "source_font": str(args.font),
        "source_encoding": encoding,
        "row_count": len(table.rows),
        "max_columns": max(map(len, table.rows), default=0),
        "rows": table.rows,
    }
    json_write(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "rows": result["row_count"],
                "max_columns": result["max_columns"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
