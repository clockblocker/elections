from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    from .common import json_write, opener, safe_name, sha256_bytes
    from .probe_matrix import signals
except ImportError:  # Direct script execution.
    from common import json_write, opener, safe_name, sha256_bytes
    from probe_matrix import signals


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch exact captures from a CDX lab report")
    parser.add_argument("cdx_report", type=Path)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--rate", type=float, default=10.0)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.limit < 1 or args.rate <= 0:
        parser.error("--limit and --rate must be positive")

    source = json.loads(args.cdx_report.read_text(encoding="utf-8"))
    captures = source.get("captures", [])[: args.limit]
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    client = opener()
    observations = []
    previous_start = 0.0
    for capture in captures:
        timestamp = capture["timestamp"]
        original = capture["original"]
        url = f"https://web.archive.org/web/{timestamp}id_/{original}"
        wait = (1.0 / args.rate) - (time.monotonic() - previous_start)
        if wait > 0:
            time.sleep(wait)
        previous_start = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": "elections-gas-lab/0.1"})
        try:
            response = client.open(request, timeout=args.timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError) as error:
            observations.append({**capture, "requested_url": url, "error": str(error)})
            continue
        with response:
            payload = response.read()
            status = getattr(response, "status", None) or getattr(response, "code", None)
            content_type = response.headers.get("Content-Type", "")
            final_url = response.geturl()
        suffix = ".html" if "html" in content_type.casefold() else ".bin"
        path = args.raw_dir / safe_name(url, suffix)
        path.write_bytes(payload)
        observations.append(
            {
                **capture,
                "requested_url": url,
                "final_url": final_url,
                "http_status": status,
                "content_type": content_type,
                "size_bytes": len(payload),
                "sha256": sha256_bytes(payload),
                "path": str(path),
                "signals": signals(payload, content_type),
            }
        )

    json_write(
        args.report,
        {
            "source_cdx_report": str(args.cdx_report),
            "rate_requests_per_second": args.rate,
            "observations": observations,
        },
    )
    useful = sum(
        bool(row.get("signals", {}).get("has_voter_accounting")) for row in observations
    )
    print(json.dumps({"report": str(args.report), "fetched": len(observations), "useful": useful}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
