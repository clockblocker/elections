from __future__ import annotations

import argparse
import concurrent.futures
import json
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from .common import decode_text, extract_tree_nodes, json_write
    from .decode_script_result import decode_script_tables
    from .export_uik_results import indexed_uiks, transpose_table
    from .pipeline import (
        classify_result,
        coverage_report,
        hierarchy_summary,
        make_plan,
        reconcile,
        tree_endpoint,
    )
    from .shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from .transport import FetchConfig, Fetcher, ResponseStore
except ImportError:
    from common import decode_text, extract_tree_nodes, json_write
    from decode_script_result import decode_script_tables
    from export_uik_results import indexed_uiks, transpose_table
    from pipeline import (
        classify_result,
        coverage_report,
        hierarchy_summary,
        make_plan,
        reconcile,
        tree_endpoint,
    )
    from shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from transport import FetchConfig, Fetcher, ResponseStore


DEFAULT_RAW = Path("data/raw/gas-duma-2021")
DEFAULT_REPORTS = Path("reports/generated/gas-duma-2021")


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain an object")
    return value


def _settings(args: argparse.Namespace) -> tuple[ResponseStore, Fetcher]:
    store = ResponseStore(args.raw_dir)
    limiter = SharedRateLimiter(
        args.rate, coordination_dir=args.coordination_dir, jitter=args.jitter
    )
    fetcher = Fetcher(
        store,
        limiter,
        FetchConfig(args.timeout, args.retries, args.backoff_initial, args.backoff_max),
    )
    return store, fetcher


def _body(store: ResponseStore, record: dict[str, Any]) -> bytes:
    return (store.root / record["body_path"]).read_bytes()


def discover(args: argparse.Namespace) -> int:
    store, fetcher = _settings(args)
    root_payload = args.root_html.read_bytes()
    root_nodes, encoding = extract_tree_nodes(root_payload, args.root_url)
    nodes = {node.node_id: node for node in root_nodes if node.node_id}
    selected_regions = set(args.region or [])
    queue = [
        node
        for node in root_nodes
        if node.load_on_demand
        and (not selected_regions or node.region in selected_regions)
    ]
    processed: set[str] = set()
    while queue:
        node = queue.pop(0)
        if not node.node_id or node.node_id in processed:
            continue
        processed.add(node.node_id)
        record = fetcher.fetch(
            tree_endpoint({"vrn": node.vrn, "tvd": node.tvd}), refresh=args.refresh
        )
        if "body_path" not in record or int(record.get("status", 0)) != 200:
            continue
        children, _ = extract_tree_nodes(
            _body(store, record), record.get("final_url", ""), node.node_id
        )
        for child in children:
            if child.node_id == node.node_id:
                continue
            nodes[child.node_id] = child
            if child.load_on_demand:
                queue.append(child)
        if len(processed) % 25 == 0:
            print(
                json.dumps(
                    {
                        "hierarchy_requests": len(processed),
                        "nodes": len(nodes),
                        "pending": len(queue),
                    }
                ),
                flush=True,
            )
    output_nodes = [vars(nodes[key]) for key in sorted(nodes)]
    result = {
        "schema_version": 1,
        "root_source": str(args.root_html),
        "root_encoding": encoding,
        "nodes": output_nodes,
        **hierarchy_summary(output_nodes),
    }
    json_write(args.output, result)
    print(
        json.dumps(
            {key: result[key] for key in ("regions", "tiks", "uiks", "complete")},
            indent=2,
        )
    )
    return 0


def plan(args: argparse.Namespace) -> int:
    hierarchy = _load(args.hierarchy)
    result = make_plan(
        hierarchy["nodes"],
        direct_uik=args.direct_uik,
        region_filter=set(args.region or []),
    )
    result["rate"] = args.rate
    result["concurrency"] = args.concurrency
    result["raw_dir"] = str(args.raw_dir)
    result["manifest"] = str(args.raw_dir / "manifest.json")
    result["coordination_dir"] = str(args.coordination_dir)
    result["output_layout"] = "proper-data/2021-duma/protocol/{tic,uik}/{type}/{id}.ts"
    if result["hierarchy"]["complete"]:
        result["resume_command"] = (
            f"python3 proper-data/crawler/duma2021.py crawl --plan {args.output}"
        )
    else:
        result["blocked_reason"] = "hierarchy has unresolved load-on-demand nodes"
        result["resume_command"] = (
            "backend/.venv/bin/python proper-data/crawler/duma2021.py discover --root-html data/raw/duma-2021-single-member-cec/index/a52134a1b6d1e88209d6.html --output reports/generated/gas-duma-2021/hierarchy.json"
        )
    json_write(args.output, result)
    printable = {
        key: result[key]
        for key in (
            "hierarchy",
            "request_classes",
            "estimated_requests",
            "rate",
            "concurrency",
            "raw_dir",
            "manifest",
            "resume_command",
            "output_layout",
        )
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2))
    return 0


