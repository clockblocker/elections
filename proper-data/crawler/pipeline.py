from __future__ import annotations

import html
import re
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import asdict
from typing import Any

try:
    from .common import TreeNode, decode_text, extract_tree_nodes
    from .decode_script_result import decode_script_tables
except ImportError:
    from common import TreeNode, decode_text, extract_tree_nodes
    from decode_script_result import decode_script_tables


DUMA_VRN = "100100225883172"
REPORT_KIND = {
    233: ("tic", "party"),
    242: ("uik", "party"),
    464: ("tic", "candidate"),
    463: ("uik", "candidate"),
}
UIK_RE = re.compile(r"^УИК\s*№?\s*(\d+)$", re.IGNORECASE)
LINK_RE = re.compile(r"href=[\"']([^\"']+)[\"']", re.IGNORECASE)


def merge_hierarchy(payloads: Iterable[tuple[bytes, str]]) -> list[dict[str, Any]]:
    merged: dict[str, TreeNode] = {}
    for payload, source_url in payloads:
        nodes, _ = extract_tree_nodes(payload, source_url)
        for node in nodes:
            if node.node_id:
                previous = merged.get(node.node_id)
                if (
                    previous
                    and previous.parent_id
                    and node.parent_id
                    and previous.parent_id != node.parent_id
                ):
                    raise ValueError(f"conflicting parents for {node.node_id}")
                merged[node.node_id] = node
    return [asdict(merged[key]) for key in sorted(merged)]


