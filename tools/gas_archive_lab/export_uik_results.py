from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

try:
    from .common import json_write
except ImportError:  # Direct script execution.
    from common import json_write


UIK_RE = re.compile(r"^УИК\s*№?\s*(\d+)$", re.IGNORECASE)


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def indexed_uiks(tree: dict[str, Any], parent_id: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for node in tree.get("nodes", []):
        if not isinstance(node, dict) or str(node.get("parent_id")) != parent_id:
            continue
        match = UIK_RE.fullmatch(str(node.get("text", "")).strip())
        if not match:
            continue
        number = int(match.group(1))
        if number in result:
            raise ValueError(f"duplicate UIK number {number} below TIK {parent_id}")
        result[number] = node
    return result


def transpose_table(
    decoded: dict[str, Any],
    uiks: dict[int, dict[str, Any]],
    *,
    option_kind: str,
) -> list[dict[str, Any]]:
    rows = decoded.get("rows")
    if not isinstance(rows, list):
        raise TypeError("decoded result has no rows")
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if isinstance(row, list) and any(UIK_RE.fullmatch(str(cell).strip()) for cell in row)
        ),
        None,
    )
    if header_index is None:
        raise ValueError("decoded table has no UIK header")
    header = rows[header_index]
    columns: dict[int, int] = {}
    for column, cell in enumerate(header):
        match = UIK_RE.fullmatch(str(cell).strip())
        if match:
            columns[int(match.group(1))] = column
    missing = sorted(set(columns) - set(uiks))
    if missing:
        raise ValueError(f"table UIKs absent from hierarchy: {missing}")

    data_rows = [row for row in rows[header_index + 1 :] if isinstance(row, list) and len(row) >= 3]
    if len(data_rows) < 13:
        raise ValueError("decoded result has too few protocol rows")
    accounting_rows = data_rows[:12]
    option_rows = data_rows[12:]
    records = []
    for number, column in columns.items():
        node = uiks[number]
        accounting = {
            str(row[1]): int(str(row[column]).split()[0]) for row in accounting_rows if column < len(row)
        }
        options = {
            str(row[1]): int(str(row[column]).split()[0]) for row in option_rows if column < len(row)
        }
        records.append(
            {
                "uik_number": number,
                "uik_tvd": str(node.get("node_id")),
                "tik_tvd": str(node.get("parent_id")),
                "official_uik_url": node.get("url"),
                "accounting": accounting,
                f"{option_kind}_votes": options,
            }
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Join a decoded GAS TIK table to exact UIK hierarchy nodes"
    )
    parser.add_argument("decoded", type=Path)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--tik-tvd", required=True)
    parser.add_argument("--kind", choices=("candidate", "party"), required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    decoded = load_object(args.decoded)
    tree = load_object(args.tree)
    uiks = indexed_uiks(tree, args.tik_tvd)
    records = transpose_table(decoded, uiks, option_kind=args.kind)
    result = {
        "schema_version": 1,
        "source": {
            "official_url": args.source_url,
            "sha256": args.source_sha256,
            "decoded_table": str(args.decoded),
            "hierarchy": str(args.tree),
        },
        "tik_tvd": args.tik_tvd,
        "uik_count": len(records),
        "records": records,
    }
    json_write(args.output, result)
    print(json.dumps({"output": str(args.output), "uiks": len(records)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
