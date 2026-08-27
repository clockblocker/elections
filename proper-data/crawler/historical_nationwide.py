from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import time
import urllib.parse
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

try:
    from .common import decode_text, extract_tree_nodes, json_write
    from .decode_script_result import decode_script_tables
    from .historical import extract_direct_protocol
    from .pipeline import (
        classify_result,
        extract_report_links,
        hierarchy_summary,
        reconcile,
        tree_endpoint,
    )
    from .shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from .transport import FetchConfig, Fetcher, ResponseStore
except ImportError:
    from common import decode_text, extract_tree_nodes, json_write
    from decode_script_result import decode_script_tables
    from historical import extract_direct_protocol
    from pipeline import (
        classify_result,
        extract_report_links,
        hierarchy_summary,
        reconcile,
        tree_endpoint,
    )
    from shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from transport import FetchConfig, Fetcher, ResponseStore


HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "historical-nationwide.json"
UIK_RE = re.compile(r"^(?:УИК|Участок)\s*№?\s*(\d+)$", re.IGNORECASE)


def load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain an object")
    return value


def election(args: argparse.Namespace) -> dict[str, Any]:
    row = load(args.config).get("elections", {}).get(str(args.year))
    if not isinstance(row, dict):
        raise TypeError(f"no nationwide configuration for {args.year}")
    contests = row.get("contests")
    if not isinstance(contests, dict) or not contests:
        raise ValueError(f"{args.year} has no configured contests")
    return {"year": args.year, "election": f"{args.year}-duma", **row}


def report_kind(config: dict[str, Any]) -> dict[int, tuple[str, str]]:
    result: dict[int, tuple[str, str]] = {}
    for contest, types in config["contests"].items():
        result[int(types["tic"])] = ("tic", contest)
        result[int(types["uik"])] = ("uik", contest)
    return result


def hierarchy_result_url(
    config: dict[str, Any],
    tik: dict[str, Any],
    children: list[dict[str, Any]],
    report_type: int,
) -> str:
    """Build a report URL strictly from verified hierarchy fields."""
    roots = {str(child["root"]) for child in children if child.get("root")}
    if len(roots) != 1:
        raise ValueError(
            f"TIK {tik['node_id']} has {len(roots)} UIK roots: {sorted(roots)}"
        )
    original = urllib.parse.urlsplit(str(tik["url"]))
    query = urllib.parse.urlencode(
        {
            "action": "show",
            "root": next(iter(roots)),
            "vrn": config["election_vrn"],
            "global": "true",
            "prver": "0",
            "pronetvd": "0" if config["year"] in (2003, 2016) else "null",
            "tvd": str(tik["node_id"]),
            "region": "0",
            "sub_region": "0",
            "type": str(report_type),
        }
    )
    return urllib.parse.urlunsplit(
        (
            original.scheme or "http",
            original.netloc or "old.izbirkom.ru",
            original.path,
            query,
            "",
        )
    )


def direct_result_url(
    config: dict[str, Any], node: dict[str, Any], report_type: int
) -> str:
    original = urllib.parse.urlsplit(str(node["url"]))
    query = urllib.parse.urlencode(
        {
            "action": "show",
            "root": str(node["root"]),
            "vrn": config["election_vrn"],
            "global": "true",
            "prver": "0",
            "pronetvd": "0" if config["year"] in (2003, 2016) else "null",
            "tvd": str(node["node_id"]),
            "region": str(node.get("region") or "0"),
            "sub_region": str(node.get("sub_region") or "0"),
            "vibid": str(node["node_id"]),
            "type": str(report_type),
        }
    )
    return urllib.parse.urlunsplit(
        (
            original.scheme or "http",
            original.netloc or "old.izbirkom.ru",
            original.path,
            query,
            "",
        )
    )


def settings(args: argparse.Namespace) -> tuple[ResponseStore, Fetcher]:
    store = ResponseStore(args.raw_dir)
    fetcher = Fetcher(
        store,
        SharedRateLimiter(
            args.rate, coordination_dir=args.coordination_dir, jitter=args.jitter
        ),
        FetchConfig(args.timeout, args.retries, args.backoff_initial, args.backoff_max),
    )
    return store, fetcher


def body(store: ResponseStore, record: dict[str, Any]) -> bytes:
    return (store.root / str(record["body_path"])).read_bytes()


