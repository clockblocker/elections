from __future__ import annotations

import argparse
import itertools
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .common import (
        decode_text,
        extract_tree_nodes,
        json_write,
        opener,
        safe_name,
        sha256_bytes,
    )
except ImportError:  # Direct script execution.
    from common import (
        decode_text,
        extract_tree_nodes,
        json_write,
        opener,
        safe_name,
        sha256_bytes,
    )


WAYBACK = "https://web.archive.org/web/{timestamp}id_/"


def replace_query(url: str, replacements: dict[str, str | None]) -> str:
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    for key, value in replacements.items():
        if value is None:
            query.pop(key, None)
        else:
            query[key] = value
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
    )


def variants(url: str, timestamps: list[str]) -> list[tuple[str, str]]:
    parts = urllib.parse.urlsplit(url)
    hosts = [parts.netloc]
    if parts.netloc == "www.vybory.izbirkom.ru":
        region = dict(urllib.parse.parse_qsl(parts.query)).get("region")
        if region:
            hosts.append(f"www.{region}.vybory.izbirkom.ru")
    paths = {parts.path}
    if parts.path.endswith("/region/region/izbirkom"):
        paths.add(parts.path.replace("/region/region/izbirkom", "/region/izbirkom"))
    elif parts.path.endswith("/region/izbirkom"):
        paths.add(parts.path.replace("/region/izbirkom", "/region/region/izbirkom"))

    originals: set[str] = set()
    for scheme, host, path, pronetvd, report_mode in itertools.product(
        ("http", "https"), hosts, paths, ("null", "0"), (None, "null")
    ):
        candidate = urllib.parse.urlunsplit((scheme, host, path, parts.query, ""))
        candidate = replace_query(
            candidate, {"pronetvd": pronetvd, "report_mode": report_mode}
        )
        originals.add(candidate)

    output = [("live", candidate) for candidate in sorted(originals)]
    output.extend(
        (f"wayback:{timestamp}", WAYBACK.format(timestamp=timestamp) + candidate)
        for timestamp in timestamps
        for candidate in sorted(originals)
    )
    return output


def signals(payload: bytes, content_type: str) -> dict[str, Any]:
    text, encoding = decode_text(payload)
    lower = text.casefold()
    nodes, _ = extract_tree_nodes(payload)
    return {
        "encoding": encoding,
        "tree_nodes": len(nodes),
        "has_uik": "уик" in lower,
        "has_tik": "тик" in lower,
        "has_candidate_word": "кандидат" in lower,
        "has_voter_accounting": "число избирателей" in lower,
        "wayback_error": "wayback machine has not archived that url" in lower,
        "looks_html": "html" in content_type.casefold() or "<html" in lower[:1000],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe GAS live/Wayback URL variants")
    parser.add_argument("--url", required=True)
    parser.add_argument("--wayback", action="append", default=[])
    parser.add_argument("--skip-live", action="store_true")
    parser.add_argument("--rate", type=float, default=10.0, help="maximum requests per second")
    parser.add_argument("--limit", type=int, default=40)
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--raw-dir", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    if args.rate <= 0 or args.limit < 1:
        parser.error("--rate and --limit must be positive")

    candidates = variants(args.url, args.wayback)
    if args.skip_live:
        candidates = [item for item in candidates if item[0].startswith("wayback:")]
    candidates = candidates[: args.limit]
    client = opener()
    observations: list[dict[str, Any]] = []
    previous_start = 0.0
    args.raw_dir.mkdir(parents=True, exist_ok=True)
    for strategy, url in candidates:
        wait = (1.0 / args.rate) - (time.monotonic() - previous_start)
        if wait > 0:
            time.sleep(wait)
        previous_start = time.monotonic()
        started = time.monotonic()
        request = urllib.request.Request(url, headers={"User-Agent": "elections-gas-lab/0.1"})
        try:
            response = client.open(request, timeout=args.timeout)
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, urllib.error.URLError) as error:
            observations.append(
                {
                    "strategy": strategy,
                    "requested_url": url,
                    "error": str(error),
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            continue
        with response:
            payload = response.read()
            final_url = response.geturl()
            status = getattr(response, "status", None) or getattr(response, "code", None)
            content_type = response.headers.get("Content-Type", "")
        suffix = ".html" if "html" in content_type.casefold() else ".bin"
        path = args.raw_dir / safe_name(url, suffix)
        path.write_bytes(payload)
        observations.append(
            {
                "strategy": strategy,
                "requested_url": url,
                "final_url": final_url,
                "http_status": status,
                "content_type": content_type,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "size_bytes": len(payload),
                "sha256": sha256_bytes(payload),
                "path": str(path),
                "signals": signals(payload, content_type),
            }
        )

    report = {
        "input_url": args.url,
        "rate_requests_per_second": args.rate,
        "candidate_count": len(candidates),
        "observations": observations,
    }
    json_write(args.report, report)
    useful = sum(
        bool(row.get("signals", {}).get("has_voter_accounting")) for row in observations
    )
    print(
        json.dumps(
            {"report": str(args.report), "observations": len(observations), "useful": useful},
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
