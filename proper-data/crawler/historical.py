from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.parse
from collections import Counter
from pathlib import Path
from typing import Any

from lxml import html as lxml_html

try:
    from .common import atomic_write, decode_text, extract_tree_nodes, json_write
    from .decode_script_result import decode_script_tables
    from .shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from .transport import FetchConfig, Fetcher, ResponseStore
except ImportError:
    from common import atomic_write, decode_text, extract_tree_nodes, json_write
    from decode_script_result import decode_script_tables
    from shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from transport import FetchConfig, Fetcher, ResponseStore


HERE = Path(__file__).resolve().parent
DEFAULT_CATALOG = HERE / "historical-elections.json"
DEFAULT_RAW = Path("data/raw/gas-duma-history")
DEFAULT_REPORTS = Path("reports/generated/gas-duma-history")
INTEGER_RE = re.compile(r"^[\s\xa0]*(\d+)")


def load_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain an object")
    return value


def elections(path: Path) -> list[dict[str, Any]]:
    rows = load_object(path).get("elections")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise TypeError(f"{path} has no elections array")
    return rows


def selected(rows: list[dict[str, Any]], years: list[int]) -> list[dict[str, Any]]:
    wanted = set(years)
    result = [row for row in rows if not wanted or int(row["year"]) in wanted]
    missing = wanted - {int(row["year"]) for row in result}
    if missing:
        raise ValueError(f"unknown election years: {sorted(missing)}")
    return result


def classify_page(payload: bytes) -> dict[str, Any]:
    if payload.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        return {
            "family": "ole-compound-spreadsheet",
            "encoding": "binary",
            "table_count": 0,
            "max_table_rows": 0,
            "max_table_columns": 0,
            "tree_node_count": 0,
            "has_voter_accounting": False,
            "has_party_signal": False,
            "has_candidate_signal": False,
            "challenge_or_error": False,
        }
    if payload.startswith(b"PK\x03\x04"):
        return {
            "family": "zip-office-document",
            "encoding": "binary",
            "table_count": 0,
            "max_table_rows": 0,
            "max_table_columns": 0,
            "tree_node_count": 0,
            "has_voter_accounting": False,
            "has_party_signal": False,
            "has_candidate_signal": False,
            "challenge_or_error": False,
        }
    source, encoding = decode_text(payload)
    lower = source.casefold()
    script_tables = decode_script_tables(source)
    try:
        document = lxml_html.fromstring(source)
    except (ValueError, lxml_html.ParserError):
        plain_tables: list[list[list[str]]] = []
    else:
        plain_tables = [
            [
                [
                    " ".join(cell.text_content().split())
                    for cell in row.xpath("./th|./td")
                ]
                for row in table.xpath(".//tr")
                if row.xpath("./th|./td")
            ]
            for table in document.xpath("//table")
        ]
    tables = script_tables or plain_tables
    nodes, _ = extract_tree_nodes(payload)
    has_font = "@font-face" in lower or ".ttf" in lower or ".woff" in lower
    has_script_decoder = bool(
        "getelementsbyclassname" in lower and "innerhtml" in lower
    )
    if has_script_decoder:
        family = "randomized-inline-javascript"
    elif has_font:
        family = "custom-font-obfuscated"
    elif plain_tables:
        family = "plain-html-table"
    elif nodes:
        family = "client-json-hierarchy"
    elif "<frameset" in lower or "<frame " in lower:
        family = "frameset-navigation"
    elif "<html" in lower:
        family = "html-navigation-or-error"
    else:
        family = "unknown"
    flat = " ".join(
        cell for table in tables for row in table for cell in row
    ).casefold()
    return {
        "family": family,
        "encoding": encoding,
        "table_count": len(tables),
        "max_table_rows": max((len(table) for table in tables), default=0),
        "max_table_columns": max(
            (len(row) for table in tables for row in table), default=0
        ),
        "tree_node_count": len(nodes),
        "has_voter_accounting": "число избирателей" in flat,
        "has_party_signal": "политическ" in flat
        or "избирательн" in flat
        and "объединен" in flat,
        "has_candidate_signal": "кандидат" in flat,
        "challenge_or_error": any(
            signal in lower
            for signal in (
                "captcha",
                "access denied",
                "service unavailable",
                "too many requests",
            )
        ),
    }