def discover(args: argparse.Namespace) -> int:
    config = election(args)
    kinds = report_kind(config)
    tic_types = {value["tic"] for value in config["contests"].values()}
    store, fetcher = settings(args)
    root_record = fetcher.fetch(config["root_url"], refresh=args.refresh)
    if int(root_record.get("status", 0)) != 200 or not root_record.get("body_path"):
        raise SystemExit(f"failed to fetch hierarchy root: {root_record}")
    root_nodes, encoding = extract_tree_nodes(
        body(store, root_record), config["root_url"]
    )
    nodes = {node.node_id: node for node in root_nodes if node.node_id}
    queue = [node for node in root_nodes if node.load_on_demand]
    processed: set[str] = set()
    resolved_empty: set[str] = set()

    def expand(node: Any) -> tuple[Any, list[Any], dict[str, Any]]:
        record = fetcher.fetch(
            tree_endpoint({"vrn": config["election_vrn"], "tvd": node.tvd}),
            refresh=args.refresh,
        )
        if int(record.get("status", 0)) != 200 or not record.get("body_path"):
            return node, [], record
        children, _ = extract_tree_nodes(
            body(store, record), str(record.get("final_url", "")), node.node_id
        )
        return (
            node,
            [child for child in children if child.node_id != node.node_id],
            record,
        )

    failures: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures: dict[concurrent.futures.Future[Any], Any] = {}
        while queue or futures:
            while queue and len(futures) < args.concurrency:
                node = queue.pop(0)
                if not node.node_id or node.node_id in processed:
                    continue
                processed.add(node.node_id)
                futures[pool.submit(expand, node)] = node
            if not futures:
                break
            done, _ = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                futures.pop(future)
                parent, children, record = future.result()
                if not children and int(record.get("status", 0)) != 200:
                    failures.append({"node_id": parent.node_id, **record})
                elif not children and parent.node_id:
                    resolved_empty.add(str(parent.node_id))
                for child in children:
                    if child.node_id:
                        nodes[child.node_id] = child
                        if child.load_on_demand:
                            queue.append(child)
                if len(processed) % 100 == 0:
                    print(
                        json.dumps(
                            {
                                "year": args.year,
                                "hierarchy_requests": len(processed),
                                "nodes": len(nodes),
                                "pending": len(queue) + len(futures),
                            }
                        ),
                        flush=True,
                    )

    output_nodes = [
        {
            **vars(nodes[key]),
            "load_on_demand": False
            if key in resolved_empty
            else nodes[key].load_on_demand,
        }
        for key in sorted(nodes)
    ]
    summary = hierarchy_summary(output_nodes)
    by_id = {str(node["node_id"]): node for node in output_nodes}
    tik_ids = sorted({str(item["tik_tvd"]) for item in summary["uik_to_tik"]})
    children_by_parent: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for child in output_nodes:
        if child.get("parent_id"):
            children_by_parent[str(child["parent_id"])].append(child)

    def resolve(
        tik_id: str,
    ) -> tuple[str, dict[str, str], dict[str, str], dict[str, Any]]:
        node = by_id[tik_id]
        if config["year"] == 2003:
            found = {
                str(report_type): hierarchy_result_url(
                    config, node, children_by_parent[tik_id], int(report_type)
                )
                for report_type in tic_types
            }
            return (
                tik_id,
                found,
                {key: "official-hierarchy-derived" for key in found},
                {"status": "not-required", "evidence": "recursive-hierarchy"},
            )
        record = fetcher.fetch(str(node["url"]), refresh=args.refresh)
        if int(record.get("status", 0)) != 200 or not record.get("body_path"):
            return tik_id, {}, {}, record
        links = extract_report_links(
            body(store, record),
            str(record.get("final_url", node["url"])),
            election_vrn=config["election_vrn"],
            report_kind=kinds,
        )
        found = {
            str(item["report_type"]): str(item["url"])
            for item in links
            if int(item["report_type"]) in tic_types
        }
        sources = {key: "official-navigation" for key in found}
        for report_type in tic_types:
            key = str(report_type)
            if key not in found:
                found[key] = hierarchy_result_url(
                    config, node, children_by_parent[tik_id], int(report_type)
                )
                sources[key] = "official-hierarchy-derived"
        return tik_id, found, sources, record

    links: dict[str, dict[str, str]] = {}
    link_sources: dict[str, dict[str, str]] = {}
    link_failures: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(resolve, tik_id) for tik_id in tik_ids]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            tik_id, found, sources, record = future.result()
            links[tik_id] = found
            link_sources[tik_id] = sources
            if set(map(str, tic_types)) - set(found):
                link_failures.append(
                    {"tik_tvd": tik_id, "found": sorted(found), "record": record}
                )
            if index % 250 == 0 or index == len(futures):
                print(
                    json.dumps(
                        {
                            "year": args.year,
                            "report_link_requests": index,
                            "total": len(futures),
                            "complete": index - len(link_failures),
                        }
                    ),
                    flush=True,
                )

    required = set(map(str, tic_types))
    result = {
        "schema_version": 1,
        "election": config["election"],
        "election_vrn": config["election_vrn"],
        "contests": config["contests"],
        "root_url": config["root_url"],
        "root_encoding": encoding,
        "nodes": output_nodes,
        **summary,
        "hierarchy_failures": failures,
        "resolved_empty_nodes": sorted(resolved_empty),
        "report_links": dict(sorted(links.items())),
        "report_link_sources": dict(sorted(link_sources.items())),
        "report_link_failures": link_failures,
        "report_links_complete": all(
            required.issubset(links.get(tik, {})) for tik in tik_ids
        ),
    }
    result["complete"] = bool(summary["complete"] and not failures)
    store.flush()
    json_write(args.output, result)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "election",
                    "regions",
                    "tiks",
                    "uiks",
                    "complete",
                    "report_links_complete",
                )
            },
            indent=2,
        )
    )
    return 0 if result["complete"] and result["report_links_complete"] else 2