def crawl(args: argparse.Namespace) -> int:
    plan_data = _load(args.plan)
    if not plan_data.get("hierarchy", {}).get("complete"):
        raise SystemExit(
            "refusing crawl: hierarchy plan is incomplete; resume discovery first"
        )
    store, fetcher = _settings(args)
    requests = plan_data["requests"]
    if args.only_failures:
        requests = [
            item
            for item in requests
            if not store.verified(item["url"])
            or int(store.verified(item["url"]).get("status", 0)) not in range(200, 300)
        ]
    stats: Counter[str] = Counter()
    started = time.monotonic()
    observations: list[dict[str, Any]] = []

    def run(item: dict[str, Any]) -> dict[str, Any]:
        record = fetcher.fetch(item["url"], refresh=args.refresh)
        classification = {}
        if record.get("body_path"):
            classification = classify_result(
                _body(store, record), int(item["report_type"])
            )
        return {**item, **record, **classification}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run, item) for item in requests]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            row = future.result()
            observations.append(row)
            stats[str(row.get("status", row.get("error_class", "error")))] += 1
            if row.get("retry_count"):
                stats["retries"] += int(row["retry_count"])
            if index % args.progress_every == 0 or index == len(futures):
                elapsed = max(0.001, time.monotonic() - started)
                rps = index / elapsed
                remaining = len(futures) - index
                print(
                    json.dumps(
                        {
                            "completed": index,
                            "total": len(futures),
                            "rolling_rps": round(rps, 2),
                            "statuses": dict(stats),
                            "valid_tables": sum(
                                bool(item.get("valid_result")) for item in observations
                            ),
                            "eta_seconds": round(remaining / rps),
                        }
                    ),
                    flush=True,
                )
    observations.sort(key=lambda item: item["url"])
    json_write(
        args.report,
        {"schema_version": 1, "plan": str(args.plan), "observations": observations},
    )
    print(
        json.dumps(
            {
                "report": str(args.report),
                "observations": len(observations),
                "statuses": dict(stats),
            },
            indent=2,
        )
    )
    return 0 if all(item.get("valid_result") for item in observations) else 2


def validate(args: argparse.Namespace) -> int:
    hierarchy = _load(args.hierarchy)
    crawl_report = _load(args.crawl_report)
    report = coverage_report(hierarchy["nodes"], crawl_report["observations"])
    if args.protocols:
        protocols = _load(args.protocols)
        failed_tiks = {
            str(item["tik_tvd"])
            for item in protocols.get("reconciliation_failures", [])
        }
        region_tiks: defaultdict[str, set[str]] = defaultdict(set)
        for relation in hierarchy.get(
            "uik_to_tik", hierarchy_summary(hierarchy["nodes"])["uik_to_tik"]
        ):
            region_tiks[str(relation.get("region", ""))].add(str(relation["tik_tvd"]))
        for row in report["regions"]:
            row["reconciliation_status"] = (
                "failed" if region_tiks[row["region"]] & failed_tiks else "passed"
            )
    json_write(args.output, report)
    missing = sum(bool(row["missing_report_types"]) for row in report["regions"])
    print(
        json.dumps(
            {
                "coverage": str(args.output),
                "regions": len(report["regions"]),
                "regions_incomplete": missing,
            },
            indent=2,
        )
    )
    return 2 if missing else 0


def _aggregate(rows: list[list[str]]) -> dict[str, dict[str, int]]:
    header = next((index for index, row in enumerate(rows) if "Сумма" in row), None)
    if header is None:
        return {"accounting": {}, "votes": {}}
    column = rows[header].index("Сумма")
    data = [row for row in rows[header + 1 :] if len(row) > max(1, column)]

    def number(value: str) -> int:
        return int(value.replace("\u00a0", " ").split()[0])

    return {
        "accounting": {row[1]: number(row[column]) for row in data[:12]},
        "votes": {row[1]: number(row[column]) for row in data[12:]},
    }


def _direct_protocol(rows: list[list[str]]) -> dict[str, dict[str, int]]:
    data = [row for row in rows if len(row) >= 3 and row[0].strip().isdigit()]
    if len(data) < 13:
        raise ValueError("direct UIK protocol has too few rows")

    def number(value: str) -> int:
        return int(value.replace("\u00a0", " ").split()[0])

    return {
        "accounting": {row[1]: number(row[2]) for row in data[:12]},
        "votes": {row[1]: number(row[2]) for row in data[12:]},
    }