def hierarchy_summary(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    by_id = {str(node["node_id"]): node for node in nodes if node.get("node_id")}
    children: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for node in nodes:
        if node.get("parent_id"):
            children[str(node["parent_id"])].append(node)
    uiks = [
        node
        for node in nodes
        if node.get("is_uik") or UIK_RE.match(str(node.get("text", "")))
    ]
    tik_ids = sorted({str(node["parent_id"]) for node in uiks if node.get("parent_id")})
    roots = [node for node in nodes if node.get("parent_id") is None]
    region_ids = {
        str(node["node_id"])
        for node in nodes
        if any(str(root.get("node_id")) == str(node.get("parent_id")) for root in roots)
    }
    unresolved = [
        str(node["node_id"])
        for node in nodes
        if node.get("load_on_demand") and not children[str(node["node_id"])]
    ]
    return {
        "regions": len(region_ids),
        "tiks": len(tik_ids),
        "uiks": len(uiks),
        "unresolved_load_nodes": unresolved,
        "complete": not unresolved,
        "uik_to_tik": [
            {
                "uik_number": int(UIK_RE.match(str(node["text"])).group(1)),
                "uik_tvd": str(node["node_id"]),
                "tik_tvd": str(node["parent_id"]),
                "tik_name": str(by_id.get(str(node["parent_id"]), {}).get("text", "")),
                "region": str(node.get("region") or ""),
            }
            for node in sorted(
                uiks,
                key=lambda item: (str(item.get("region")), str(item.get("node_id"))),
            )
            if UIK_RE.match(str(node.get("text", "")))
        ],
    }


def tree_endpoint(node: dict[str, Any], host: str = "old.izbirkom.ru") -> str:
    query = urllib.parse.urlencode(
        {
            "action": "tvdTree",
            "tvdchildren": "true",
            "vrn": node["vrn"],
            "tvd": node["tvd"],
        }
    )
    return f"http://{host}/region/izbirkom?{query}"


def _region_ancestor(
    node: dict[str, Any], by_id: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    current = node
    while current.get("parent_id") and by_id.get(str(current["parent_id"]), {}).get(
        "parent_id"
    ):
        current = by_id[str(current["parent_id"])]
    return current


def result_url(
    node: dict[str, Any], region_node: dict[str, Any], report_type: int
) -> str:
    if report_type not in REPORT_KIND:
        raise ValueError(f"unsupported report type {report_type}")
    original = urllib.parse.urlsplit(str(node["url"]))
    host_parts = original.netloc.split(".")
    slug = host_parts[1] if len(host_parts) > 4 and host_parts[0] == "www" else None
    path = f"/region/region/{slug}" if slug else "/region/izbirkom"
    query = dict(urllib.parse.parse_qsl(original.query))
    query.update(
        {
            "action": "show",
            "tvd": str(region_node["tvd"]),
            "vrn": DUMA_VRN,
            "region": str(node.get("region") or region_node.get("region") or ""),
            "global": "null",
            "sub_region": str(
                node.get("sub_region") or region_node.get("sub_region") or ""
            ),
            "prver": "0",
            "pronetvd": "null",
            "vibid": str(node["tvd"]),
            "type": str(report_type),
        }
    )
    query.pop("root", None)
    return urllib.parse.urlunsplit(
        ("http", "old.izbirkom.ru", path, urllib.parse.urlencode(query), "")
    )


def make_plan(
    nodes: list[dict[str, Any]],
    *,
    direct_uik: bool = False,
    region_filter: set[str] | None = None,
) -> dict[str, Any]:
    summary = hierarchy_summary(nodes)
    by_id = {str(node["node_id"]): node for node in nodes if node.get("node_id")}
    requests: list[dict[str, Any]] = []
    mappings = summary["uik_to_tik"]
    tik_ids = sorted({item["tik_tvd"] for item in mappings})
    for tik_id in tik_ids:
        tik = by_id[tik_id]
        if region_filter and str(tik.get("region")) not in region_filter:
            continue
        region = _region_ancestor(tik, by_id)
        for report_type in (233, 464):
            requests.append(
                {
                    "class": f"tic-{report_type}",
                    "report_type": report_type,
                    "entity_id": tik_id,
                    "region": str(tik.get("region") or ""),
                    "url": result_url(tik, region, report_type),
                }
            )
    if direct_uik:
        for relation in mappings:
            uik = by_id[relation["uik_tvd"]]
            if region_filter and str(uik.get("region")) not in region_filter:
                continue
            region = _region_ancestor(uik, by_id)
            for report_type in (242, 463):
                requests.append(
                    {
                        "class": f"uik-{report_type}",
                        "report_type": report_type,
                        "entity_id": relation["uik_tvd"],
                        "region": str(uik.get("region") or ""),
                        "tik_tvd": relation["tik_tvd"],
                        "url": result_url(uik, region, report_type),
                    }
                )
    counts = Counter(item["class"] for item in requests)
    return {
        "schema_version": 1,
        "election_vrn": DUMA_VRN,
        "hierarchy": {
            key: summary[key] for key in ("regions", "tiks", "uiks", "complete")
        },
        "request_classes": dict(sorted(counts.items())),
        "estimated_requests": len(requests),
        "requests": requests,
    }


def extract_report_links(payload: bytes, source_url: str) -> list[dict[str, Any]]:
    source, _ = decode_text(payload)
    result = []
    for escaped in LINK_RE.findall(source):
        url = urllib.parse.urljoin(source_url, html.unescape(escaped))
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        try:
            report_type = int(query.get("type", ""))
        except ValueError:
            continue
        if report_type in REPORT_KIND and query.get("vrn") == DUMA_VRN:
            result.append({"report_type": report_type, "url": url})
    return sorted(
        {item["url"]: item for item in result}.values(), key=lambda item: item["url"]
    )


def classify_result(
    payload: bytes, requested_type: int | None = None
) -> dict[str, Any]:
    source, encoding = decode_text(payload)
    lower = source.casefold()
    tables = decode_script_tables(source)
    rows = max(tables, key=lambda table: sum(len(row) for row in table), default=[])
    flat = " ".join(cell for row in rows for cell in row).casefold()
    has_accounting = (
        "число избирателей" in flat or "число избирательных бюллетеней" in flat
    )
    uik_headers = sorted(
        {
            int(match.group(1))
            for row in rows
            for cell in row
            if (match := UIK_RE.fullmatch(cell.strip()))
        }
    )
    party = "политическ" in flat or "единая россия" in flat
    candidate = bool(has_accounting and len(rows) > 13 and not party)
    error_signals = [
        signal
        for signal in ("access denied", "captcha", "service unavailable", "ошибка")
        if signal in lower
    ]
    header_index = next(
        (
            index
            for index, row in enumerate(rows)
            if any(UIK_RE.fullmatch(cell.strip()) for cell in row)
        ),
        None,
    )
    numeric_values: list[str] = []
    if header_index is not None:
        columns = [
            index
            for index, cell in enumerate(rows[header_index])
            if UIK_RE.fullmatch(cell.strip())
        ]
        for row in rows[header_index + 1 : header_index + 13]:
            numeric_values.extend(row[index] for index in columns if index < len(row))
    else:
        numeric_values = [row[2] for row in rows[:12] if len(row) >= 3]
    numeric_valid = bool(numeric_values) and all(
        re.match(r"^\d+(?:\s|$)", value.strip()) for value in numeric_values
    )
    # Legacy navigation contains an "error" menu label on otherwise valid pages;
    # a decoded protocol table is stronger evidence than that ambient word.
    valid = bool(rows and has_accounting and numeric_valid)
    if valid:
        error_signals = []
    level = "tic-column-report" if uik_headers else "uik-direct-protocol"
    ballot = (
        "candidate"
        if candidate and not party
        else "party"
        if party and not candidate
        else "unknown"
    )
    expected = REPORT_KIND.get(requested_type or -1)
    kind_matches = bool(
        expected
        and expected[1] == ballot
        and ((expected[0] == "tic") == bool(uik_headers))
    )
    return {
        "valid_result": valid,
        "encoding": encoding,
        "level": level,
        "ballot": ballot,
        "requested_type": requested_type,
        "kind_matches_requested_type": kind_matches,
        "row_count": len(rows),
        "column_count": max(map(len, rows), default=0),
        "numeric_cells_valid": numeric_valid,
        "uik_numbers": uik_headers,
        "error_signals": error_signals,
    }


def reconcile(
    records: list[dict[str, Any]], aggregate: dict[str, Any]
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for field in ("accounting", "votes"):
        totals: defaultdict[str, int] = defaultdict(int)
        for record in records:
            for label, value in record.get(field, {}).items():
                totals[label] += int(value)
        expected = {str(k): int(v) for k, v in aggregate.get(field, {}).items()}
        for label in sorted(set(totals) | set(expected)):
            if totals[label] != expected.get(label, 0):
                failures.append(
                    {
                        "field": field,
                        "label": label,
                        "uik_sum": totals[label],
                        "tik_value": expected.get(label, 0),
                        "difference": totals[label] - expected.get(label, 0),
                    }
                )
    return failures


def coverage_report(
    nodes: list[dict[str, Any]], observations: list[dict[str, Any]]
) -> dict[str, Any]:
    summary = hierarchy_summary(nodes)
    relations = summary["uik_to_tik"]
    by_number = {
        (item["tik_tvd"], item["uik_number"]): item["uik_tvd"] for item in relations
    }
    regions: dict[str, dict[str, Any]] = {}
    for relation in relations:
        region = relation["region"]
        row = regions.setdefault(
            region,
            {
                "region": region,
                "tik_ids": set(),
                "uik_ids": set(),
                "party_uiks": set(),
                "candidate_uiks": set(),
                "failed_urls": [],
                "provenance": Counter(),
            },
        )
        row["tik_ids"].add(relation["tik_tvd"])
        row["uik_ids"].add(relation["uik_tvd"])
    for observation in observations:
        region = str(observation.get("region", "unknown"))
        row = regions.setdefault(
            region,
            {
                "region": region,
                "tik_ids": set(),
                "uik_ids": set(),
                "party_uiks": set(),
                "candidate_uiks": set(),
                "failed_urls": [],
                "provenance": Counter(),
            },
        )
        row["provenance"][observation.get("provenance", "unknown")] += 1
        covered = set(map(str, observation.get("uik_tvds", [])))
        if not covered and observation.get("entity_id"):
            if observation.get("level") == "uik-direct-protocol":
                covered.add(str(observation["entity_id"]))
            else:
                covered.update(
                    by_number[(str(observation["entity_id"]), int(number))]
                    for number in observation.get("uik_numbers", [])
                    if (str(observation["entity_id"]), int(number)) in by_number
                )
        if observation.get("ballot") == "party":
            row["party_uiks"].update(covered)
        if observation.get("ballot") == "candidate":
            row["candidate_uiks"].update(covered)
        if observation.get("error") or not observation.get("valid_result", False):
            row["failed_urls"].append(
                {
                    "url": observation.get("requested_url"),
                    "error_class": observation.get("error_class", "validation"),
                }
            )
    output = []
    for key in sorted(regions):
        row = regions[key]
        output.append(
            {
                "region": key,
                "discovered_tik_count": len(row["tik_ids"]),
                "discovered_uik_count": len(row["uik_ids"]),
                "uiks_with_party_results": len(row["party_uiks"]),
                "uiks_with_candidate_results": len(row["candidate_uiks"]),
                "uiks_with_known_tik": len(row["uik_ids"]),
                "missing_report_types": [
                    kind
                    for kind, values in (
                        ("party", row["party_uiks"]),
                        ("candidate", row["candidate_uiks"]),
                    )
                    if values != row["uik_ids"]
                ],
                "failed_urls": row["failed_urls"],
                "provenance": dict(row["provenance"]),
                "reconciliation_status": "not-run",
            }
        )
    return {"schema_version": 1, "election": "2021-duma", "regions": output}