def plan(args: argparse.Namespace) -> int:
    hierarchy = load(args.hierarchy)
    requests = []
    for tik_tvd, links in sorted(hierarchy["report_links"].items()):
        node = next(
            item for item in hierarchy["nodes"] if str(item["node_id"]) == str(tik_tvd)
        )
        for contest, types in hierarchy["contests"].items():
            report_type = int(types["tic"])
            requests.append(
                {
                    "class": f"tic-{report_type}",
                    "contest": contest,
                    "report_type": report_type,
                    "entity_id": str(tik_tvd),
                    "region": str(node.get("region") or ""),
                    "url": links.get(str(report_type)),
                    "url_source": hierarchy.get("report_link_sources", {})
                    .get(str(tik_tvd), {})
                    .get(str(report_type), "official-navigation"),
                }
            )
    ready = bool(
        hierarchy.get("complete")
        and hierarchy.get("report_links_complete")
        and all(item["url"] for item in requests)
    )
    result = {
        "schema_version": 1,
        "election": hierarchy["election"],
        "election_vrn": hierarchy["election_vrn"],
        "contests": hierarchy["contests"],
        "hierarchy": {
            key: hierarchy[key] for key in ("regions", "tiks", "uiks", "complete")
        },
        "request_classes": dict(Counter(item["class"] for item in requests)),
        "url_sources": dict(Counter(item["url_source"] for item in requests)),
        "estimated_requests": len(requests),
        "requests": requests,
        "rate": args.rate,
        "concurrency": args.concurrency,
        "raw_dir": str(args.raw_dir),
        "manifest": str(args.raw_dir / "manifest.json"),
        "ready": ready,
    }
    json_write(args.output, result)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "election",
                    "hierarchy",
                    "request_classes",
                    "estimated_requests",
                    "raw_dir",
                    "ready",
                )
            },
            indent=2,
        )
    )
    return 0 if ready else 2


def crawl(args: argparse.Namespace) -> int:
    plan_data = load(args.plan)
    if not plan_data.get("ready"):
        raise SystemExit("refusing crawl: plan is not ready")
    kinds = report_kind(plan_data)
    store, fetcher = settings(args)
    requests = list(plan_data["requests"])
    if args.only_failures:
        requests = [
            item
            for item in requests
            if not (saved := store.verified(item["url"]))
            or int(saved.get("status", 0)) not in range(200, 300)
        ]
    started = time.monotonic()
    stats: Counter[str] = Counter()

    def run(item: dict[str, Any]) -> dict[str, Any]:
        record = fetcher.fetch(item["url"], refresh=args.refresh or args.only_failures)
        payload = body(store, record) if record.get("body_path") else b""
        classification = classify_result(
            payload,
            int(item["report_type"]),
            election_vrn=plan_data["election_vrn"],
            report_kind=kinds,
        )
        if payload and not classification.get("valid_result"):
            source_text, rows = decoded_rows(payload)
            header = next(
                (
                    row
                    for row in rows
                    if any(UIK_RE.fullmatch(cell.strip()) for cell in row)
                ),
                [],
            )
            uik_numbers = [
                int(match.group(1))
                for cell in header
                if (match := UIK_RE.fullmatch(cell.strip()))
            ]
            accounting = [
                row
                for row in rows
                if len(row) >= 3 and row[1].casefold().startswith("число ")
            ]
            classification.update(
                {
                    "valid_result": bool(
                        uik_numbers
                        and len(accounting) >= 10
                        and plan_data["election_vrn"] in source_text
                    ),
                    "level": "tic-column-report" if uik_numbers else "unknown",
                    "uik_numbers": uik_numbers,
                    "row_count": len(rows),
                    "column_count": max(map(len, rows), default=0),
                    "election_vrn_matches": plan_data["election_vrn"] in source_text,
                }
            )
        classification["ballot"] = item["contest"]
        classification["kind_matches_requested_type"] = bool(
            classification.get("valid_result")
            and classification.get("level") == "tic-column-report"
        )
        return {**item, **record, **classification}

    observations: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {pool.submit(run, item) for item in requests}
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            row = future.result()
            observations.append(row)
            stats[str(row.get("status", row.get("error_class", "error")))] += 1
            if index % args.progress_every == 0 or index == len(futures):
                elapsed = max(0.001, time.monotonic() - started)
                rps = index / elapsed
                print(
                    json.dumps(
                        {
                            "election": plan_data["election"],
                            "completed": index,
                            "total": len(futures),
                            "rolling_rps": round(rps, 2),
                            "statuses": dict(stats),
                            "valid_tables": sum(
                                bool(item.get("valid_result")) for item in observations
                            ),
                            "eta_seconds": round((len(futures) - index) / rps),
                        }
                    ),
                    flush=True,
                )
    observations.sort(key=lambda item: item["url"])
    store.flush()
    result = {
        "schema_version": 1,
        "plan": str(args.plan),
        "election": plan_data["election"],
        "observations": observations,
    }
    json_write(args.report, result)
    valid = all(
        item.get("valid_result") and item.get("kind_matches_requested_type")
        for item in observations
    ) and len(observations) == len(requests)
    print(
        json.dumps(
            {
                "report": str(args.report),
                "observations": len(observations),
                "statuses": dict(stats),
                "valid": valid,
            },
            indent=2,
        )
    )
    return 0 if valid else 2