def build(args: argparse.Namespace) -> int:
    """Transpose preserved TIK columns, pair ballots, and reconcile to TIK sums."""
    hierarchy = _load(args.hierarchy)
    crawl_report = _load(args.crawl_report)
    store = ResponseStore(args.raw_dir)
    tree = {"nodes": hierarchy["nodes"]}
    paired: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    sources: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    failures: list[dict[str, Any]] = []
    direct: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    conflicts: list[dict[str, Any]] = []
    tik_names = {str(node["node_id"]): str(node["text"]) for node in hierarchy["nodes"]}
    for observation in crawl_report["observations"]:
        if args.tik and str(
            observation.get("tik_tvd") or observation.get("entity_id")
        ) not in set(args.tik):
            continue
        report_type = int(observation.get("report_type", 0))
        if (
            report_type not in (233, 242, 463, 464)
            or not observation.get("valid_result")
            or not observation.get("body_path")
        ):
            continue
        source, _ = decode_text(_body(store, observation))
        tables = decode_script_tables(source)
        rows = max(tables, key=lambda value: sum(map(len, value)), default=[])
        widths = Counter(map(len, rows))
        regular_width = max(widths, key=lambda width: (widths[width], width), default=0)
        rows = [row for row in rows if len(row) == regular_width]
        if report_type in (242, 463):
            uik_tvd = str(observation["entity_id"])
            kind = "party" if report_type == 242 else "candidate"
            direct[uik_tvd][kind] = {
                **_direct_protocol(rows),
                "source": {
                    "official_url": observation["requested_url"],
                    "final_url": observation.get("final_url"),
                    "sha256": observation["sha256"],
                    "retrieved_at": observation.get("retrieved_at"),
                    "provenance": observation.get("provenance"),
                },
            }
            continue
        tik_tvd = str(observation["entity_id"])
        kind = "party" if report_type == 233 else "candidate"
        records = transpose_table(
            {"rows": rows}, indexed_uiks(tree, tik_tvd), option_kind=kind
        )
        for record in records:
            item = paired[str(record["uik_tvd"])]
            item.update(
                {
                    "uik_number": record["uik_number"],
                    "uik_tvd": str(record["uik_tvd"]),
                    "tik_tvd": tik_tvd,
                    "tik_name": tik_names.get(tik_tvd, ""),
                    f"{kind}_accounting": record["accounting"],
                    f"{kind}_votes": record[f"{kind}_votes"],
                }
            )
        aggregate = _aggregate(rows)
        normalized = [
            {"accounting": record["accounting"], "votes": record[f"{kind}_votes"]}
            for record in records
        ]
        failures.extend(
            {"tik_tvd": tik_tvd, "report_type": report_type, **failure}
            for failure in reconcile(normalized, aggregate)
        )
        sources[tik_tvd][kind] = {
            "official_url": observation["requested_url"],
            "final_url": observation.get("final_url"),
            "sha256": observation["sha256"],
            "retrieved_at": observation.get("retrieved_at"),
            "provenance": observation.get("provenance"),
        }
    for uik_tvd, ballots in sorted(direct.items()):
        record = paired.get(uik_tvd)
        if record is None:
            conflicts.append(
                {"uik_tvd": uik_tvd, "error": "direct protocol absent from TIK columns"}
            )
            continue
        for kind, direct_protocol in ballots.items():
            differing = [
                field
                for field in ("accounting", "votes")
                if direct_protocol[field] != record.get(f"{kind}_{field}")
            ]
            if differing:
                conflicts.append(
                    {"uik_tvd": uik_tvd, "ballot": kind, "differing_fields": differing}
                )
            else:
                record[f"{kind}_source"] = direct_protocol["source"]
    required = {
        "party_accounting",
        "party_votes",
        "candidate_accounting",
        "candidate_votes",
    }
    complete = [
        record for _, record in sorted(paired.items()) if required.issubset(record)
    ]
    incomplete = [
        {"uik_tvd": key, "missing": sorted(required - set(record))}
        for key, record in sorted(paired.items())
        if not required.issubset(record)
    ]
    result = {
        "schema_version": 2,
        "election": "2021-duma",
        "records": complete,
        "sources": [
            {"tik_tvd": key, **value}
            for key, value in sorted(sources.items())
            if {"party", "candidate"}.issubset(value)
        ],
        "reconciliation_failures": failures,
        "duplicate_conflicts": conflicts,
        "incomplete_uiks": incomplete,
    }
    json_write(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "complete_uiks": len(complete),
                "incomplete_uiks": len(incomplete),
                "reconciliation_failures": len(failures),
                "duplicate_conflicts": len(conflicts),
            },
            indent=2,
        )
    )
    return 2 if incomplete or failures or conflicts else 0