def make_plan(rows: list[dict[str, Any]], args: argparse.Namespace) -> dict[str, Any]:
    if args.source_mode == "live-only":
        sources = (("official-election-entry-live", "discovery_urls"),)
    elif args.source_mode == "archive-only":
        sources = (("official-election-entry-archive", "archive_urls"),)
    else:
        sources = (
            ("official-election-entry-live", "discovery_urls"),
            ("official-election-entry-archive", "archive_urls"),
        )
    requests = [
        {
            "year": int(row["year"]),
            "election_vrn": row.get("vrn"),
            "request_class": request_class,
            "url": url,
        }
        for row in rows
        for request_class, field in sources
        for url in row.get(field, [])
    ]
    return {
        "schema_version": 1,
        "mode": "dry-run",
        "rate_rps": args.rate,
        "dispatch_interval_ms": round(1000 / args.rate, 3),
        "concurrency": args.concurrency,
        "timeout_seconds": args.timeout,
        "retries": args.retries,
        "raw_dir": str(args.raw_dir),
        "manifest": str(args.raw_dir / "manifest.json"),
        "coordination_dir": str(args.coordination_dir),
        "estimated_requests": len(requests),
        "request_classes": dict(Counter(row["request_class"] for row in requests)),
        "nationwide_crawl": False,
        "source_mode": args.source_mode,
        "requests": requests,
    }