def number(value: str) -> int:
    match = re.match(r"\s*(\d+)", value.replace("\u00a0", " "))
    if not match:
        raise ValueError(f"not an integer cell: {value!r}")
    return int(match.group(1))


def decoded_rows(payload: bytes) -> tuple[str, list[list[str]]]:
    """Decode a result table and repair malformed 2003 text-only UIK headers."""
    source, _ = decode_text(payload)
    tables = decode_script_tables(source)
    for values in tables:
        if not values or not any(UIK_RE.fullmatch(cell.strip()) for cell in values[0]):
            continue
        width = len(values[0])
        labels = next(
            (
                table
                for table in tables
                if table is not values
                and len(table) == len(values)
                and any(
                    len(row) >= 2 and row[1].casefold().startswith("число ")
                    for row in table
                )
            ),
            None,
        )
        if labels is not None:
            combined = [["", "", "Сумма", *values[0]]]
            combined.extend(
                [label[0], label[1], label[2], *value]
                for label, value in zip(labels[1:], values[1:], strict=True)
                if len(label) == 3 and len(value) == width
            )
            return source, combined
    rows = max(tables, key=lambda value: sum(map(len, value)), default=[])
    widths = Counter(map(len, rows))
    width = max(widths, key=lambda value: (widths[value], value), default=0)
    regular = [row for row in rows if len(row) == width]
    if regular and not any(
        UIK_RE.fullmatch(cell.strip()) for row in regular for cell in row
    ):
        marker = source.find("<nobr>Сумма</nobr>")
        end = source.find("</thead>", marker)
        label_numbers = re.findall(
            r"(?:УИК|Участок)\s*№?\s*(\d+)",
            source[marker:end],
            re.IGNORECASE,
        )
        labels = [f"УИК №{value}" for value in label_numbers]
        if labels and width == len(labels) + 3:
            regular.insert(0, ["", "", "Сумма", *labels])
    return source, regular


