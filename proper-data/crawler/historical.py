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
    elif plain_tables:
        family = "plain-html-table"
    elif has_font:
        family = "custom-font-obfuscated"
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


def live_official_url(request: dict[str, Any]) -> str:
    """Return the exact live official counterpart of a checked-in probe URL."""
    explicit = request.get("live_url")
    if explicit:
        return str(explicit)
    archive_url = str(request["url"])
    parsed = urllib.parse.urlsplit(archive_url)
    if parsed.netloc != "web.archive.org":
        return archive_url
    match = re.match(r"^/web/[^/]+/(https?://.*)$", parsed.path)
    if not match:
        raise ValueError(f"cannot recover official URL from archive URL: {archive_url}")
    embedded = match.group(1)
    if parsed.query:
        embedded = f"{embedded}?{parsed.query}"
    if parsed.fragment:
        embedded = f"{embedded}#{parsed.fragment}"
    official = urllib.parse.urlsplit(embedded)
    if official.netloc.casefold() == "www.vybory.izbirkom.ru":
        official = official._replace(netloc="old.izbirkom.ru")
    return urllib.parse.urlunsplit(official)


def spec_requests(
    requests: list[dict[str, Any]], source_mode: str
) -> list[dict[str, Any]]:
    """Expand checked-in evidence requests into selected live/archive variants."""
    result: list[dict[str, Any]] = []
    for request in requests:
        candidate_url = str(request["url"])
        archive_url = request.get("archive_url")
        if urllib.parse.urlsplit(candidate_url).netloc == "web.archive.org":
            archive_url = candidate_url
        live_url = live_official_url(request)
        if source_mode == "live-only":
            variants = (("live-official", live_url),)
        elif source_mode == "archive-only":
            variants = (("archived-official", str(archive_url)),) if archive_url else ()
        else:
            variants = (("live-official", live_url),)
            if archive_url:
                variants += (("archived-official", str(archive_url)),)
        for source_variant, url in variants:
            result.append(
                {
                    **request,
                    "url": url,
                    "official_url": live_url,
                    "archive_url": str(archive_url) if archive_url else None,
                    "source_variant": source_variant,
                }
            )
    return result


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
    requests = spec_requests(requests, args.source_mode)
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
        "source_mode": args.source_mode,
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
            "source_variant": request.get("source_variant"),
            "official_url": request.get("official_url"),
            "archive_url": request.get("archive_url"),
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


