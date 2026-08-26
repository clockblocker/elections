from __future__ import annotations

import argparse
import json
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from .common import build_request, json_write, opener
except ImportError:  # Direct script execution.
    from common import build_request, json_write, opener


CDX_URL = "https://web.archive.org/cdx/search/cdx"


def main() -> int:
    parser = argparse.ArgumentParser(description="Query Wayback CDX for GAS captures")
    parser.add_argument("--url", required=True, help="exact URL or a narrow wildcard")
    parser.add_argument("--from-year", type=int, default=2021)
    parser.add_argument("--to-year", type=int, default=2022)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not 1 <= args.limit <= 10_000:
        parser.error("--limit must be between 1 and 10000")

    params = urllib.parse.urlencode(
        {
            "url": args.url,
            "from": args.from_year,
            "to": args.to_year,
            "output": "json",
            "fl": "timestamp,original,statuscode,mimetype,digest,length",
            "filter": "statuscode:200",
            "collapse": "digest",
            "limit": args.limit,
        }
    )
    request_url = f"{CDX_URL}?{params}"
    request = build_request(request_url)
    with opener().open(request, timeout=args.timeout) as response:
        rows = json.loads(response.read())
    headings = rows[0] if rows else []
    captures = [dict(zip(headings, row, strict=True)) for row in rows[1:]]
    json_write(
        args.output,
        {
            "query": args.url,
            "from_year": args.from_year,
            "to_year": args.to_year,
            "capture_count": len(captures),
            "captures": captures,
        },
    )
    print(json.dumps({"output": str(args.output), "captures": len(captures)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