def transpose(
    rows: list[list[str]], uiks: dict[int, dict[str, Any]], contest: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    header_index = next(
        index
        for index, row in enumerate(rows)
        if any(UIK_RE.fullmatch(cell.strip()) for cell in row)
    )
    header = rows[header_index]
    columns = {
        int(match.group(1)): index
        for index, cell in enumerate(header)
        if (match := UIK_RE.fullmatch(cell.strip()))
    }
    missing = sorted(set(columns) - set(uiks))
    if missing:
        raise ValueError(f"table UIKs absent from hierarchy: {missing}")
    data = [
        row
        for row in rows[header_index + 1 :]
        if len(row) == len(header) and len(row) >= 3 and row[1].strip()
    ]
    accounting = [row for row in data if row[1].casefold().startswith("число ")]
    votes = [row for row in data if not row[1].casefold().startswith("число ")]
    if len(accounting) < 10 or not votes:
        raise ValueError("protocol table lacks accounting or vote rows")
    records = []
    for uik_number, column in columns.items():
        node = uiks[uik_number]
        records.append(
            {
                "uik_number": uik_number,
                "uik_tvd": str(node["node_id"]),
                "tik_tvd": str(node["parent_id"]),
                "accounting": {row[1]: number(row[column]) for row in accounting},
                f"{contest}_votes": {row[1]: number(row[column]) for row in votes},
            }
        )
    aggregate = {
        "accounting": {row[1]: number(row[2]) for row in accounting},
        "votes": {row[1]: number(row[2]) for row in votes},
    }
    return records, aggregate


def recover_gaps(args: argparse.Namespace) -> int:
    config = election(args)
    hierarchy, protocols = load(args.hierarchy), load(args.protocols)
    by_id = {str(node["node_id"]): node for node in hierarchy["nodes"]}
    missing_by_uik: dict[str, set[str]] = defaultdict(set)
    for item in protocols.get("missing_hierarchy_uiks", []):
        missing_by_uik[str(item["uik_tvd"])].update(config["contests"])
    for item in protocols.get("incomplete_uiks", []):
        for field in item.get("missing", []):
            missing_by_uik[str(item["uik_tvd"])].add(str(field).split("_", 1)[0])
    requests = [
        {
            "class": f"uik-{types['uik']}",
            "contest": contest,
            "report_type": int(types["uik"]),
            "entity_id": uik_tvd,
            "tik_tvd": str(by_id[uik_tvd]["parent_id"]),
            "region": str(by_id[uik_tvd].get("region") or ""),
            "url": direct_result_url(config, by_id[uik_tvd], int(types["uik"])),
            "url_source": "official-hierarchy-derived",
        }
        for uik_tvd, contests in sorted(missing_by_uik.items())
        for contest, types in config["contests"].items()
        if contest in contests
    ]
    store, fetcher = settings(args)
    if args.only_failures:
        requests = [
            item
            for item in requests
            if not (saved := store.verified(item["url"]))
            or int(saved.get("status", 0)) not in range(200, 300)
        ]

    def run(item: dict[str, Any]) -> dict[str, Any]:
        record = fetcher.fetch(item["url"], refresh=args.refresh or args.only_failures)
        valid = False
        error: str | None = None
        if record.get("body_path") and int(record.get("status", 0)) == 200:
            try:
                protocol = extract_direct_protocol(
                    body(store, record),
                    commission_name=by_id[item["entity_id"]]["text"],
                )
                valid = bool(protocol["validation"]["vote_sum_matches_valid_ballots"])
            except (ValueError, TypeError) as caught:
                error = f"{type(caught).__name__}: {caught}"
        return {
            **item,
            **record,
            "valid_result": valid,
            "level": "uik-direct-protocol",
            "ballot": item["contest"],
            "kind_matches_requested_type": valid,
            "validation_error": error,
        }

    observations: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(run, item) for item in requests]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            observations.append(future.result())
            if index % 100 == 0 or index == len(futures):
                print(
                    json.dumps(
                        {
                            "election": config["election"],
                            "completed": index,
                            "total": len(futures),
                            "valid": sum(
                                bool(item.get("valid_result")) for item in observations
                            ),
                        }
                    ),
                    flush=True,
                )
    observations.sort(key=lambda item: item["url"])
    store.flush()
    json_write(
        args.report,
        {
            "schema_version": 1,
            "election": config["election"],
            "observations": observations,
        },
    )
    valid = all(item.get("valid_result") for item in observations)
    print(
        json.dumps(
            {
                "report": str(args.report),
                "observations": len(observations),
                "valid": sum(bool(item.get("valid_result")) for item in observations),
                "complete": valid,
            },
            indent=2,
        )
    )
    return 0 if valid else 2


