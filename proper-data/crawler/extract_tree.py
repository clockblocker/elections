from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from .common import extract_tree_nodes, json_write, node_dicts
except ImportError:  # Direct script execution.
    from common import extract_tree_nodes, json_write, node_dicts


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract embedded GAS tvdTreeJson hierarchy")
    parser.add_argument("html", type=Path)
    parser.add_argument("--source-url", default="")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    nodes, encoding = extract_tree_nodes(args.html.read_bytes(), args.source_url)
    result = {
        "source_file": str(args.html),
        "source_url": args.source_url or None,
        "encoding": encoding,
        "node_count": len(nodes),
        "nodes": node_dicts(nodes),
    }
    if args.output:
        json_write(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