def plan(args: argparse.Namespace) -> int:
    result = make_plan(selected(elections(args.catalog), args.year), args)
    if args.output:
        json_write(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def probe(args: argparse.Namespace) -> int:
    rows = selected(elections(args.catalog), args.year)
    plan_data = make_plan(rows, args)
    store = ResponseStore(args.raw_dir)
    limiter = SharedRateLimiter(args.rate, coordination_dir=args.coordination_dir)
    fetcher = Fetcher(
        store,
        limiter,
        FetchConfig(
            timeout=args.timeout,
            retries=args.retries,
            backoff_initial=args.backoff_initial,
            backoff_max=args.backoff_max,
        ),
    )
    observations = []
    for request in plan_data["requests"]:
        record = fetcher.fetch(request["url"], refresh=args.refresh)
        classification: dict[str, Any] = {}
        if record.get("body_path"):
            payload = (store.root / record["body_path"]).read_bytes()
            classification = classify_page(payload)
            # Enrich the resumable raw manifest with election identity.
            record = store.save(
                request["url"],
                payload,
                {
                    **{
                        key: value
                        for key, value in record.items()
                        if key
                        not in {
                            "request_id",
                            "requested_url",
                            "body_path",
                            "byte_length",
                            "sha256",
                        }
                    },
                    "election_year": request["year"],
                    "election_vrn": request.get("election_vrn"),
                    "request_class": request["request_class"],
                    "archive_capture_timestamp": archive_timestamp(
                        record.get("final_url", "")
                    ),
                },
            )
        else:
            record = store.save_failure(
                request["url"],
                {
                    **{
                        key: value
                        for key, value in record.items()
                        if key not in {"request_id", "requested_url"}
                    },
                    "election_year": request["year"],
                    "election_vrn": request.get("election_vrn"),
                    "request_class": request["request_class"],
                },
            )
        observations.append({**request, **record, **classification})
    result = {
        "schema_version": 1,
        "plan": plan_data,
        "observations": observations,
    }
    json_write(args.report, result)
    print(
        json.dumps(
            {
                "report": str(args.report),
                "observations": len(observations),
                "statuses": dict(
                    Counter(
                        str(row.get("status", row.get("error_class", "error")))
                        for row in observations
                    )
                ),
                "families": dict(
                    Counter(str(row.get("family", "none")) for row in observations)
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def probe_spec(args: argparse.Namespace) -> int:
    spec = load_object(args.spec)
    requests = spec.get("requests")
    if not isinstance(requests, list) or not all(
        isinstance(row, dict) for row in requests
    ):
        raise TypeError(f"{args.spec} has no requests array")
    plan_data = {
        "schema_version": 1,
        "mode": "dry-run" if args.dry_run else "controlled-probe",
        "spec": str(args.spec),
        "rate_rps": args.rate,
        "dispatch_interval_ms": round(1000 / args.rate, 3),
        "concurrency": args.concurrency,
        "timeout_seconds": args.timeout,
        "retries": args.retries,
        "raw_dir": str(args.raw_dir),
        "coordination_dir": str(args.coordination_dir),
        "estimated_requests": len(requests),
        "request_classes": dict(
            Counter(str(row.get("request_class", "unspecified")) for row in requests)
        ),
        "nationwide_crawl": False,
        "requests": requests,
    }
    if args.dry_run:
        print(json.dumps(plan_data, ensure_ascii=False, indent=2))
        return 0
    store = ResponseStore(args.raw_dir)
    if args.only_failures:
        requests = [
            row
            for row in requests
            if not (existing := store.verified(str(row["url"])))
            or int(existing.get("status", 0)) not in range(200, 300)
        ]
    fetcher = Fetcher(
        store,
        SharedRateLimiter(args.rate, coordination_dir=args.coordination_dir),
        FetchConfig(
            timeout=args.timeout,
            retries=args.retries,
            backoff_initial=args.backoff_initial,
            backoff_max=args.backoff_max,
        ),
    )
    observations = []
    for request in requests:
        url = str(request["url"])
        record = fetcher.fetch(url, refresh=args.refresh)
        classification: dict[str, Any] = {}
        metadata = {
            **{
                key: value
                for key, value in record.items()
                if key
                not in {
                    "request_id",
                    "requested_url",
                    "body_path",
                    "byte_length",
                    "sha256",
                }
            },
            "election_year": request.get("year"),
            "election_vrn": request.get("election_vrn"),
            "request_class": request.get("request_class"),
            "contest": request.get("contest"),
            "expected_granularity": request.get("expected_granularity"),
        }
        if record.get("body_path"):
            payload = (store.root / record["body_path"]).read_bytes()
            classification = classify_page(payload)
            metadata["archive_capture_timestamp"] = archive_timestamp(
                str(record.get("final_url", ""))
            )
            if record.get("final_url") and record["final_url"] != url:
                metadata["redirect_history"] = [url, record["final_url"]]
            record = store.save(url, payload, metadata)
        else:
            record = store.save_failure(url, metadata)
        observations.append({**request, **record, **classification})
    result = {"schema_version": 1, "plan": plan_data, "observations": observations}
    json_write(args.report, result)
    print(
        json.dumps(
            {
                "report": str(args.report),
                "observations": len(observations),
                "statuses": dict(
                    Counter(
                        str(row.get("status", row.get("error_class", "error")))
                        for row in observations
                    )
                ),
                "families": dict(
                    Counter(str(row.get("family", "none")) for row in observations)
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def classify_saved(args: argparse.Namespace) -> int:
    payload = args.input.read_bytes()
    result = classify_page(payload)
    if args.direct_protocol:
        result["direct_protocol"] = extract_direct_protocol(payload)
    if args.output:
        json_write(args.output, result)
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def validate_matrix(args: argparse.Namespace) -> int:
    matrix = load_object(args.matrix)
    rows = matrix.get("elections")
    if not isinstance(rows, list):
        raise TypeError("availability matrix has no elections array")
    expected_years = {1993, 1995, 1999, 2003, 2007, 2011, 2016}
    actual_years = {int(row["year"]) for row in rows if isinstance(row, dict)}
    if actual_years != expected_years:
        raise ValueError(
            f"matrix years {sorted(actual_years)} != {sorted(expected_years)}"
        )
    required = {
        "election_identifier",
        "official_entry_urls",
        "verified_official_hosts",
        "live_availability",
        "archived_official_availability",
        "page_decoder_families",
        "character_encodings",
        "hierarchy_extraction_method",
        "observed_verified_reports",
        "party_uik_results",
        "candidate_uik_results",
        "uik_to_tik",
        "successful_probe_counts",
        "missing_elements",
        "failure_reasons",
        "readiness",
        "evidence",
    }
    checked_files = 0
    for row in rows:
        missing = required - set(row)
        if missing:
            raise ValueError(f"{row.get('year')} missing fields: {sorted(missing)}")
        for evidence in row["evidence"]:
            digest = str(evidence.get("sha256", ""))
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"invalid evidence hash for {row['year']}")
            path = Path(str(evidence.get("path", "")))
            if path.is_file() and str(path).startswith("data/raw/"):
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
                if actual != digest:
                    raise ValueError(f"evidence hash mismatch: {path}")
                checked_files += 1
    print(
        json.dumps(
            {
                "matrix": str(args.matrix),
                "years": sorted(actual_years),
                "checked_raw_files": checked_files,
                "valid": True,
            },
            indent=2,
        )
    )
    return 0


def archive_timestamp(url: str) -> str | None:
    parts = urllib.parse.urlsplit(url)
    if parts.netloc != "web.archive.org":
        return None
    components = parts.path.split("/")
    if len(components) > 2 and components[1] == "web":
        return components[2].removesuffix("id_") or None
    return None


def extract_direct_protocol(payload: bytes) -> dict[str, Any]:
    source, encoding = decode_text(payload)
    document = lxml_html.fromstring(source)
    commission_name = ""
    numbered: dict[int, tuple[str, int]] = {}
    for row in document.xpath("//tr"):
        cells = row.xpath("./th|./td")
        if len(cells) < 2:
            continue
        values = [" ".join(cell.text_content().split()) for cell in cells]
        if values[0] == "Наименование Избирательной комиссии" and len(values) > 1:
            commission_name = values[1]
            continue
        if len(cells) < 3 or not values[0].isdigit():
            continue
        label = values[1]
        bold = " ".join(cells[2].xpath("string(.//b[1])").split())
        match = INTEGER_RE.match(bold or values[2])
        if not label or not match:
            continue
        number = int(values[0])
        candidate = (label, int(match.group(1)))
        previous = numbered.get(number)
        if previous is not None and previous != candidate:
            raise ValueError(
                f"conflicting protocol row {number}: {previous} vs {candidate}"
            )
        numbered[number] = candidate
    if not commission_name or len(numbered) < 3:
        raise ValueError("page has no direct commission protocol")
    accounting = {
        label: value
        for _, (label, value) in sorted(numbered.items())
        if label.casefold().startswith("число ")
    }
    votes = {
        label: value
        for _, (label, value) in sorted(numbered.items())
        if not label.casefold().startswith("число ")
    }
    if not accounting or not votes:
        raise ValueError("protocol does not contain both accounting and option rows")
    valid_label = next(
        (
            label
            for label in accounting
            if label.casefold().startswith(
                "число действительных избирательных бюллетеней"
            )
        ),
        None,
    )
    vote_sum = sum(votes.values())
    valid_ballots = accounting.get(valid_label) if valid_label else None
    return {
        "encoding": encoding,
        "commission_name": commission_name,
        "accounting": accounting,
        "votes": votes,
        "validation": {
            "vote_sum": vote_sum,
            "valid_ballots": valid_ballots,
            "vote_sum_matches_valid_ballots": valid_ballots == vote_sum,
        },
    }


def _ts_literal(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2).replace("</", "<\\/")


def generate_samples(args: argparse.Namespace) -> int:
    report = load_object(args.probe_report)
    observations = report.get("observations")
    if not isinstance(observations, list):
        raise TypeError(f"{args.probe_report} has no observations array")
    store_root = Path(str(report.get("plan", {}).get("raw_dir", DEFAULT_RAW)))
    generated = 0
    for row in observations:
        if (
            not isinstance(row, dict)
            or row.get("request_class") != "gas-tik-summary"
            or int(row.get("status", 0)) != 200
            or not row.get("body_path")
        ):
            continue
        year = int(row["year"])
        if args.year and year not in set(args.year):
            continue
        report_type = int(
            urllib.parse.parse_qs(urllib.parse.urlsplit(str(row["url"])).query)["type"][
                0
            ]
        )
        entity_tvd = str(row["entity_tvd"])
        protocol = extract_direct_protocol((store_root / row["body_path"]).read_bytes())
        value = {
            "election": f"{year}-duma",
            "electionVrn": str(row["election_vrn"]),
            "protocol": "party",
            "reportType": report_type,
            "commission": {
                "level": "tik",
                "name": protocol["commission_name"],
                "tvd": entity_tvd,
                "region": row.get("region_label"),
            },
            "accounting": protocol["accounting"],
            "votes": protocol["votes"],
            "validation": protocol["validation"],
            "source": {
                "requestedUrl": row["url"],
                "finalUrl": row.get("final_url"),
                "retrievedAt": row.get("retrieved_at"),
                "archiveCaptureTimestamp": row.get("archive_capture_timestamp"),
                "sha256": row["sha256"],
                "provenance": row.get("provenance"),
                "encoding": protocol["encoding"],
                "extractionMethod": "plain-html-direct-protocol",
                "derivation": "direct",
            },
        }
        destination = (
            args.output_root
            / f"{year}-duma"
            / "protocol"
            / "tic"
            / str(report_type)
            / f"{entity_tvd}.ts"
        )
        declaration = f"duma_{year}_tic_{report_type}_{entity_tvd}"
        content = (
            "// This file is autogenerated by proper-data/crawler/historical.py.\n"
            "// Do not edit it manually.\n\n"
            'import type { HistoricalTikProtocol } from "../../types";\n\n'
            f"export const {declaration} = {_ts_literal(value)} satisfies HistoricalTikProtocol;\n"
        )
        atomic_write(destination, content.encode("utf-8"))
        generated += 1
    if not generated:
        raise ValueError("no verified direct TIK protocols matched")
    print(
        json.dumps(
            {"generated": generated, "output_root": str(args.output_root)}, indent=2
        )
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Controlled historical State Duma official-source probes"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    common.add_argument("--year", type=int, action="append", default=[])
    common.add_argument("--rate", type=float, default=10.0)
    common.add_argument("--concurrency", type=int, default=2)
    common.add_argument("--timeout", type=float, default=20.0)
    common.add_argument("--retries", type=int, default=3)
    common.add_argument("--backoff-initial", type=float, default=0.5)
    common.add_argument("--backoff-max", type=float, default=30.0)
    common.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    common.add_argument(
        "--coordination-dir", type=Path, default=DEFAULT_COORDINATION_DIR
    )
    common.add_argument(
        "--source-mode",
        choices=("live-only", "archive-fallback", "archive-only"),
        default="archive-fallback",
    )
    sub = result.add_subparsers(dest="command", required=True)
    command = sub.add_parser("plan", parents=[common])
    command.add_argument("--output", type=Path)
    command.set_defaults(func=plan)
    command = sub.add_parser("probe", parents=[common])
    command.add_argument(
        "--report", type=Path, default=DEFAULT_REPORTS / "entry-probes.json"
    )
    command.add_argument("--refresh", action="store_true")
    command.set_defaults(func=probe)
    command = sub.add_parser("probe-spec", parents=[common])
    command.add_argument("--spec", type=Path, required=True)
    command.add_argument("--report", type=Path, required=True)
    command.add_argument("--dry-run", action="store_true")
    command.add_argument("--refresh", action="store_true")
    command.add_argument("--only-failures", action="store_true")
    command.set_defaults(func=probe_spec)
    command = sub.add_parser("generate-samples")
    command.add_argument("--probe-report", type=Path, required=True)
    command.add_argument("--year", type=int, action="append", default=[])
    command.add_argument("--output-root", type=Path, default=Path("proper-data"))
    command.set_defaults(func=generate_samples)
    command = sub.add_parser("classify")
    command.add_argument("--input", type=Path, required=True)
    command.add_argument("--direct-protocol", action="store_true")
    command.add_argument("--output", type=Path)
    command.set_defaults(func=classify_saved)
    command = sub.add_parser("validate-matrix")
    command.add_argument(
        "--matrix",
        type=Path,
        default=Path("proper-data/historical-duma-availability.json"),
    )
    command.set_defaults(func=validate_matrix)
    return result


def main() -> int:
    args = parser().parse_args()
    if hasattr(args, "rate") and (
        args.rate <= 0 or args.concurrency < 1 or args.retries < 0
    ):
        raise SystemExit("rate/concurrency must be positive and retries non-negative")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