def build(args: argparse.Namespace) -> int:
    hierarchy, crawl_report = load(args.hierarchy), load(args.crawl_report)
    observations = list(crawl_report["observations"])
    for path in args.additional_crawl_report or []:
        observations.extend(load(path)["observations"])
    store = ResponseStore(args.raw_dir)
    by_id = {str(node["node_id"]): node for node in hierarchy["nodes"]}
    relations = (
        hierarchy.get("uik_to_tik")
        or hierarchy_summary(hierarchy["nodes"])["uik_to_tik"]
    )
    relation_by_uik = {str(item["uik_tvd"]): item for item in relations}
    tik_names = {key: str(node["text"]) for key, node in by_id.items()}
    uiks_by_tik: defaultdict[str, dict[int, dict[str, Any]]] = defaultdict(dict)
    for relation in relations:
        uiks_by_tik[str(relation["tik_tvd"])][int(relation["uik_number"])] = by_id[
            str(relation["uik_tvd"])
        ]
    paired: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    sources: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    tik_protocols: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    failures: list[dict[str, Any]] = []
    build_errors: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    aggregate_tiks: set[tuple[str, str]] = set()
    kinds = report_kind(hierarchy)
    for observation in observations:
        if not observation.get("valid_result") or not observation.get("body_path"):
            continue
        report_type = int(observation["report_type"])
        level, configured_contest = kinds[report_type]
        contest = str(observation.get("contest") or configured_contest)
        if str(observation.get("class", "")).startswith("tic-") and (
            level == "uik" or observation.get("level") == "uik-direct-protocol"
        ):
            tik_tvd = str(observation["entity_id"])
            payload = body(store, observation)
            try:
                protocol = extract_direct_protocol(
                    payload, commission_name=tik_names.get(tik_tvd, "")
                )
            except ValueError:
                _, rows = decoded_rows(payload)
                data = [row for row in rows if len(row) >= 3 and row[1].strip()]
                accounting = [
                    row for row in data if row[1].casefold().startswith("число ")
                ]
                votes = [
                    row
                    for row in data
                    if not row[1].casefold().startswith("число ")
                    and re.match(r"\s*\d+", row[2])
                ]
                if len(accounting) < 10 or not votes:
                    raise ValueError("page has no aggregate commission protocol")
                protocol = {
                    "accounting": {row[1]: number(row[2]) for row in accounting},
                    "votes": {row[1]: number(row[2]) for row in votes},
                }
            uik_nodes = uiks_by_tik[tik_tvd]
            tik_protocols[tik_tvd][contest] = {
                "accounting": protocol["accounting"],
                "votes": protocol["votes"],
                "uik_count": len(uik_nodes),
                "uik_tvds": [
                    str(uik_nodes[number]["node_id"]) for number in sorted(uik_nodes)
                ],
            }
            sources[tik_tvd][contest] = {
                "official_url": observation["requested_url"],
                "final_url": observation.get("final_url"),
                "sha256": observation["sha256"],
                "retrieved_at": observation.get("retrieved_at"),
                "provenance": observation.get("provenance"),
                "report_type": report_type,
                "derivation": "direct",
            }
            aggregate_tiks.add((tik_tvd, contest))
            continue
        if level == "uik":
            uik_tvd = str(observation["entity_id"])
            relation = relation_by_uik[uik_tvd]
            protocol = extract_direct_protocol(
                body(store, observation), commission_name=by_id[uik_tvd]["text"]
            )
            record = paired[uik_tvd]
            differing = [
                field
                for field in ("accounting", "votes")
                if f"{contest}_{field}" in record
                and record[f"{contest}_{field}"] != protocol[field]
            ]
            if differing:
                conflicts.append(
                    {
                        "uik_tvd": uik_tvd,
                        "contest": contest,
                        "differing_fields": differing,
                    }
                )
                continue
            record.update(
                {
                    "uik_number": relation["uik_number"],
                    "uik_tvd": uik_tvd,
                    "tik_tvd": str(relation["tik_tvd"]),
                    "tik_name": tik_names.get(str(relation["tik_tvd"]), ""),
                    "region": str(relation.get("region", "")),
                    f"{contest}_accounting": protocol["accounting"],
                    f"{contest}_votes": protocol["votes"],
                    f"{contest}_source": {
                        "official_url": observation["requested_url"],
                        "final_url": observation.get("final_url"),
                        "sha256": observation["sha256"],
                        "retrieved_at": observation.get("retrieved_at"),
                        "provenance": observation.get("provenance"),
                        "report_type": report_type,
                        "derivation": "direct",
                    },
                }
            )
            continue
        tik_tvd = str(observation["entity_id"])
        _, rows = decoded_rows(body(store, observation))
        try:
            records, aggregate = transpose(rows, uiks_by_tik[tik_tvd], contest)
        except (IndexError, KeyError, StopIteration, ValueError) as error:
            build_errors.append(
                {
                    "tik_tvd": tik_tvd,
                    "contest": contest,
                    "url": observation["url"],
                    "error": f"{type(error).__name__}: {error}",
                }
            )
            continue
        for record in records:
            relation = relation_by_uik[record["uik_tvd"]]
            paired[record["uik_tvd"]].update(
                {
                    "uik_number": record["uik_number"],
                    "uik_tvd": record["uik_tvd"],
                    "tik_tvd": tik_tvd,
                    "tik_name": tik_names.get(tik_tvd, ""),
                    "region": str(relation.get("region", "")),
                    f"{contest}_accounting": record["accounting"],
                    f"{contest}_votes": record[f"{contest}_votes"],
                }
            )
        tik_protocols[tik_tvd][contest] = {
            **aggregate,
            "uik_count": len(records),
            "uik_tvds": [record["uik_tvd"] for record in records],
        }
        failures.extend(
            {"tik_tvd": tik_tvd, "report_type": observation["report_type"], **failure}
            for failure in reconcile(
                [
                    {
                        "accounting": record["accounting"],
                        "votes": record[f"{contest}_votes"],
                    }
                    for record in records
                ],
                aggregate,
            )
        )
        sources[tik_tvd][contest] = {
            "official_url": observation["requested_url"],
            "final_url": observation.get("final_url"),
            "sha256": observation["sha256"],
            "retrieved_at": observation.get("retrieved_at"),
            "provenance": observation.get("provenance"),
        }
    for tik_tvd, contest in sorted(aggregate_tiks):
        direct_records = [
            record
            for record in paired.values()
            if str(record.get("tik_tvd")) == tik_tvd
            and f"{contest}_accounting" in record
            and f"{contest}_votes" in record
        ]
        failures.extend(
            {
                "tik_tvd": tik_tvd,
                "report_type": sources[tik_tvd][contest]["report_type"],
                **failure,
            }
            for failure in reconcile(
                [
                    {
                        "accounting": record[f"{contest}_accounting"],
                        "votes": record[f"{contest}_votes"],
                    }
                    for record in direct_records
                ],
                tik_protocols[tik_tvd][contest],
            )
        )
    required = {
        f"{contest}_{field}"
        for contest in hierarchy["contests"]
        for field in ("accounting", "votes")
    }
    records = [record for _, record in sorted(paired.items())]
    incomplete = [
        {"uik_tvd": key, "missing": sorted(required - set(record))}
        for key, record in sorted(paired.items())
        if not required.issubset(record)
    ]
    missing = [
        {**relation, "cause": "absent-from-tik-result-columns"}
        for relation in relations
        if str(relation["uik_tvd"]) not in paired
    ]
    complete_tiks = {
        key
        for key, value in sources.items()
        if set(hierarchy["contests"]).issubset(value)
    }
    result = {
        "schema_version": 3,
        "election": hierarchy["election"],
        "election_vrn": hierarchy["election_vrn"],
        "contests": hierarchy["contests"],
        "records": records,
        "complete_uik_count": sum(required.issubset(record) for record in records),
        "sources": [
            {
                "tik_tvd": key,
                "tik_name": tik_names.get(key, ""),
                "region": str(
                    next(
                        (
                            r.get("region", "")
                            for r in relations
                            if str(r["tik_tvd"]) == key
                        ),
                        "",
                    )
                ),
                **value,
            }
            for key, value in sorted(sources.items())
            if key in complete_tiks
        ],
        "tik_protocols": [
            {"tik_tvd": key, **value}
            for key, value in sorted(tik_protocols.items())
            if set(hierarchy["contests"]).issubset(value)
        ],
        "reconciliation_failures": failures,
        "build_errors": build_errors,
        "duplicate_conflicts": conflicts,
        "incomplete_uiks": incomplete,
        "missing_hierarchy_uiks": missing,
        "relations": relations,
    }
    json_write(args.output, result)
    invalid = bool(
        failures
        or build_errors
        or incomplete
        or missing
        or conflicts
        or len(complete_tiks) != hierarchy["tiks"]
    )
    print(
        json.dumps(
            {
                "output": str(args.output),
                "complete_uiks": result["complete_uik_count"],
                "complete_tiks": len(complete_tiks),
                "incomplete_uiks": len(incomplete),
                "missing_hierarchy_uiks": len(missing),
                "reconciliation_failures": len(failures),
                "build_errors": len(build_errors),
                "duplicate_conflicts": len(conflicts),
            },
            indent=2,
        )
    )
    return 2 if invalid else 0