def probe(args: argparse.Namespace) -> int:
    """Validate a small mix of freshly fetched and already preserved official pages."""
    spec = _load(args.spec)
    store, fetcher = _settings(args)
    observations = []
    for item in spec["requests"]:
        if item.get("source_path"):
            source_path = Path(item["source_path"])
            payload = source_path.read_bytes()
            record = store.save(
                item["url"],
                payload,
                {
                    "final_url": item["url"],
                    "retrieved_at": item.get("retrieved_at"),
                    "status": 200,
                    "content_type": "text/html",
                    "elapsed_seconds": 0,
                    "retry_count": 0,
                    "source_host": item.get("source_host", "old.izbirkom.ru"),
                    "provenance": item.get("provenance", "live-official"),
                    "imported_from": str(source_path),
                },
            )
        else:
            record = fetcher.fetch(item["url"], refresh=args.refresh)
            payload = _body(store, record) if record.get("body_path") else b""
        observations.append(
            {**item, **record, **classify_result(payload, int(item["report_type"]))}
        )
    regions = {str(item["region"]) for item in observations}
    tiks = {str(item["tik_tvd"]) for item in observations if item.get("tik_tvd")}
    report = {
        "schema_version": 1,
        "regions": sorted(regions),
        "tik_count": len(tiks),
        "observations": observations,
        "success": len(regions) >= 2
        and len(tiks) >= 2
        and {233, 464}.issubset(
            {
                int(item["report_type"])
                for item in observations
                if item.get("valid_result") and item.get("kind_matches_requested_type")
            }
        )
        and any(
            item.get("level") == "uik-direct-protocol"
            and item.get("valid_result")
            and item.get("kind_matches_requested_type")
            for item in observations
        ),
    }
    json_write(args.report, report)
    print(
        json.dumps(
            {
                "report": str(args.report),
                "regions": len(regions),
                "tiks": len(tiks),
                "valid": sum(bool(item.get("valid_result")) for item in observations),
                "success": report["success"],
            },
            indent=2,
        )
    )
    return 0 if report["success"] else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Restartable official 2021 State Duma crawler"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    common.add_argument("--rate", type=float, default=10.0)
    common.add_argument("--jitter", type=float, default=0.05)
    common.add_argument("--concurrency", type=int, default=6)
    common.add_argument("--timeout", type=float, default=20.0)
    common.add_argument("--retries", type=int, default=5)
    common.add_argument("--backoff-initial", type=float, default=0.5)
    common.add_argument("--backoff-max", type=float, default=30.0)
    common.add_argument("--refresh", action="store_true")
    common.add_argument(
        "--coordination-dir", type=Path, default=DEFAULT_COORDINATION_DIR
    )
    sub = result.add_subparsers(dest="command", required=True)
    command = sub.add_parser("discover", parents=[common])
    command.set_defaults(func=discover)
    command.add_argument("--root-html", type=Path, required=True)
    command.add_argument(
        "--root-url",
        default="http://old.izbirkom.ru/region/izbirkom?action=show&root=0&tvd=100100225883177&vrn=100100225883172&region=0&sub_region=0",
    )
    command.add_argument("--region", action="append")
    command.add_argument(
        "--output", type=Path, default=DEFAULT_REPORTS / "hierarchy.json"
    )
    command = sub.add_parser("plan", parents=[common])
    command.set_defaults(func=plan)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--direct-uik", action="store_true")
    command.add_argument("--region", action="append")
    command.add_argument("--output", type=Path, default=DEFAULT_REPORTS / "plan.json")
    command = sub.add_parser("crawl", parents=[common])
    command.set_defaults(func=crawl)
    command.add_argument("--plan", type=Path, required=True)
    command.add_argument("--only-failures", action="store_true")
    command.add_argument("--progress-every", type=int, default=100)
    command.add_argument("--report", type=Path, default=DEFAULT_REPORTS / "crawl.json")
    command = sub.add_parser("validate")
    command.set_defaults(func=validate)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--crawl-report", type=Path, required=True)
    command.add_argument("--protocols", type=Path)
    command.add_argument(
        "--output", type=Path, default=DEFAULT_REPORTS / "coverage.json"
    )
    command = sub.add_parser("build")
    command.set_defaults(func=build)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--crawl-report", type=Path, required=True)
    command.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    command.add_argument("--tik", action="append")
    command.add_argument(
        "--output", type=Path, default=DEFAULT_REPORTS / "protocols.json"
    )
    command = sub.add_parser("probe", parents=[common])
    command.set_defaults(func=probe)
    command.add_argument("--spec", type=Path, required=True)
    command.add_argument("--report", type=Path, default=DEFAULT_REPORTS / "probe.json")
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