def build_sample_requests(
    selections: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    store_root: Path,
    uik_sample_size: int,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for selection in selections:
        tik_tvd = str(selection["tik_tvd"])
        evidence = next(
            (
                row
                for row in reversed(observations)
                if isinstance(row, dict)
                and row.get("request_class") == "gas-hierarchy-children"
                and row.get("expected_granularity") == "uik-navigation"
                and str(row.get("entity_tvd")) == tik_tvd
                and int(row.get("status", 0)) == 200
                and row.get("body_path")
            ),
            None,
        )
        if evidence is None:
            raise ValueError(f"no preserved UIK hierarchy for TIK {tik_tvd}")
        nodes, _ = extract_tree_nodes(
            (store_root / str(evidence["body_path"])).read_bytes(),
            str(evidence["url"]),
            tik_tvd,
        )
        uiks = [node for node in nodes if node.is_uik and node.tvd and node.root]
        if not uiks:
            raise ValueError(f"hierarchy for TIK {tik_tvd} has no UIKs")
        roots = {node.root for node in uiks}
        if len(roots) != 1:
            raise ValueError(f"hierarchy for TIK {tik_tvd} has conflicting roots")
        root = next(iter(roots))
        base = "http://old.izbirkom.ru/region/region/izbirkom"
        common = {
            "action": "show",
            "root": root,
            "vrn": str(selection["election_vrn"]),
            "global": "true",
            "prver": "0",
            "pronetvd": str(selection["pronetvd"]),
        }

        def url(parameters: dict[str, str], base_url: str = base) -> str:
            return f"{base_url}?{urllib.parse.urlencode(parameters)}"

        shared = {
            "year": int(selection["year"]),
            "election_vrn": str(selection["election_vrn"]),
            "region_label": selection["region_label"],
            "entity_label": selection["tik_label"],
            "entity_tvd": tik_tvd,
            "hierarchy_evidence_path": evidence["body_path"],
            "hierarchy_evidence_sha256": evidence["sha256"],
        }
        for contest in selection["contests"]:
            direct_type = int(contest["direct_type"])
            column_type = int(contest["column_type"])
            contest_name = str(contest["contest"])
            result.extend(
                [
                    {
                        **shared,
                        "request_class": "gas-tik-direct-protocol",
                        "contest": contest_name,
                        "expected_granularity": "tik-direct",
                        "report_type": direct_type,
                        "url": url(
                            {
                                **common,
                                "tvd": tik_tvd,
                                "region": "0",
                                "sub_region": "0",
                                "vibid": tik_tvd,
                                "type": str(direct_type),
                            }
                        ),
                    },
                    {
                        **shared,
                        "request_class": "gas-tik-column-report",
                        "contest": contest_name,
                        "expected_granularity": "uik-columns",
                        "report_type": column_type,
                        "url": url(
                            {
                                **common,
                                "tvd": tik_tvd,
                                "region": "0",
                                "sub_region": "0",
                                "type": str(column_type),
                            }
                        ),
                    },
                ]
            )
            for node in uiks[:uik_sample_size]:
                result.append(
                    {
                        **shared,
                        "request_class": "gas-uik-direct-protocol",
                        "contest": contest_name,
                        "expected_granularity": "uik-direct",
                        "report_type": direct_type,
                        "uik_label": node.text,
                        "uik_tvd": node.tvd,
                        "url": url(
                            {
                                **common,
                                "root": str(node.root),
                                "tvd": str(node.tvd),
                                "region": str(node.region),
                                "sub_region": str(node.sub_region),
                                "vibid": str(node.tvd),
                                "type": str(direct_type),
                            }
                        ),
                    }
                )
    return result


def make_sample_spec(args: argparse.Namespace) -> int:
    selections_data = load_object(args.selections)
    rows = selections_data.get("selections")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise TypeError(f"{args.selections} has no selections array")
    hierarchy_report = load_object(args.hierarchy_report)
    observations = hierarchy_report.get("observations")
    if not isinstance(observations, list):
        raise TypeError(f"{args.hierarchy_report} has no observations array")
    store_root = Path(str(hierarchy_report.get("plan", {}).get("raw_dir", DEFAULT_RAW)))
    sample_size = int(selections_data.get("uik_sample_size", 3))
    requests = build_sample_requests(rows, observations, store_root, sample_size)
    result = {
        "schema_version": 1,
        "generated_from": {
            "selections": str(args.selections),
            "hierarchy_report": str(args.hierarchy_report),
        },
        "nationwide_crawl": False,
        "uik_sample_size": sample_size,
        "requests": requests,
    }
    json_write(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "requests": len(requests),
                "years": dict(Counter(str(row["year"]) for row in requests)),
                "classes": dict(Counter(row["request_class"] for row in requests)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _integer(value: str) -> int | None:
    match = INTEGER_RE.match(value)
    return int(match.group(1)) if match else None


def _uik_number(label: str) -> int | None:
    match = re.search(r"УИК\s*(?:№\s*)?(\d+)", label, re.IGNORECASE)
    return int(match.group(1)) if match else None


def _result_table(payload: bytes) -> tuple[str, list[list[str]]]:
    source, _ = decode_text(payload)
    tables = decode_script_tables(source)
    if not tables:
        raise ValueError("page has no decoded result table")
    table = max(
        tables,
        key=lambda value: (
            any("УИК" in cell for row in value for cell in row),
            max((len(row) for row in value), default=0),
            len(value),
        ),
    )
    return source, table


def validate_sample_observations(
    report: dict[str, Any], store_root: Path
) -> dict[str, Any]:
    observations = report.get("observations")
    if not isinstance(observations, list):
        raise TypeError("protocol probe report has no observations array")
    failures: list[dict[str, Any]] = []
    summaries: dict[tuple[int, str], Counter[str]] = {}
    columns: dict[tuple[int, str, str], dict[str, Any]] = {}

    def summary(row: dict[str, Any]) -> Counter[str]:
        key = (int(row["year"]), str(row["contest"]))
        return summaries.setdefault(key, Counter())

    for row in observations:
        if not isinstance(row, dict):
            continue
        stats = summary(row)
        stats["requests"] += 1
        if int(row.get("status", 0)) != 200 or not row.get("body_path"):
            failures.append({"url": row.get("url"), "reason": "non-200 response"})
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(str(row["url"])).query)
        if query.get("vrn", [None])[0] != str(row["election_vrn"]):
            failures.append({"url": row["url"], "reason": "election VRN mismatch"})
        if query.get("type", [None])[0] != str(row["report_type"]):
            failures.append({"url": row["url"], "reason": "report type mismatch"})
        payload = (store_root / str(row["body_path"])).read_bytes()
        if row["request_class"] == "gas-tik-column-report":
            source, table = _result_table(payload)
            width = max((len(value) for value in table), default=0)
            header = next(
                (value for value in table if any("УИК" in cell for cell in value)),
                None,
            )
            labels = header[3:] if header else []
            if len(labels) != width - 3:
                marker = source.find("<nobr>Сумма</nobr>")
                end = source.find("</thead>", marker)
                labels = re.findall(
                    r"УИК\s*(?:№\s*)?\d+", source[marker:end], re.IGNORECASE
                )
            evidence_path = store_root / str(row["hierarchy_evidence_path"])
            nodes, _ = extract_tree_nodes(
                evidence_path.read_bytes(), parent_id=str(row["entity_tvd"])
            )
            hierarchy_numbers = [
                _uik_number(node.text) for node in nodes if node.is_uik
            ]
            header_numbers = [_uik_number(label) for label in labels]
            if header_numbers != hierarchy_numbers:
                failures.append(
                    {
                        "url": row["url"],
                        "reason": "UIK columns do not match hierarchy evidence",
                        "header_uiks": header_numbers,
                        "hierarchy_uiks": hierarchy_numbers,
                    }
                )
            row_sums_checked = 0
            for values in table:
                if len(values) != width or not values[0].isdigit():
                    continue
                numbers = [_integer(value) for value in values[2:]]
                if None in numbers or numbers[0] != sum(numbers[1:]):
                    failures.append(
                        {
                            "url": row["url"],
                            "reason": "aggregate row does not equal UIK sum",
                            "label": values[1],
                        }
                    )
                else:
                    row_sums_checked += 1
            stats["column_reports"] += 1
            stats["uik_columns"] += len(labels)
            stats["aggregate_rows_reconciled"] += row_sums_checked
            columns[(int(row["year"]), str(row["entity_tvd"]), str(row["contest"]))] = {
                "labels": labels,
                "table": table,
                "width": width,
            }
        else:
            protocol = extract_direct_protocol(
                payload,
                commission_name=str(row.get("uik_label") or row["entity_label"]),
            )
            if not protocol["validation"]["vote_sum_matches_valid_ballots"]:
                failures.append(
                    {"url": row["url"], "reason": "votes do not match valid ballots"}
                )
            stats[
                "direct_uik_protocols"
                if row["request_class"] == "gas-uik-direct-protocol"
                else "direct_tik_protocols"
            ] += 1

    for row in observations:
        if not isinstance(row, dict) or row.get("request_class") not in {
            "gas-uik-direct-protocol",
            "gas-tik-direct-protocol",
        }:
            continue
        if int(row.get("status", 0)) != 200 or not row.get("body_path"):
            continue
        key = (int(row["year"]), str(row["entity_tvd"]), str(row["contest"]))
        aggregate = columns.get(key)
        if aggregate is None:
            failures.append({"url": row["url"], "reason": "no paired column report"})
            continue
        protocol = extract_direct_protocol(
            (store_root / str(row["body_path"])).read_bytes(),
            commission_name=str(row.get("uik_label") or row["entity_label"]),
        )
        direct = {**protocol["accounting"], **protocol["votes"]}
        if row["request_class"] == "gas-uik-direct-protocol":
            wanted = _uik_number(str(row["uik_label"]))
            indices = [
                index
                for index, label in enumerate(aggregate["labels"])
                if _uik_number(label) == wanted
            ]
            if not indices:
                failures.append({"url": row["url"], "reason": "UIK column missing"})
                continue
            column_index = indices[0] + 3
        else:
            column_index = 2
        compared = 0
        mismatched: list[str] = []
        for values in aggregate["table"]:
            if len(values) <= column_index or values[1] not in direct:
                continue
            compared += 1
            if _integer(values[column_index]) != direct[values[1]]:
                mismatched.append(values[1])
        if mismatched:
            failures.append(
                {
                    "url": row["url"],
                    "reason": "direct protocol differs from aggregate column",
                    "labels": mismatched,
                }
            )
        else:
            summary(row)["direct_aggregate_rows_reconciled"] += compared

    return {
        "schema_version": 1,
        "valid": not failures,
        "failures": failures,
        "summaries": [
            {"year": year, "contest": contest, **dict(sorted(values.items()))}
            for (year, contest), values in sorted(summaries.items())
        ],
    }


def validate_samples(args: argparse.Namespace) -> int:
    report = load_object(args.probe_report)
    store_root = Path(str(report.get("plan", {}).get("raw_dir", DEFAULT_RAW)))
    result = validate_sample_observations(report, store_root)
    json_write(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


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


def extract_direct_protocol(
    payload: bytes, *, commission_name: str = ""
) -> dict[str, Any]:
    source, encoding = decode_text(payload)
    document = lxml_html.fromstring(source)
    decoded_tables = decode_script_tables(source)
    if decoded_tables:
        tables = decoded_tables
    else:
        tables = [
            [
                [
                    " ".join(
                        (
                            cell.xpath("string(.//b[1])")
                            if index == 2 and cell.xpath(".//b[1]")
                            else cell.text_content()
                        ).split()
                    )
                    for index, cell in enumerate(row.xpath("./th|./td"))
                ]
                for row in table.xpath(".//tr")
            ]
            for table in document.xpath("//table")
        ]
    numbered: dict[int, tuple[str, int]] = {}
    for table in tables:
        for values in table:
            if len(values) < 2:
                continue
            if values[0] == "Наименование Избирательной комиссии":
                commission_name = commission_name or values[1]
                continue
            if len(values) < 3 or not values[0].isdigit():
                continue
            label = values[1]
            match = INTEGER_RE.match(values[2])
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
            if "действительных" in label.casefold()
            and "бюллетен" in label.casefold()
            and "недействительных" not in label.casefold()
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
    direct_classes = {
        "gas-tik-summary",
        "gas-tik-direct-protocol",
        "gas-uik-direct-protocol",
    }
    selected_years = set(args.year)
    generated = 0
    generated_years: set[int] = set()
    relations: dict[int, dict[str, dict[str, Any]]] = {}
    destinations: set[Path] = set()
    sortable = [row for row in observations if isinstance(row, dict)]
    sortable.sort(
        key=lambda row: (
            int(row.get("year", 0)),
            str(row.get("request_class", "")),
            int(row.get("report_type", 0)),
            str(row.get("entity_tvd", "")),
            str(row.get("uik_tvd", "")),
        )
    )
    for row in sortable:
        if (
            row.get("request_class") not in direct_classes
            or int(row.get("status", 0)) != 200
            or not row.get("body_path")
        ):
            continue
        year = int(row["year"])
        if selected_years and year not in selected_years:
            continue
        report_type = int(
            row.get("report_type")
            or urllib.parse.parse_qs(urllib.parse.urlsplit(str(row["url"])).query)[
                "type"
            ][0]
        )
        is_uik = row.get("request_class") == "gas-uik-direct-protocol"
        level = "uik" if is_uik else "tik"
        entity_tvd = str(row["uik_tvd"] if is_uik else row["entity_tvd"])
        commission_name = str(
            row.get("uik_label") if is_uik else row.get("entity_label") or ""
        )
        protocol = extract_direct_protocol(
            (store_root / row["body_path"]).read_bytes(),
            commission_name=commission_name,
        )
        extraction_method = (
            "randomized-inline-javascript-direct-protocol"
            if row.get("family") == "randomized-inline-javascript"
            else "plain-html-direct-protocol"
        )
        value = {
            "election": f"{year}-duma",
            "electionVrn": str(row["election_vrn"]),
            "protocol": str(row.get("contest") or "party"),
            "reportType": report_type,
            "commission": {
                "level": level,
                "name": protocol["commission_name"],
                "tvd": entity_tvd,
                "tikTvd": str(row["entity_tvd"]) if is_uik else None,
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
                "extractionMethod": extraction_method,
                "derivation": "direct",
                "hierarchyEvidence": {
                    "path": row.get("hierarchy_evidence_path"),
                    "sha256": row.get("hierarchy_evidence_sha256"),
                }
                if row.get("hierarchy_evidence_sha256")
                else None,
            },
        }
        destination = (
            args.output_root
            / f"{year}-duma"
            / "protocol"
            / ("uik" if is_uik else "tic")
            / str(report_type)
            / f"{entity_tvd}.ts"
        )
        if destination in destinations:
            raise ValueError(f"duplicate generated protocol destination: {destination}")
        destinations.add(destination)
        declaration = f"duma_{year}_{level}_{report_type}_{entity_tvd}"
        protocol_type = "HistoricalUikProtocol" if is_uik else "HistoricalTikProtocol"
        content = (
            "// This file is autogenerated by proper-data/crawler/historical.py.\n"
            "// Do not edit it manually.\n\n"
            f'import type {{ {protocol_type} }} from "../../types";\n\n'
            f"export const {declaration} = {_ts_literal(value)} satisfies {protocol_type};\n"
        )
        atomic_write(destination, content.encode("utf-8"))
        generated += 1
        generated_years.add(year)
        if is_uik:
            relations.setdefault(year, {})[entity_tvd] = {
                "uikTvd": entity_tvd,
                "uikNumber": _uik_number(str(row["uik_label"])),
                "tikTvd": str(row["entity_tvd"]),
                "tikName": row["entity_label"],
                "region": row.get("region_label"),
                "hierarchyEvidence": {
                    "path": row.get("hierarchy_evidence_path"),
                    "sha256": row.get("hierarchy_evidence_sha256"),
                },
            }
    if not generated:
        raise ValueError("no verified direct protocols matched")
    for year in sorted(generated_years):
        types = f'''// This file is autogenerated by proper-data/crawler/historical.py.
// Do not edit it manually.

export type HistoricalProtocolSource = Readonly<{{
  requestedUrl: string;
  finalUrl: string | null;
  retrievedAt: string | null;
  archiveCaptureTimestamp: string | null;
  sha256: string;
  provenance: "live-official" | "wayback";
  encoding: string;
  extractionMethod: "plain-html-direct-protocol" | "randomized-inline-javascript-direct-protocol";
  derivation: "direct";
  hierarchyEvidence: Readonly<{{ path: string | null; sha256: string | null }}> | null;
}}>;

type HistoricalProtocol = Readonly<{{
  election: "{year}-duma";
  electionVrn: string;
  protocol: "party" | "candidate";
  reportType: number;
  accounting: Readonly<Record<string, number>>;
  votes: Readonly<Record<string, number>>;
  validation: Readonly<{{
    vote_sum: number;
    valid_ballots: number | null;
    vote_sum_matches_valid_ballots: boolean;
  }}>;
  source: HistoricalProtocolSource;
}}>;

export type HistoricalTikProtocol = HistoricalProtocol & Readonly<{{
  commission: Readonly<{{ level: "tik"; name: string; tvd: string; tikTvd: null; region: string | null }}>;
}}>;

export type HistoricalUikProtocol = HistoricalProtocol & Readonly<{{
  commission: Readonly<{{ level: "uik"; name: string; tvd: string; tikTvd: string; region: string | null }}>;
}}>;
'''
        atomic_write(
            args.output_root / f"{year}-duma" / "protocol" / "types.ts",
            types.encode("utf-8"),
        )
        relation_value = [relations[year][key] for key in sorted(relations.get(year, {}))]
        relation_source = (
            "// This file is autogenerated by proper-data/crawler/historical.py.\n"
            "// Do not edit it manually.\n\n"
            f"export const duma_{year}_uik_to_tik = "
            f"{_ts_literal(relation_value)} as const;\n"
        )
        atomic_write(
            args.output_root / f"{year}-duma" / "uik-to-tik.ts",
            relation_source.encode("utf-8"),
        )
    print(
        json.dumps(
            {
                "generated_protocols": generated,
                "years": sorted(generated_years),
                "output_root": str(args.output_root),
            },
            indent=2,
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
    command = sub.add_parser("make-sample-spec")
    command.add_argument("--hierarchy-report", type=Path, required=True)
    command.add_argument("--selections", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.set_defaults(func=make_sample_spec)
    command = sub.add_parser("validate-samples")
    command.add_argument("--probe-report", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command.set_defaults(func=validate_samples)
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