def validate(args: argparse.Namespace) -> int:
    hierarchy, protocols = load(args.hierarchy), load(args.protocols)
    recovery_observations: list[dict[str, Any]] = []
    for path in args.recovery_report or []:
        recovery_observations.extend(load(path).get("observations", []))
    required = {
        f"{contest}_{field}"
        for contest in hierarchy["contests"]
        for field in ("accounting", "votes")
    }
    regions: defaultdict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "tiks": set(),
            "uiks": set(),
            **{contest: set() for contest in hierarchy["contests"]},
        }
    )
    for relation in hierarchy["uik_to_tik"]:
        row = regions[str(relation.get("region", ""))]
        row["tiks"].add(str(relation["tik_tvd"]))
        row["uiks"].add(str(relation["uik_tvd"]))
    for record in protocols["records"]:
        row = regions[str(record.get("region", ""))]
        for contest in hierarchy["contests"]:
            if {f"{contest}_accounting", f"{contest}_votes"}.issubset(record):
                row[contest].add(str(record["uik_tvd"]))
    output = []
    for region, row in sorted(regions.items()):
        output.append(
            {
                "region": region,
                "discovered_tik_count": len(row["tiks"]),
                "discovered_uik_count": len(row["uiks"]),
                **{
                    f"uiks_with_{contest}_results": len(row[contest])
                    for contest in hierarchy["contests"]
                },
                "uiks_with_known_tik": len(row["uiks"]),
                "missing_report_types": [
                    contest
                    for contest in hierarchy["contests"]
                    if row[contest] != row["uiks"]
                ],
                "reconciliation_status": "failed"
                if any(
                    str(item["tik_tvd"]) in row["tiks"]
                    for item in protocols["reconciliation_failures"]
                )
                else "passed",
            }
        )
    result = {
        "schema_version": 1,
        "election": hierarchy["election"],
        "regions": output,
        "summary": {
            "regions": len(output),
            "tiks": hierarchy["tiks"],
            "uiks": hierarchy["uiks"],
            "complete_uiks": sum(
                required.issubset(record) for record in protocols["records"]
            ),
            "missing_hierarchy_uiks": len(protocols["missing_hierarchy_uiks"]),
            "incomplete_uiks": len(protocols["incomplete_uiks"]),
            "reconciliation_failures": len(protocols["reconciliation_failures"]),
            "build_errors": len(protocols["build_errors"]),
            "duplicate_conflicts": len(protocols.get("duplicate_conflicts", [])),
            "direct_recovery_requests": len(recovery_observations),
            "direct_recovery_valid": sum(
                bool(item.get("valid_result")) for item in recovery_observations
            ),
        },
        "failed_direct_recovery": [
            {
                key: item.get(key)
                for key in (
                    "entity_id",
                    "tik_tvd",
                    "region",
                    "contest",
                    "report_type",
                    "requested_url",
                    "status",
                    "sha256",
                    "validation_error",
                )
            }
            for item in recovery_observations
            if not item.get("valid_result")
        ],
    }
    json_write(args.output, result)
    valid = (
        not any(
            row["missing_report_types"] or row["reconciliation_status"] == "failed"
            for row in output
        )
        and not result["summary"]["build_errors"]
    )
    print(
        json.dumps(
            {"coverage": str(args.output), **result["summary"], "valid": valid},
            indent=2,
        )
    )
    return 0 if valid else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Restartable nationwide historical State Duma crawler"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    common.add_argument("--year", type=int, required=True)
    common.add_argument("--raw-dir", type=Path, required=True)
    common.add_argument("--rate", type=float, default=10.0)
    common.add_argument("--concurrency", type=int, default=6)
    common.add_argument("--timeout", type=float, default=20.0)
    common.add_argument("--retries", type=int, default=4)
    common.add_argument("--backoff-initial", type=float, default=0.5)
    common.add_argument("--backoff-max", type=float, default=20.0)
    common.add_argument("--jitter", type=float, default=0.1)
    common.add_argument(
        "--coordination-dir", type=Path, default=DEFAULT_COORDINATION_DIR
    )
    common.add_argument("--refresh", action="store_true")
    sub = result.add_subparsers(dest="command", required=True)
    command = sub.add_parser("discover", parents=[common])
    command.set_defaults(func=discover)
    command.add_argument("--output", type=Path, required=True)
    command = sub.add_parser("plan", parents=[common])
    command.set_defaults(func=plan)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--output", type=Path, required=True)
    command = sub.add_parser("crawl", parents=[common])
    command.set_defaults(func=crawl)
    command.add_argument("--plan", type=Path, required=True)
    command.add_argument("--report", type=Path, required=True)
    command.add_argument("--only-failures", action="store_true")
    command.add_argument("--progress-every", type=int, default=100)
    command = sub.add_parser("build", parents=[common])
    command.set_defaults(func=build)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--crawl-report", type=Path, required=True)
    command.add_argument("--additional-crawl-report", type=Path, action="append")
    command.add_argument("--output", type=Path, required=True)
    command = sub.add_parser("recover-gaps", parents=[common])
    command.set_defaults(func=recover_gaps)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--protocols", type=Path, required=True)
    command.add_argument("--report", type=Path, required=True)
    command.add_argument("--only-failures", action="store_true")
    command = sub.add_parser("validate", parents=[common])
    command.set_defaults(func=validate)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--protocols", type=Path, required=True)
    command.add_argument("--recovery-report", type=Path, action="append")
    command.add_argument("--output", type=Path, required=True)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.rate <= 0 or args.concurrency < 1 or args.retries < 0:
        raise SystemExit("rate/concurrency must be positive and retries non-negative")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
