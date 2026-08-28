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
    from .registries import (
        normalized_choice_name,
        normalized_full_name,
        parse_candidate_registry,
        parse_legacy_party_detail,
        parse_party_registry,
        registry_source,
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
    from registries import (
        normalized_choice_name,
        normalized_full_name,
        parse_candidate_registry,
        parse_legacy_party_detail,
        parse_party_registry,
        registry_source,
    )
    from shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from transport import FetchConfig, Fetcher, ResponseStore


HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE / "historical-nationwide.json"
UIK_RE = re.compile(r"^(?:УИК|Участок)\s*№?\s*(\d+)$", re.IGNORECASE)
DATE_RE = re.compile(
    r"(?<!\d)(?P<day>\d{1,2})[./](?P<month>\d{1,2})[./]"
    r"(?P<year>\d{2}|\d{4})(?!\d)"
)
RESULT_ORDINAL_RE = re.compile(r"^(?:\s*\d+\s*\.\s*)+")
DOB_SUFFIX_RE = re.compile(
    r"\s*(?:\(\s*)?(?P<day>\d{1,2})[./](?P<month>\d{1,2})[./]"
    r"(?P<year>\d{2}|\d{4})\s*\)?\s*$"
)
OIK_BREADCRUMB_RE = re.compile(
    r'href=["\'](?P<href>[^"\']+)["\'][^>]*>\s*'
    r'ОИК\s*№?\s*(?P<number>\d+)\s*</a>',
    re.IGNORECASE,
)


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


def has_single_member_districts(config: dict[str, Any]) -> bool:
    """Return whether the configured election has an official OIK tier."""
    return str(config.get("election", "")).endswith("-duma") and "candidate" in config.get(
        "contests", {}
    )


def enriched_relations(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Rebuild relation identities from ancestors instead of stale saved reductions."""
    return hierarchy_summary(
        config["nodes"], include_oik=has_single_member_districts(config)
    )["uik_to_tik"]


def oik_breadcrumbs(payload: bytes) -> list[tuple[str, int]]:
    """Extract exact official ``OIK №…`` breadcrumb targets from a GAS page."""
    text, _ = decode_text(payload)
    result: set[tuple[str, int]] = set()
    for match in OIK_BREADCRUMB_RE.finditer(text):
        href = match.group("href").replace("&amp;", "&")
        tvd = urllib.parse.parse_qs(urllib.parse.urlsplit(href).query).get("tvd")
        if tvd and tvd[0]:
            result.add((str(tvd[0]), int(match.group("number"))))
    return sorted(result)


def oik_breadcrumb_links(payload: bytes, source_url: str) -> list[dict[str, Any]]:
    """Return exact official OIK breadcrumb URLs with their stated numbers."""
    text, _ = decode_text(payload)
    result: dict[tuple[str, int, str], dict[str, Any]] = {}
    for match in OIK_BREADCRUMB_RE.finditer(text):
        url = urllib.parse.urljoin(
            source_url, match.group("href").replace("&amp;", "&")
        )
        tvd = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("tvd")
        if not tvd or not tvd[0]:
            continue
        key = (str(tvd[0]), int(match.group("number")), url)
        result[key] = {
            "oik_tvd": key[0],
            "district_number": key[1],
            "url": key[2],
        }
    return [result[key] for key in sorted(result)]


def registry_specs(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    value = config.get("registry_reports", {})
    if not isinstance(value, dict):
        raise TypeError("registry_reports must be an object")
    return {str(key): dict(spec) for key, spec in value.items()}


def observation_source(observation: dict[str, Any]) -> dict[str, Any]:
    """Adapt a crawl observation to the shared immutable source contract."""
    requested_url = observation.get("requested_url") or observation.get("url")
    return registry_source({**observation, "requested_url": requested_url})


def report_url_type_matches(url: str, expected_report_type: int) -> bool:
    """Require the retained final official URL to identify the planned report type."""
    values = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("type", [])
    return values == [str(expected_report_type)]


def candidate_is_registered(candidate: dict[str, Any]) -> bool:
    if candidate.get("is_elected"):
        return True
    status = str(candidate.get("registration_status") or "").casefold()
    excluded = ("отказ", "выбыв", "утрат", "отмен")
    on_ballot_status = "зарегистр" in status or "депутат" in status
    return on_ballot_status and not any(word in status for word in excluded)


def aggregate_protocol(payload: bytes, commission_name: str = "") -> dict[str, Any]:
    """Parse one aggregate GAS result report across legacy table variants."""
    try:
        parsed = extract_direct_protocol(payload, commission_name=commission_name)
        return {"accounting": parsed["accounting"], "votes": parsed["votes"]}
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
        return {
            "accounting": {row[1]: number(row[2]) for row in accounting},
            "votes": {row[1]: number(row[2]) for row in votes},
        }


def district_numbers_from_observations(
    config: dict[str, Any],
    relations: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    store: ResponseStore,
) -> dict[str, int]:
    """Validate the official OIK-number bijection preserved in TIK result pages."""
    if not has_single_member_districts(config):
        return {}
    oik_by_tik: dict[str, str] = {}
    expected_oiks: set[str] = set()
    for relation in relations:
        tik_tvd = str(relation["tik_tvd"])
        oik_tvd = str(relation.get("oik_tvd") or "")
        if not oik_tvd:
            raise ValueError(f"single-member TIK {tik_tvd} has no OIK ancestor")
        previous = oik_by_tik.setdefault(tik_tvd, oik_tvd)
        if previous != oik_tvd:
            raise ValueError(f"TIK {tik_tvd} has conflicting OIK ancestors")
        expected_oiks.add(oik_tvd)

    evidence: defaultdict[str, set[int]] = defaultdict(set)
    missing_breadcrumbs: set[str] = set()
    for observation in observations:
        if (
            not str(observation.get("class", "")).startswith("tic-")
            or not observation.get("valid_result")
            or not observation.get("body_path")
        ):
            continue
        tik_tvd = str(observation.get("entity_id") or "")
        expected_oik = oik_by_tik.get(tik_tvd)
        if not expected_oik:
            continue
        matches = oik_breadcrumbs(body(store, observation))
        exact = {number for oik_tvd, number in matches if oik_tvd == expected_oik}
        if len(matches) != 1 or not exact:
            missing_breadcrumbs.add(tik_tvd)
        evidence[expected_oik].update(exact)

    conflicts = {
        oik_tvd: sorted(numbers)
        for oik_tvd, numbers in evidence.items()
        if len(numbers) != 1
    }
    absent = sorted(expected_oiks - set(evidence))
    resolved = {
        oik_tvd: next(iter(evidence[oik_tvd]))
        for oik_tvd in expected_oiks
        if len(evidence.get(oik_tvd, set())) == 1
    }
    numbers = list(resolved.values())
    duplicate_numbers = sorted(
        number for number, count in Counter(numbers).items() if count != 1
    )
    if (
        len(expected_oiks) != 225
        or absent
        or conflicts
        or duplicate_numbers
        or missing_breadcrumbs
        or set(numbers) != set(range(1, 226))
    ):
        raise ValueError(
            "district hierarchy validation failed: "
            f"oiks={len(expected_oiks)}, absent={len(absent)}, "
            f"conflicts={len(conflicts)}, duplicate_numbers={duplicate_numbers}, "
            f"numbers={len(set(numbers))}, missing_breadcrumb_tiks="
            f"{len(missing_breadcrumbs)}"
        )
    return resolved


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
    summary = hierarchy_summary(
        output_nodes, include_oik=has_single_member_districts(config)
    )
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

    specs = registry_specs(config)
    registry_links: dict[str, list[dict[str, Any]]] = {
        "election": [],
        "districts": [],
        "party_details": [],
    }
    registry_link_errors: list[dict[str, Any]] = []
    cec_nodes = [node for node in output_nodes if node.get("parent_id") is None]
    if len(cec_nodes) != 1:
        registry_link_errors.append(
            {
                "scope": "election",
                "error": "expected-exactly-one-cec-node",
                "node_ids": [str(node.get("node_id")) for node in cec_nodes],
            }
        )
    else:
        cec_node = cec_nodes[0]
        cec_record = fetcher.fetch(str(cec_node["url"]), refresh=args.refresh)
        cec_links: list[dict[str, Any]] = []
        election_specs = {
            name: spec
            for name, spec in specs.items()
            if str(spec.get("scope")) == "election"
            or name in ("candidate", "party")
        }
        if int(cec_record.get("status", 0)) == 200 and cec_record.get("body_path"):
            desired_types = {
                int(spec["report_type"]): ("cec", name)
                for name, spec in election_specs.items()
            }
            cec_links = extract_report_links(
                body(store, cec_record),
                str(cec_record.get("final_url", cec_node["url"])),
                election_vrn=config["election_vrn"],
                report_kind=desired_types,
            )
        for name, spec in sorted(election_specs.items()):
            report_type = int(spec["report_type"])
            exact = sorted(
                {
                    str(item["url"])
                    for item in cec_links
                    if int(item["report_type"]) == report_type
                }
            )
            if len(exact) == 1:
                url = exact[0]
                url_source = "official-navigation"
            elif spec.get("official_url"):
                url = str(spec["official_url"])
                url_source = "official-configured"
            else:
                registry_link_errors.append(
                    {
                        "scope": "election",
                        "kind": name,
                        "report_type": report_type,
                        "urls": exact,
                        "navigation_status": cec_record.get("status"),
                        "error": "expected-one-exact-cec-report-link",
                    }
                )
                continue
            registry_links["election"].append(
                {
                    "kind": name,
                    "scope": str(spec.get("scope") or "election"),
                    "report_type": report_type,
                    "entity_id": str(cec_node["node_id"]),
                    "url": url,
                    "url_source": url_source,
                    **{
                        key: spec[key]
                        for key in (
                            "expected_count",
                            "expected_unique_count",
                            "expected_registered_count",
                            "expected_literal_elected_count",
                            "expected_mutable_status_districts",
                            "expected_partial_tik_candidate_maps",
                            "expected_district_count",
                            "expected_ballot_count",
                        )
                        if spec.get(key) is not None
                    },
                }
            )

        party_spec = specs.get("party")
        party_link = next(
            (
                item
                for item in registry_links["election"]
                if item["kind"] == "party"
            ),
            None,
        )
        if party_spec and party_spec.get("detail_report_type") and party_link:
            record = fetcher.fetch(str(party_link["url"]), refresh=args.refresh)
            if int(record.get("status", 0)) == 200 and record.get("body_path"):
                parsed = parse_party_registry(
                    body(store, record),
                    str(record.get("final_url", party_link["url"])),
                    election_vrn=config["election_vrn"],
                )
                for party in parsed["parties"]:
                    if party.get("detail_url"):
                        registry_links["party_details"].append(
                            {
                                "kind": "party-detail",
                                "scope": "election",
                                "report_type": int(
                                    party_spec["detail_report_type"]
                                ),
                                "entity_id": str(party["party_list_vrnio"]),
                                "url": str(party["detail_url"]),
                                "url_source": "official-registry-link",
                            }
                        )
                expected_details = int(party_spec.get("expected_detail_count", 0))
                if expected_details and len(registry_links["party_details"]) != expected_details:
                    registry_link_errors.append(
                        {
                            "scope": "election",
                            "kind": "party-detail",
                            "expected": expected_details,
                            "actual": len(registry_links["party_details"]),
                            "error": "incomplete-party-detail-links",
                        }
                    )
            else:
                registry_link_errors.append(
                    {
                        "scope": "election",
                        "kind": "party-detail",
                        "status": record.get("status"),
                        "error": "party-registry-unavailable-during-discovery",
                    }
                )

    winner_spec = specs.get("winner")
    if winner_spec and str(winner_spec.get("scope")) == "district":
        relations_by_oik: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        for relation in summary["uik_to_tik"]:
            if relation.get("oik_tvd"):
                relations_by_oik[str(relation["oik_tvd"])].append(relation)

        def resolve_district_registry(
            oik_tvd: str, district_relations: list[dict[str, Any]]
        ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
            relation = min(
                district_relations,
                key=lambda item: (str(item["tik_tvd"]), str(item["uik_tvd"])),
            )
            tik_tvd = str(relation["tik_tvd"])
            tik_node = by_id[tik_tvd]
            navigation = fetcher.fetch(str(tik_node["url"]), refresh=args.refresh)
            if int(navigation.get("status", 0)) != 200 or not navigation.get(
                "body_path"
            ):
                return None, {
                    "oik_tvd": oik_tvd,
                    "tik_tvd": tik_tvd,
                    "status": navigation.get("status"),
                    "error": "representative-tik-navigation-unavailable",
                }
            breadcrumbs = oik_breadcrumb_links(
                body(store, navigation),
                str(navigation.get("final_url", tik_node["url"])),
            )
            exact = [item for item in breadcrumbs if item["oik_tvd"] == oik_tvd]
            if len(breadcrumbs) != 1 or len(exact) != 1:
                return None, {
                    "oik_tvd": oik_tvd,
                    "tik_tvd": tik_tvd,
                    "breadcrumbs": breadcrumbs,
                    "error": "expected-one-exact-oik-breadcrumb",
                }
            breadcrumb = exact[0]
            oik_record = fetcher.fetch(str(breadcrumb["url"]), refresh=args.refresh)
            if int(oik_record.get("status", 0)) != 200 or not oik_record.get(
                "body_path"
            ):
                return None, {
                    "oik_tvd": oik_tvd,
                    "district_number": breadcrumb["district_number"],
                    "status": oik_record.get("status"),
                    "error": "oik-navigation-unavailable",
                }
            report_type = int(winner_spec["report_type"])
            report_links = extract_report_links(
                body(store, oik_record),
                str(oik_record.get("final_url", breadcrumb["url"])),
                election_vrn=config["election_vrn"],
                report_kind={report_type: ("oik", "winner")},
            )
            urls = sorted({str(item["url"]) for item in report_links})
            if len(urls) != 1:
                return None, {
                    "oik_tvd": oik_tvd,
                    "district_number": breadcrumb["district_number"],
                    "urls": urls,
                    "error": "expected-one-exact-oik-winner-result-link",
                }
            return (
                {
                    "kind": "winner",
                    "scope": "district",
                    "report_type": report_type,
                    "entity_id": oik_tvd,
                    "oik_tvd": oik_tvd,
                    "oik_name": str(relation.get("oik_name") or ""),
                    "district_number": int(breadcrumb["district_number"]),
                    "region_code": str(relation.get("region_code") or ""),
                    "region_tvd": str(relation.get("region_tvd") or ""),
                    "region_name": str(relation.get("region_name") or ""),
                    "oik_url": str(breadcrumb["url"]),
                    "url": urls[0],
                    "url_source": "official-oik-navigation",
                },
                None,
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.concurrency
        ) as pool:
            futures = [
                pool.submit(resolve_district_registry, oik_tvd, values)
                for oik_tvd, values in sorted(relations_by_oik.items())
            ]
            for index, future in enumerate(
                concurrent.futures.as_completed(futures), 1
            ):
                item, error = future.result()
                if item:
                    registry_links["districts"].append(item)
                if error:
                    registry_link_errors.append(error)
                if index % 25 == 0 or index == len(futures):
                    print(
                        json.dumps(
                            {
                                "year": args.year,
                                "oik_registry_link_requests": index,
                                "total": len(futures),
                                "resolved": len(registry_links["districts"]),
                            }
                        ),
                        flush=True,
                    )

    registry_links["election"].sort(
        key=lambda item: (str(item["kind"]), int(item["report_type"]))
    )
    registry_links["districts"].sort(
        key=lambda item: (int(item["district_number"]), str(item["oik_tvd"]))
    )
    registry_links["party_details"].sort(key=lambda item: str(item["entity_id"]))
    expected_election_kinds = {
        name
        for name, spec in specs.items()
        if str(spec.get("scope")) == "election" or name in ("candidate", "party")
    }
    found_election_kinds = {item["kind"] for item in registry_links["election"]}
    district_count_expected = int(
        specs.get("winner", {}).get("expected_count", 0)
    )
    registry_links_complete = bool(
        not registry_link_errors
        and expected_election_kinds == found_election_kinds
        and (
            not district_count_expected
            or len(registry_links["districts"]) == district_count_expected
        )
    )

    required = set(map(str, tic_types))
    result = {
        "schema_version": 2,
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
        "registry_reports": specs,
        "registry_links": registry_links,
        "registry_link_errors": registry_link_errors,
        "registry_links_complete": registry_links_complete,
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
                    "registry_links_complete",
                )
            },
            indent=2,
        )
    )
    return (
        0
        if result["complete"]
        and result["report_links_complete"]
        and result["registry_links_complete"]
        else 2
    )


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
    for item in hierarchy.get("registry_links", {}).get("election", []):
        kind = str(item["kind"])
        if kind == "winner":
            request_class = f"election-winner-result-{item['report_type']}"
        else:
            request_class = f"election-{kind}-registry-{item['report_type']}"
        requests.append({"class": request_class, **item})
    for item in hierarchy.get("registry_links", {}).get("districts", []):
        requests.append(
            {
                "class": f"district-winner-result-{item['report_type']}",
                **item,
            }
        )
    for item in hierarchy.get("registry_links", {}).get("party_details", []):
        requests.append(
            {"class": f"party-detail-{item['report_type']}", **item}
        )
    ready = bool(
        hierarchy.get("complete")
        and hierarchy.get("report_links_complete")
        and hierarchy.get("registry_links_complete")
        and all(item["url"] for item in requests)
    )
    result = {
        "schema_version": 2,
        "election": hierarchy["election"],
        "election_vrn": hierarchy["election_vrn"],
        "contests": hierarchy["contests"],
        "registry_reports": hierarchy.get("registry_reports", {}),
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
        "registry_links_complete": hierarchy.get("registry_links_complete", False),
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


def finalize_result_classification(
    classification: dict[str, Any],
    *,
    request_class: str,
    report_type: int,
    contest_types: dict[str, Any],
    exact_report_type: bool,
) -> dict[str, Any]:
    """Distinguish an official same-type TIK aggregate from a direct UIK page."""
    result = dict(classification)
    shared_direct_type = bool(
        request_class.startswith("tic-")
        and int(contest_types["tic"]) == report_type
        and int(contest_types["uik"]) == report_type
        and result.get("valid_result")
        and result.get("level") == "uik-direct-protocol"
        and exact_report_type
    )
    if shared_direct_type:
        result["level"] = "tik-direct-protocol"
    result["kind_matches_requested_type"] = bool(
        result.get("valid_result")
        and exact_report_type
        and result.get("level") in ("tic-column-report", "tik-direct-protocol")
    )
    return result


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
        request_class = str(item.get("class") or "")
        exact_report_type = report_url_type_matches(
            str(record.get("final_url") or item["url"]), int(item["report_type"])
        )
        if payload and "candidate-registry" in request_class:
            parsed = parse_candidate_registry(
                payload,
                str(record.get("final_url", item["url"])),
                election_vrn=plan_data["election_vrn"],
                scope=str(item.get("scope") or "election"),
            )
            registered_count = sum(
                candidate_is_registered(candidate)
                for candidate in parsed["candidates"]
            )
            literal_elected_count = sum(
                bool(candidate.get("is_elected")) for candidate in parsed["candidates"]
            )
            valid = bool(
                parsed["valid_candidate_registry"]
                and parsed["election_vrn_matches"]
                and (
                    item.get("expected_count") is None
                    or parsed["candidate_count"] == int(item["expected_count"])
                )
                and (
                    item.get("expected_unique_count") is None
                    or parsed["unique_candidate_vibid_count"]
                    == int(item["expected_unique_count"])
                )
                and (
                    item.get("expected_registered_count") is None
                    or registered_count == int(item["expected_registered_count"])
                )
                and (
                    item.get("expected_literal_elected_count") is None
                    or literal_elected_count
                    == int(item["expected_literal_elected_count"])
                )
                and (
                    item.get("expected_district_count") is None
                    or len(parsed["district_numbers"])
                    == int(item["expected_district_count"])
                )
            )
            return {
                **item,
                **record,
                **parsed,
                "registered_count": registered_count,
                "literal_elected_count": literal_elected_count,
                "valid_result": valid,
                "kind_matches_requested_type": exact_report_type,
                "level": f"{item.get('scope', 'election')}-candidate-registry",
                "ballot": "candidate-registry",
            }
        if payload and "party-registry" in request_class:
            parsed = parse_party_registry(
                payload,
                str(record.get("final_url", item["url"])),
                election_vrn=plan_data["election_vrn"],
            )
            valid = bool(
                parsed["valid_party_registry"]
                and parsed["election_vrn_matches"]
                and (
                    item.get("expected_count") is None
                    or parsed["party_count"] == int(item["expected_count"])
                )
            )
            return {
                **item,
                **record,
                **parsed,
                "valid_result": valid,
                "kind_matches_requested_type": exact_report_type,
                "level": "election-party-registry",
                "ballot": "party-registry",
            }
        if payload and request_class.startswith("party-detail-"):
            parsed = parse_legacy_party_detail(
                payload,
                str(record.get("final_url", item["url"])),
                election_vrn=plan_data["election_vrn"],
            )
            return {
                **item,
                **record,
                **parsed,
                "valid_result": parsed["valid_party_detail"],
                "kind_matches_requested_type": exact_report_type,
                "level": "election-party-detail",
                "ballot": "party-registry",
            }
        if payload and "winner-result" in request_class:
            try:
                protocol = aggregate_protocol(payload)
                valid = bool(protocol["votes"] and plan_data["election_vrn"] in decode_text(payload)[0])
                error = None
            except ValueError as caught:
                protocol = {"accounting": {}, "votes": {}}
                valid = False
                error = f"{type(caught).__name__}: {caught}"
            return {
                **item,
                **record,
                "valid_result": valid,
                "kind_matches_requested_type": exact_report_type,
                "level": f"{item.get('scope', 'election')}-winner-result",
                "ballot": "candidate",
                "winner_vote_labels": sorted(protocol["votes"]),
                "validation_error": error,
            }
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
        classification = finalize_result_classification(
            classification,
            request_class=request_class,
            report_type=int(item["report_type"]),
            contest_types=plan_data["contests"][item["contest"]],
            exact_report_type=exact_report_type,
        )
        classification["ballot"] = item["contest"]
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


def source_is_complete(source: dict[str, Any] | None) -> bool:
    if not source:
        return False
    digest = str(source.get("sha256") or "")
    return bool(
        source.get("official_url")
        and re.fullmatch(r"[0-9a-f]{64}", digest)
        and source.get("report_type") is not None
        and source.get("retrieved_at")
        and source.get("final_url")
        and source.get("provenance")
    )


def candidate_label_matches(label: str, candidate: dict[str, Any]) -> bool:
    """Strictly match a result label by full name, with an exact DOB disambiguator."""
    label_without_ordinal = RESULT_ORDINAL_RE.sub("", label)
    label_key = normalized_full_name(label_without_ordinal)
    name_key = normalized_full_name(str(candidate["full_name"]))
    if label_key == name_key:
        return True
    birth_date = str(candidate.get("birth_date") or "").strip()
    registry_date = DATE_RE.search(birth_date)
    label_date = DOB_SUFFIX_RE.search(label_without_ordinal)
    if not registry_date or not label_date:
        return False
    label_name = label_without_ordinal[: label_date.start()]
    if normalized_full_name(label_name) != name_key:
        return False

    def date_key(match: re.Match[str]) -> tuple[int, int, int]:
        return (
            int(match.group("day")),
            int(match.group("month")),
            int(match.group("year")) % 100,
        )

    return date_key(label_date) == date_key(registry_date)


def build_registry_products(
    hierarchy: dict[str, Any],
    observations: list[dict[str, Any]],
    store: ResponseStore,
    relations: list[dict[str, Any]],
    records: list[dict[str, Any]],
    tik_protocols: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    specs = registry_specs(hierarchy)
    source_errors: list[dict[str, Any]] = []
    registry_errors: list[dict[str, Any]] = []
    identity_errors: list[dict[str, Any]] = []
    winner_errors: list[dict[str, Any]] = []
    allowed_anomalies: list[dict[str, Any]] = []

    candidate_rows: list[dict[str, Any]] = []
    candidate_source: dict[str, Any] | None = None
    candidate_observations = [
        item
        for item in observations
        if "candidate-registry" in str(item.get("class") or "")
    ]
    candidate_spec = specs.get("candidate")
    if candidate_spec:
        if len(candidate_observations) != 1:
            source_errors.append(
                {
                    "kind": "candidate-registry",
                    "expected": 1,
                    "actual": len(candidate_observations),
                    "error": "expected-one-candidate-registry-source",
                }
            )
        elif candidate_observations[0].get("body_path"):
            observation = candidate_observations[0]
            if not observation.get("valid_result") or not observation.get(
                "kind_matches_requested_type"
            ):
                source_errors.append(
                    {
                        "kind": "candidate-registry",
                        "error": "candidate-registry-observation-not-validated",
                    }
                )
            parsed = parse_candidate_registry(
                body(store, observation),
                str(observation.get("final_url") or observation.get("url") or ""),
                election_vrn=hierarchy["election_vrn"],
                scope=str(candidate_spec.get("scope") or "election"),
            )
            candidate_source = observation_source(observation)
            raw_candidate_rows = [
                {
                    **candidate,
                    "candidate_key": (
                        f"gas:candidate-vibid:{candidate['candidate_vibid']}"
                    ),
                }
                for candidate in parsed["candidates"]
            ]
            registered_count = sum(map(candidate_is_registered, raw_candidate_rows))
            checks = {
                "candidate_count": parsed["candidate_count"],
                "unique_candidate_count": parsed["unique_candidate_vibid_count"],
                "registered_count": registered_count,
                "literal_elected_count": sum(
                    bool(item.get("is_elected")) for item in raw_candidate_rows
                ),
                "district_count": len(parsed["district_numbers"]),
            }
            expected = {
                "candidate_count": candidate_spec.get("expected_count"),
                "unique_candidate_count": candidate_spec.get(
                    "expected_unique_count"
                ),
                "registered_count": candidate_spec.get(
                    "expected_registered_count"
                ),
                "literal_elected_count": candidate_spec.get(
                    "expected_literal_elected_count"
                ),
                "district_count": candidate_spec.get("expected_district_count"),
            }
            for field, expected_value in expected.items():
                if expected_value is not None and checks[field] != int(expected_value):
                    registry_errors.append(
                        {
                            "kind": "candidate-registry",
                            "field": field,
                            "expected": int(expected_value),
                            "actual": checks[field],
                            "error": "candidate-registry-count-mismatch",
                        }
                    )
            duplicate_vibids = parsed.get("duplicate_candidate_vibids", [])
            if duplicate_vibids:
                if (
                    int(candidate_spec.get("expected_count", 0))
                    - int(candidate_spec.get("expected_unique_count", 0))
                    == len(raw_candidate_rows)
                    - len({item["candidate_vibid"] for item in raw_candidate_rows})
                ):
                    allowed_anomalies.append(
                        {
                            "allowed": True,
                            "kind": "candidate-registry-history-duplicates",
                            "candidate_vibids": duplicate_vibids,
                            "row_count": len(raw_candidate_rows),
                            "unique_candidate_vibid_count": len(
                                {
                                    item["candidate_vibid"]
                                    for item in raw_candidate_rows
                                }
                            ),
                        }
                    )
                else:
                    registry_errors.append(
                        {
                            "kind": "candidate-registry",
                            "candidate_vibids": duplicate_vibids,
                            "error": "unexpected-duplicate-candidate-vibids",
                        }
                    )
            history_by_vibid: defaultdict[str, list[dict[str, Any]]] = defaultdict(
                list
            )
            for candidate in raw_candidate_rows:
                history_by_vibid[str(candidate["candidate_vibid"])].append(candidate)
            for candidate_vibid, history in history_by_vibid.items():
                selected = max(
                    enumerate(history),
                    key=lambda item: (
                        bool(item[1].get("is_elected")),
                        candidate_is_registered(item[1]),
                        bool(item[1].get("registry_election_status")),
                        bool(item[1].get("registration_status")),
                        -item[0],
                    ),
                )[1]
                consolidated = dict(selected)
                if len(history) > 1:
                    consolidated["registry_history"] = [
                        {
                            key: value
                            for key, value in row.items()
                            if key != "candidate_key"
                        }
                        for row in history
                    ]
                    consolidated["history_consolidation"] = (
                        "prefer-elected-then-registered-official-row"
                    )
                if str(consolidated["candidate_vibid"]) != candidate_vibid:
                    raise AssertionError("candidate history identity changed")
                candidate_rows.append(consolidated)
            if not parsed["valid_candidate_registry"] or not parsed[
                "election_vrn_matches"
            ]:
                registry_errors.append(
                    {
                        "kind": "candidate-registry",
                        "error": "invalid-candidate-registry",
                    }
                )
        else:
            source_errors.append(
                {
                    "kind": "candidate-registry",
                    "error": "candidate-registry-source-has-no-body",
                }
            )
        if not source_is_complete(candidate_source):
            source_errors.append(
                {
                    "kind": "candidate-registry",
                    "error": "candidate-registry-source-incomplete",
                }
            )

    party_choices: list[dict[str, Any]] = []
    party_source: dict[str, Any] | None = None
    party_detail_sources: list[dict[str, Any]] = []
    party_observations = [
        item
        for item in observations
        if "party-registry" in str(item.get("class") or "")
    ]
    party_spec = specs.get("party")
    if party_spec:
        if len(party_observations) != 1:
            source_errors.append(
                {
                    "kind": "party-registry",
                    "expected": 1,
                    "actual": len(party_observations),
                    "error": "expected-one-party-registry-source",
                }
            )
        elif party_observations[0].get("body_path"):
            observation = party_observations[0]
            if not observation.get("valid_result") or not observation.get(
                "kind_matches_requested_type"
            ):
                source_errors.append(
                    {
                        "kind": "party-registry",
                        "error": "party-registry-observation-not-validated",
                    }
                )
            parsed = parse_party_registry(
                body(store, observation),
                str(observation.get("final_url") or observation.get("url") or ""),
                election_vrn=hierarchy["election_vrn"],
            )
            party_source = observation_source(observation)
            if (
                not parsed["valid_party_registry"]
                or not parsed["election_vrn_matches"]
                or parsed["party_count"] != int(party_spec["expected_count"])
            ):
                registry_errors.append(
                    {
                        "kind": "party-registry",
                        "expected": int(party_spec["expected_count"]),
                        "actual": parsed["party_count"],
                        "error": "invalid-or-wrong-size-party-registry",
                    }
                )
            party_choices = [
                {
                    **party,
                    "vote_key": (
                        "gas:association-vibid:"
                        if party["identity_kind"] == "vibid"
                        else "gas:vrnio:"
                    )
                    + str(party["party_list_vrnio"]),
                }
                for party in parsed["parties"]
            ]
        else:
            source_errors.append(
                {
                    "kind": "party-registry",
                    "error": "party-registry-source-has-no-body",
                }
            )
        if not source_is_complete(party_source):
            source_errors.append(
                {
                    "kind": "party-registry",
                    "error": "party-registry-source-incomplete",
                }
            )

        detail_by_association: dict[str, dict[str, Any]] = {}
        detail_source_by_association: dict[str, dict[str, Any]] = {}
        for observation in observations:
            if not str(observation.get("class") or "").startswith("party-detail-"):
                continue
            if not observation.get("body_path"):
                source_errors.append(
                    {
                        "kind": "party-detail",
                        "entity_id": observation.get("entity_id"),
                        "error": "party-detail-source-has-no-body",
                    }
                )
                continue
            if not observation.get("valid_result") or not observation.get(
                "kind_matches_requested_type"
            ):
                source_errors.append(
                    {
                        "kind": "party-detail",
                        "entity_id": observation.get("entity_id"),
                        "error": "party-detail-observation-not-validated",
                    }
                )
            parsed_detail = parse_legacy_party_detail(
                body(store, observation),
                str(observation.get("final_url") or observation.get("url") or ""),
                election_vrn=hierarchy["election_vrn"],
            )
            source = observation_source(observation)
            party_detail_sources.append(
                {"association_vibid": parsed_detail["association_vibid"], **source}
            )
            detail_source_by_association[parsed_detail["association_vibid"]] = source
            if not parsed_detail["valid_party_detail"]:
                registry_errors.append(
                    {
                        "kind": "party-detail",
                        "entity_id": observation.get("entity_id"),
                        "error": "invalid-party-detail",
                    }
                )
            if not source_is_complete(source):
                source_errors.append(
                    {
                        "kind": "party-detail",
                        "entity_id": observation.get("entity_id"),
                        "error": "party-detail-source-incomplete",
                    }
                )
            detail_by_association[parsed_detail["association_vibid"]] = parsed_detail
        for choice in party_choices:
            detail = detail_by_association.get(str(choice["party_list_vrnio"]))
            if detail:
                choice.update(
                    {
                        key: detail[key]
                        for key in (
                            "list_vibid",
                            "official_name",
                            "entity_kind",
                            "charter_registration_date",
                            "charter_registration_number",
                            "draw_number",
                            "mandates",
                            "votes",
                            "vote_percent",
                        )
                    }
                )
                choice["detail_source"] = detail_source_by_association.get(
                    str(choice["party_list_vrnio"])
                )
        expected_detail_count = int(party_spec.get("expected_detail_count", 0))
        if expected_detail_count and len(party_detail_sources) != expected_detail_count:
            source_errors.append(
                {
                    "kind": "party-detail",
                    "expected": expected_detail_count,
                    "actual": len(party_detail_sources),
                    "error": "party-detail-source-count-mismatch",
                }
            )
        if expected_detail_count:
            missing_detail_choices = [
                str(choice["vote_key"])
                for choice in party_choices
                if not source_is_complete(choice.get("detail_source"))
            ]
            if missing_detail_choices:
                source_errors.append(
                    {
                        "kind": "party-detail",
                        "vote_keys": missing_detail_choices,
                        "error": "party-choices-lack-attributed-detail-source",
                    }
                )

    candidates_by_district: defaultdict[int | None, list[dict[str, Any]]] = (
        defaultdict(list)
    )
    for candidate in candidate_rows:
        district = candidate.get("district_number")
        candidates_by_district[int(district) if district is not None else None].append(
            candidate
        )

    configured_result_identity_anomalies: dict[
        tuple[int, str], dict[str, Any]
    ] = {}
    result_identity_anomaly_hits: defaultdict[str, set[str]] = defaultdict(set)
    for configured in (candidate_spec or {}).get("result_identity_anomalies", []):
        district_number = int(configured["district_number"])
        label = str(configured["label"])
        special_key = f"special:official-result-label:{district_number}"
        lookup_key = (district_number, normalized_full_name(label))
        anomaly = {
            "district_number": district_number,
            "label": label,
            "reason": str(configured["reason"]),
            "vote_key": special_key,
        }
        if lookup_key in configured_result_identity_anomalies:
            registry_errors.append(
                {
                    **anomaly,
                    "error": "duplicate-configured-result-identity-anomaly",
                }
            )
        configured_result_identity_anomalies[lookup_key] = anomaly

    winner_config = specs.get("winner", {})
    expected_obfuscated_winner_labels = {
        int(item["district_number"]): str(item["label"])
        for item in winner_config.get("expected_obfuscated_winner_labels", [])
    }
    expected_obfuscated_winner_districts = set(
        expected_obfuscated_winner_labels
    )
    observed_obfuscated_winner_districts: set[int] = set()
    obfuscated_winner_labels: dict[int, str] = {}

    identity_error_keys: set[tuple[Any, ...]] = set()
    matched_party_keys: set[str] = set()
    special_party_keys_seen: set[str] = set()

    def record_identity_error(error: dict[str, Any]) -> None:
        key = (
            error.get("kind"),
            error.get("district_number"),
            error.get("label"),
            error.get("error"),
        )
        if key not in identity_error_keys:
            identity_error_keys.add(key)
            identity_errors.append(error)

    def rewrite_candidate_votes(
        votes: dict[str, int], district_number: int | None, context: dict[str, Any]
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        candidates = candidates_by_district.get(district_number, [])
        eligible = [item for item in candidates if candidate_is_registered(item)]
        for label, count in votes.items():
            if normalized_choice_name(label) == normalized_choice_name("Против всех"):
                key = "special:against-all"
                special_party_keys_seen.add(key)
            else:
                matched = {
                    str(item["candidate_key"])
                    for item in eligible
                    if candidate_label_matches(label, item)
                }
                if len(matched) != 1:
                    label_without_ordinal = RESULT_ORDINAL_RE.sub("", label)
                    configured_anomaly = (
                        configured_result_identity_anomalies.get(
                            (
                                district_number,
                                normalized_full_name(label_without_ordinal),
                            )
                        )
                        if district_number is not None
                        else None
                    )
                    if configured_anomaly is not None:
                        key = str(configured_anomaly["vote_key"])
                        if context.get("uik_tvd"):
                            result_identity_anomaly_hits[key].add(
                                f"uik:{context['uik_tvd']}"
                            )
                        elif context.get("winner_result"):
                            result_identity_anomaly_hits[key].add("oik")
                        elif context.get("tik_tvd"):
                            result_identity_anomaly_hits[key].add(
                                f"tik:{context['tik_tvd']}"
                            )
                    elif (
                        context.get("winner_result")
                        and district_number in expected_obfuscated_winner_districts
                        and not label_without_ordinal.strip()
                        and label
                        == expected_obfuscated_winner_labels[district_number]
                    ):
                        key = (
                            "special:official-obfuscated-winner-label:"
                            f"{district_number}"
                        )
                        observed_obfuscated_winner_districts.add(district_number)
                        obfuscated_winner_labels[district_number] = label
                    else:
                        record_identity_error(
                            {
                                **context,
                                "kind": "candidate",
                                "district_number": district_number,
                                "label": label,
                                "matched_candidate_keys": sorted(matched),
                                "error": "unmatched-or-ambiguous-result-candidate",
                            }
                        )
                        result[label] = int(count)
                        continue
                else:
                    key = next(iter(matched))
            if key in result:
                record_identity_error(
                    {
                        **context,
                        "kind": "candidate",
                        "district_number": district_number,
                        "label": label,
                        "candidate_key": key,
                        "error": "multiple-result-labels-map-to-one-candidate",
                    }
                )
                result[label] = int(count)
            else:
                result[key] = int(count)
        return result

    party_by_name: defaultdict[str, set[str]] = defaultdict(set)
    for choice in party_choices:
        party_by_name[normalized_choice_name(str(choice["name"]))].add(
            str(choice["vote_key"])
        )

    def rewrite_party_votes(
        votes: dict[str, int], context: dict[str, Any]
    ) -> dict[str, int]:
        result: dict[str, int] = {}
        for label, count in votes.items():
            if normalized_choice_name(label) == normalized_choice_name("Против всех"):
                key = "special:against-all"
            else:
                matched = party_by_name.get(normalized_choice_name(label), set())
                if len(matched) != 1:
                    record_identity_error(
                        {
                            **context,
                            "kind": "party",
                            "label": label,
                            "matched_vote_keys": sorted(matched),
                            "error": "unmatched-or-ambiguous-result-party",
                        }
                    )
                    result[label] = int(count)
                    continue
                key = next(iter(matched))
                matched_party_keys.add(key)
            if key in result:
                record_identity_error(
                    {
                        **context,
                        "kind": "party",
                        "label": label,
                        "vote_key": key,
                        "error": "multiple-result-labels-map-to-one-party",
                    }
                )
                result[label] = int(count)
            else:
                result[key] = int(count)
        return result

    for record in records:
        if "candidate_votes" in record and candidate_spec:
            district = record.get("district_number")
            record["candidate_votes"] = rewrite_candidate_votes(
                record["candidate_votes"],
                int(district) if district is not None else None,
                {"uik_tvd": record.get("uik_tvd"), "tik_tvd": record.get("tik_tvd")},
            )
        if "party_votes" in record and party_spec:
            record["party_votes"] = rewrite_party_votes(
                record["party_votes"],
                {"uik_tvd": record.get("uik_tvd"), "tik_tvd": record.get("tik_tvd")},
            )
    for tik_tvd, ballots in tik_protocols.items():
        if candidate_spec and ballots.get("candidate"):
            district = ballots["candidate"].get("district_number")
            if district is None:
                relation = next(
                    (
                        item
                        for item in relations
                        if str(item["tik_tvd"]) == str(tik_tvd)
                    ),
                    {},
                )
                district = relation.get("district_number")
            ballots["candidate"]["votes"] = rewrite_candidate_votes(
                ballots["candidate"]["votes"],
                int(district) if district is not None else None,
                {"tik_tvd": tik_tvd},
            )
        if party_spec and ballots.get("party"):
            ballots["party"]["votes"] = rewrite_party_votes(
                ballots["party"]["votes"], {"tik_tvd": tik_tvd}
            )
    for choice in party_choices:
        choice["is_on_federal_ballot"] = (
            str(choice["vote_key"]) in matched_party_keys
        )
    if "special:against-all" in special_party_keys_seen:
        party_choices.append(
            {
                "vote_key": "special:against-all",
                "party_list_vrnio": "",
                "identity_kind": "special",
                "name": "Против всех",
                "official_name": "Против всех",
                "is_on_federal_ballot": True,
            }
        )
    if party_spec:
        ballot_count = len(matched_party_keys)
        expected_ballot_count = int(party_spec.get("expected_ballot_count", 0))
        if expected_ballot_count and ballot_count != expected_ballot_count:
            registry_errors.append(
                {
                    "kind": "party-registry",
                    "expected": expected_ballot_count,
                    "actual": ballot_count,
                    "error": "official-party-ballot-count-mismatch",
                }
            )

    candidate_tik_key_equality_valid = True
    if candidate_spec and str(candidate_spec.get("scope")) == "election":
        uik_candidate_key_sets = {
            frozenset(map(str, record["candidate_votes"]))
            for record in records
            if isinstance(record.get("candidate_votes"), dict)
        }
        if len(uik_candidate_key_sets) != 1:
            candidate_tik_key_equality_valid = False
            identity_errors.append(
                {
                    "kind": "candidate",
                    "key_sets": [sorted(item) for item in uik_candidate_key_sets],
                    "error": "presidential-uik-candidate-key-set-varies",
                }
            )
        else:
            expected_keys = set(next(iter(uik_candidate_key_sets)))
            expected_partial_tiks = {
                str(item)
                for item in candidate_spec.get(
                    "expected_partial_tik_candidate_maps", []
                )
            }
            actual_partial_tiks: set[str] = set()
            for tik_tvd, ballots in sorted(tik_protocols.items()):
                candidate_ballot = ballots.get("candidate")
                if not isinstance(candidate_ballot, dict) or not isinstance(
                    candidate_ballot.get("votes"), dict
                ):
                    continue
                present_keys = set(map(str, candidate_ballot["votes"]))
                if present_keys == expected_keys:
                    continue
                actual_partial_tiks.add(str(tik_tvd))
                if (
                    hierarchy["election"] == "2004-president"
                    and str(tik_tvd) in expected_partial_tiks
                    and present_keys < expected_keys
                ):
                    allowed_anomalies.append(
                        {
                            "allowed": True,
                            "kind": "partial-official-tik-candidate-map",
                            "election": hierarchy["election"],
                            "tik_tvd": str(tik_tvd),
                            "present_vote_keys": sorted(present_keys),
                            "missing_vote_keys": sorted(expected_keys - present_keys),
                        }
                    )
                else:
                    candidate_tik_key_equality_valid = False
                    identity_errors.append(
                        {
                            "kind": "candidate",
                            "tik_tvd": str(tik_tvd),
                            "present_vote_keys": sorted(present_keys),
                            "expected_vote_keys": sorted(expected_keys),
                            "error": "presidential-tik-candidate-keys-disagree-with-uiks",
                        }
                    )
            if actual_partial_tiks != expected_partial_tiks:
                candidate_tik_key_equality_valid = False
                registry_errors.append(
                    {
                        "kind": "registry-anomalies",
                        "expected_partial_tik_candidate_maps": sorted(
                            expected_partial_tiks
                        ),
                        "actual_partial_tik_candidate_maps": sorted(
                            actual_partial_tiks
                        ),
                        "error": "partial-tik-candidate-map-set-mismatch",
                    }
                )

    district_tik_totals: defaultdict[int, defaultdict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    if candidate_spec and str(candidate_spec.get("scope")) == "district":
        relation_by_tik = {
            str(item["tik_tvd"]): item
            for item in relations
            if item.get("district_number") is not None
        }
        for tik_tvd, ballots in tik_protocols.items():
            candidate_ballot = ballots.get("candidate")
            if not isinstance(candidate_ballot, dict) or not isinstance(
                candidate_ballot.get("votes"), dict
            ):
                continue
            district = candidate_ballot.get("district_number")
            if district is None:
                district = relation_by_tik.get(str(tik_tvd), {}).get(
                    "district_number"
                )
            if district is None:
                registry_errors.append(
                    {
                        "kind": "district-tik-winner-totals",
                        "tik_tvd": str(tik_tvd),
                        "error": "tik-candidate-protocol-lacks-district",
                    }
                )
                continue
            for vote_key, count in candidate_ballot["votes"].items():
                district_tik_totals[int(district)][str(vote_key)] += int(count)

    winner_observations = [
        item
        for item in observations
        if "winner-result" in str(item.get("class") or "")
    ]
    winner_sources: dict[int | None, dict[str, Any]] = {}
    winner_keys: dict[int | None, str] = {}
    expected_winner_sources = 0
    winner_spec = specs.get("winner")
    if winner_spec:
        expected_winner_sources = (
            int(winner_spec.get("expected_count", 0))
            if str(winner_spec.get("scope")) == "district"
            else 1
        )
        if len(winner_observations) != expected_winner_sources:
            source_errors.append(
                {
                    "kind": "winner-result",
                    "expected": expected_winner_sources,
                    "actual": len(winner_observations),
                    "error": "winner-result-source-count-mismatch",
                }
            )
        for observation in winner_observations:
            district = observation.get("district_number")
            district_number = int(district) if district is not None else None
            if not observation.get("body_path"):
                source_errors.append(
                    {
                        "kind": "winner-result",
                        "district_number": district_number,
                        "error": "winner-result-source-has-no-body",
                    }
                )
                continue
            if not observation.get("valid_result") or not observation.get(
                "kind_matches_requested_type"
            ):
                source_errors.append(
                    {
                        "kind": "winner-result",
                        "district_number": district_number,
                        "error": "winner-result-observation-not-validated",
                    }
                )
            source = observation_source(observation)
            winner_sources[district_number] = source
            if not source_is_complete(source):
                source_errors.append(
                    {
                        "kind": "winner-result",
                        "district_number": district_number,
                        "error": "winner-result-source-incomplete",
                    }
                )
            try:
                protocol = aggregate_protocol(body(store, observation))
            except ValueError as caught:
                winner_errors.append(
                    {
                        "district_number": district_number,
                        "error": f"invalid-winner-result: {caught}",
                    }
                )
                continue
            keyed = rewrite_candidate_votes(
                protocol["votes"],
                district_number,
                {
                    "district_number": district_number,
                    "source": source,
                    "winner_result": True,
                },
            )
            if not keyed:
                winner_errors.append(
                    {
                        "district_number": district_number,
                        "error": "winner-result-has-no-candidate-votes",
                    }
                )
                continue
            highest_value = max(keyed.values())
            highest = sorted(
                key for key, value in keyed.items() if int(value) == highest_value
            )
            if len(highest) != 1:
                winner_errors.append(
                    {
                        "district_number": district_number,
                        "candidate_keys": highest,
                        "error": "winner-result-does-not-have-unique-highest-choice",
                    }
                )
                continue
            oik_highest_key = highest[0]
            winner_key = oik_highest_key
            if (
                district_number is not None
                and str(winner_spec.get("scope")) == "district"
            ):
                tik_totals = district_tik_totals.get(district_number, {})
                if not tik_totals:
                    winner_errors.append(
                        {
                            "district_number": district_number,
                            "error": "district-has-no-keyed-tik-winner-totals",
                        }
                    )
                    continue
                tik_highest_value = max(tik_totals.values())
                tik_highest = sorted(
                    key
                    for key, value in tik_totals.items()
                    if int(value) == tik_highest_value
                )
                if len(tik_highest) != 1:
                    winner_errors.append(
                        {
                            "district_number": district_number,
                            "candidate_keys": tik_highest,
                            "error": "summed-tik-results-do-not-have-unique-highest-choice",
                        }
                    )
                    continue
                winner_key = tik_highest[0]
                if not (
                    winner_key.startswith("gas:candidate-vibid:")
                    or winner_key == "special:against-all"
                ):
                    winner_errors.append(
                        {
                            "district_number": district_number,
                            "label": winner_key,
                            "error": "summed-tik-highest-choice-is-unmatched",
                        }
                    )
                    continue
                if highest_value != tik_highest_value:
                    winner_errors.append(
                        {
                            "district_number": district_number,
                            "oik_highest_vote_total": highest_value,
                            "summed_tik_winner_vote_total": tik_highest_value,
                            "winner_candidate_key": winner_key,
                            "error": "oik-highest-total-disagrees-with-summed-tik-winner-total",
                        }
                    )
                    continue
                if (
                    oik_highest_key.startswith("gas:candidate-vibid:")
                    or oik_highest_key == "special:against-all"
                ):
                    if oik_highest_key != winner_key:
                        winner_errors.append(
                            {
                                "district_number": district_number,
                                "oik_highest_choice_key": oik_highest_key,
                                "summed_tik_winner_key": winner_key,
                                "error": "oik-and-summed-tik-winner-keys-disagree",
                            }
                        )
                        continue
                elif not oik_highest_key.startswith(
                    "special:official-obfuscated-winner-label:"
                ):
                    winner_errors.append(
                        {
                            "district_number": district_number,
                            "label": oik_highest_key,
                            "error": "winner-result-highest-choice-is-unmatched",
                        }
                    )
                    continue
                source.update(
                    {
                        "winner_derivation": "summed-official-tik-results-validated-against-oik-total",
                        "oik_unique_highest_vote_total": highest_value,
                        "summed_tik_winner_vote_total": tik_highest_value,
                    }
                )
            elif not (
                winner_key.startswith("gas:candidate-vibid:")
                or winner_key == "special:against-all"
            ):
                winner_errors.append(
                    {
                        "district_number": district_number,
                        "label": winner_key,
                        "error": "winner-result-highest-choice-is-unmatched",
                    }
                )
                continue
            winner_keys[district_number] = winner_key
            elected = {
                str(item["candidate_key"])
                for item in candidates_by_district.get(district_number, [])
                if item.get("is_elected")
            }
            elected_status_accepted = False
            if elected == {winner_key}:
                elected_status_accepted = True
            elif not elected and winner_key == "special:against-all" and hierarchy[
                "election"
            ] == "2003-duma":
                allowed_anomalies.append(
                    {
                        "allowed": True,
                        "kind": "against-all-district-winner",
                        "district_number": district_number,
                        "special_winner_key": winner_key,
                    }
                )
                elected_status_accepted = True
            elif (
                not elected
                and winner_key.startswith("gas:candidate-vibid:")
                and hierarchy["election"] in ("2003-duma", "2016-duma")
            ):
                allowed_anomalies.append(
                    {
                        "allowed": True,
                        "kind": "mutable-registry-elected-status",
                        "election": hierarchy["election"],
                        "district_number": district_number,
                        "winner_candidate_key": winner_key,
                        "registry_statuses": sorted(
                            {
                                str(item.get("registry_election_status") or "")
                                for item in candidates_by_district.get(
                                    district_number, []
                                )
                                if item["candidate_key"] == winner_key
                            }
                        ),
                    }
                )
                elected_status_accepted = True
            else:
                winner_errors.append(
                    {
                        "district_number": district_number,
                        "official_elected_candidate_keys": sorted(elected),
                        "highest_vote_candidate_key": winner_key,
                        "error": "literal-elected-status-disagrees-with-unique-highest-result",
                    }
                )
            if elected_status_accepted:
                for item in candidates_by_district.get(district_number, []):
                    registry_is_elected = bool(item.get("is_elected"))
                    winner_is_elected = str(item["candidate_key"]) == winner_key
                    if registry_is_elected != winner_is_elected:
                        item["registry_is_elected"] = registry_is_elected
                    item["is_elected"] = winner_is_elected

    result_identity_anomalies: list[dict[str, Any]] = []
    for anomaly in sorted(
        configured_result_identity_anomalies.values(),
        key=lambda item: int(item["district_number"]),
    ):
        vote_key = str(anomaly["vote_key"])
        contexts = sorted(result_identity_anomaly_hits.get(vote_key, set()))
        levels = sorted({context.split(":", 1)[0] for context in contexts})
        observed_counts = {
            level: sum(
                context == level or context.startswith(f"{level}:")
                for context in contexts
            )
            for level in levels
        }
        result_identity_anomalies.append(
            {
                **anomaly,
                "observed_levels": levels,
                "observed_counts": observed_counts,
            }
        )
        if levels != ["oik", "tik", "uik"]:
            registry_errors.append(
                {
                    **anomaly,
                    "expected_levels": ["oik", "tik", "uik"],
                    "actual_levels": levels,
                    "error": "configured-result-identity-anomaly-observation-mismatch",
                }
            )
            continue
        allowed_anomalies.append(
            {
                "allowed": True,
                "kind": "official-result-label-identity",
                **anomaly,
                "observed_levels": levels,
                "observed_counts": observed_counts,
            }
        )

    for district_number in sorted(observed_obfuscated_winner_districts):
        allowed_anomalies.append(
            {
                "allowed": True,
                "kind": "obfuscated-oik-winner-label",
                "district_number": district_number,
                "raw_label": obfuscated_winner_labels[district_number],
                "winner_candidate_key": winner_keys.get(district_number),
            }
        )

    district_seeds: dict[int, dict[str, Any]] = {}
    for relation in relations:
        if relation.get("district_number") is None:
            continue
        district_number = int(relation["district_number"])
        seed = {
            "district_number": district_number,
            "oik_tvd": str(relation.get("oik_tvd") or ""),
            "oik_name": str(relation.get("oik_name") or ""),
            "region_code": str(relation.get("region_code") or ""),
            "region_tvd": str(relation.get("region_tvd") or ""),
            "region_name": str(relation.get("region_name") or ""),
        }
        previous = district_seeds.setdefault(district_number, seed)
        if previous != seed:
            registry_errors.append(
                {
                    "district_number": district_number,
                    "error": "conflicting-district-hierarchy-identity",
                }
            )
    districts: list[dict[str, Any]] = []
    if winner_spec and str(winner_spec.get("scope")) == "district":
        for district_number, seed in sorted(district_seeds.items()):
            winner_key = winner_keys.get(district_number)
            winner_vibid = (
                winner_key.removeprefix("gas:candidate-vibid:")
                if winner_key and winner_key.startswith("gas:candidate-vibid:")
                else None
            )
            districts.append(
                {
                    **seed,
                    "winner_candidate_key": winner_key
                    if winner_vibid is not None
                    else None,
                    "winner_candidate_vibid": winner_vibid,
                    "special_winner_key": winner_key
                    if winner_key == "special:against-all"
                    else None,
                    "candidates": candidates_by_district.get(district_number, []),
                    "source": candidate_source,
                    "winner_source": winner_sources.get(district_number),
                }
            )
        if len(districts) != int(winner_spec.get("expected_count", 0)):
            registry_errors.append(
                {
                    "kind": "district-catalog",
                    "expected": int(winner_spec.get("expected_count", 0)),
                    "actual": len(districts),
                    "error": "district-catalog-size-mismatch",
                }
            )

    presidential_winner_key = winner_keys.get(None)
    presidential_winner_vibid = (
        presidential_winner_key.removeprefix("gas:candidate-vibid:")
        if presidential_winner_key
        and presidential_winner_key.startswith("gas:candidate-vibid:")
        else None
    )
    candidate_catalog = (
        {
            "scope": str(candidate_spec.get("scope") or "election"),
            "winner_candidate_key": presidential_winner_key,
            "winner_candidate_vibid": presidential_winner_vibid,
            "candidates": candidate_rows,
            "result_identity_anomalies": result_identity_anomalies,
            "source": candidate_source,
            "winner_source": winner_sources.get(None),
        }
        if candidate_spec
        else None
    )
    party_catalog = (
        {
            "choices": party_choices,
            "source": party_source,
            "detail_sources": sorted(
                party_detail_sources,
                key=lambda item: str(item.get("association_vibid") or ""),
            ),
        }
        if party_spec
        else None
    )
    candidate_key_formula_valid = all(
        item["candidate_key"]
        == f"gas:candidate-vibid:{item['candidate_vibid']}"
        for item in candidate_rows
    )
    party_key_formula_valid = all(
        (
            item["identity_kind"] == "special"
            and item["vote_key"] == "special:against-all"
        )
        or (
            item["identity_kind"] in ("vibid", "vrnio")
            and item["vote_key"]
            == (
                "gas:association-vibid:"
                if item["identity_kind"] == "vibid"
                else "gas:vrnio:"
            )
            + str(item["party_list_vrnio"])
        )
        for item in party_choices
    )
    candidate_vote_keys = {
        str(item["candidate_key"]) for item in candidate_rows
    } | {"special:against-all"} | {
        str(item["vote_key"]) for item in result_identity_anomalies
    }
    party_vote_keys = {str(item["vote_key"]) for item in party_choices} | {
        "special:against-all"
    }
    candidate_vote_key_sets_valid = all(
        set(map(str, record.get("candidate_votes", {}))).issubset(
            candidate_vote_keys
        )
        for record in records
    ) and all(
        set(map(str, ballots.get("candidate", {}).get("votes", {}))).issubset(
            candidate_vote_keys
        )
        for ballots in tik_protocols.values()
    )
    party_vote_key_sets_valid = all(
        set(map(str, record.get("party_votes", {}))).issubset(party_vote_keys)
        for record in records
    ) and all(
        set(map(str, ballots.get("party", {}).get("votes", {}))).issubset(
            party_vote_keys
        )
        for ballots in tik_protocols.values()
    )
    winner_key_exclusivity_valid = all(
        bool(item.get("winner_candidate_vibid"))
        != bool(item.get("special_winner_key"))
        for item in districts
    )
    anomaly_kinds = {
        str(item.get("kind"))
        for item in allowed_anomalies
        if item.get("allowed") is True
    }
    allowed_anomaly_kinds = {
        "candidate-registry-history-duplicates",
        "against-all-district-winner",
        "mutable-registry-elected-status",
        "partial-official-tik-candidate-map",
        "official-result-label-identity",
        "obfuscated-oik-winner-label",
    }
    anomalies_valid = bool(
        all(item.get("allowed") is True for item in allowed_anomalies)
        and anomaly_kinds.issubset(allowed_anomaly_kinds)
    )
    if winner_spec and str(winner_spec.get("scope")) == "district":
        actual_special_districts = sorted(
            int(item["district_number"])
            for item in allowed_anomalies
            if item.get("kind") == "against-all-district-winner"
        )
        expected_special_districts = sorted(
            map(int, winner_spec.get("expected_special_winner_districts", []))
        )
        literal_elected_count = int(
            candidate_spec.get("expected_literal_elected_count", 0)
        )
        expected_mutable_count = (
            int(winner_spec.get("expected_count", 0))
            - literal_elected_count
            - len(expected_special_districts)
        )
        actual_mutable_districts = sorted(
            int(item["district_number"])
            for item in allowed_anomalies
            if item.get("kind") == "mutable-registry-elected-status"
        )
        if actual_special_districts != expected_special_districts:
            anomalies_valid = False
            registry_errors.append(
                {
                    "kind": "registry-anomalies",
                    "expected_special_winner_districts": expected_special_districts,
                    "actual_special_winner_districts": actual_special_districts,
                    "error": "special-winner-anomaly-set-mismatch",
                }
            )
        expected_obfuscated_pairs = sorted(
            (
                int(item["district_number"]),
                str(item["label"]),
            )
            for item in winner_spec.get("expected_obfuscated_winner_labels", [])
        )
        actual_obfuscated_pairs = sorted(
            (
                int(item["district_number"]),
                str(item["raw_label"]),
            )
            for item in allowed_anomalies
            if item.get("kind") == "obfuscated-oik-winner-label"
        )
        if actual_obfuscated_pairs != expected_obfuscated_pairs:
            anomalies_valid = False
            registry_errors.append(
                {
                    "kind": "registry-anomalies",
                    "expected_obfuscated_winner_labels": expected_obfuscated_pairs,
                    "actual_obfuscated_winner_labels": actual_obfuscated_pairs,
                    "error": "obfuscated-winner-label-anomaly-set-mismatch",
                }
            )
        expected_result_identity_keys = sorted(
            str(item["vote_key"]) for item in result_identity_anomalies
        )
        actual_result_identity_keys = sorted(
            str(item["vote_key"])
            for item in allowed_anomalies
            if item.get("kind") == "official-result-label-identity"
        )
        if actual_result_identity_keys != expected_result_identity_keys:
            anomalies_valid = False
            registry_errors.append(
                {
                    "kind": "registry-anomalies",
                    "expected_result_identity_anomaly_keys": (
                        expected_result_identity_keys
                    ),
                    "actual_result_identity_anomaly_keys": (
                        actual_result_identity_keys
                    ),
                    "error": "result-identity-anomaly-set-mismatch",
                }
            )
        configured_mutable_districts = candidate_spec.get(
            "expected_mutable_status_districts"
        )
        expected_mutable_districts = (
            sorted(map(int, configured_mutable_districts))
            if configured_mutable_districts is not None
            else None
        )
        mutable_status_matches = (
            actual_mutable_districts == expected_mutable_districts
            if expected_mutable_districts is not None
            else len(actual_mutable_districts) == expected_mutable_count
        )
        if not mutable_status_matches:
            anomalies_valid = False
            registry_errors.append(
                {
                    "kind": "registry-anomalies",
                    "expected_mutable_status_count": expected_mutable_count,
                    "expected_mutable_status_districts": expected_mutable_districts,
                    "actual_mutable_status_districts": actual_mutable_districts,
                    "error": "mutable-status-anomaly-count-mismatch",
                }
            )
    sources_complete = not source_errors
    identities_complete = not identity_errors
    winners_complete = not winner_errors and (
        not winner_spec or len(winner_keys) == expected_winner_sources
    )
    registry_gates = {
        "sources_complete": sources_complete,
        "registry_counts_complete": not registry_errors,
        "every_result_choice_matched_to_official_identity": identities_complete,
        "identity_key_formulas_valid": candidate_key_formula_valid
        and party_key_formula_valid,
        "uik_and_tik_vote_key_sets_valid": candidate_vote_key_sets_valid
        and party_vote_key_sets_valid
        and candidate_tik_key_equality_valid,
        "regular_and_special_winner_keys_exclusive": winner_key_exclusivity_valid,
        "allowed_source_anomalies_exact": anomalies_valid,
        "literal_elected_and_unique_highest_winner_agree": winners_complete,
        "allowed_anomalies": allowed_anomalies,
        "errors": {
            "sources": source_errors,
            "registry": registry_errors,
            "identity": identity_errors,
            "winner": winner_errors,
        },
    }
    registry_gates["passed"] = bool(
        sources_complete
        and not registry_errors
        and identities_complete
        and candidate_key_formula_valid
        and party_key_formula_valid
        and candidate_vote_key_sets_valid
        and party_vote_key_sets_valid
        and candidate_tik_key_equality_valid
        and winner_key_exclusivity_valid
        and anomalies_valid
        and winners_complete
    )
    registry_gates["publishable"] = registry_gates["passed"]
    return {
        "candidate_catalog": candidate_catalog,
        "party_catalog": party_catalog,
        "districts": districts,
        "result_identity_anomalies": result_identity_anomalies,
        "winner_source": winner_sources.get(None),
        "registry_gates": registry_gates,
    }


def build(args: argparse.Namespace) -> int:
    hierarchy, crawl_report = load(args.hierarchy), load(args.crawl_report)
    observations = list(crawl_report["observations"])
    for path in args.additional_crawl_report or []:
        observations.extend(load(path)["observations"])
    store = ResponseStore(args.raw_dir)
    by_id = {str(node["node_id"]): node for node in hierarchy["nodes"]}
    relations = enriched_relations(hierarchy)
    district_numbers = district_numbers_from_observations(
        hierarchy, relations, observations, store
    )
    for relation in relations:
        oik_tvd = str(relation.get("oik_tvd") or "")
        relation["district_number"] = district_numbers.get(oik_tvd)
        relation["oik_tvd"] = oik_tvd or None
        relation["oik_name"] = relation.get("oik_name") or None
    relation_by_uik = {str(item["uik_tvd"]): item for item in relations}
    relation_by_tik: dict[str, dict[str, Any]] = {}
    for relation in relations:
        tik_tvd = str(relation["tik_tvd"])
        scope = {
            key: relation.get(key)
            for key in (
                "region",
                "region_code",
                "region_tvd",
                "region_name",
                "district_number",
                "oik_tvd",
                "oik_name",
            )
        }
        previous = relation_by_tik.setdefault(tik_tvd, scope)
        if previous != scope:
            raise ValueError(f"TIK {tik_tvd} has conflicting hierarchy identities")
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
        request_class = str(observation.get("class") or "")
        if (
            "registry" in request_class
            or "winner-result" in request_class
            or request_class.startswith("party-detail-")
        ):
            continue
        report_type = int(observation["report_type"])
        if report_type not in kinds:
            continue
        level, configured_contest = kinds[report_type]
        contest = str(observation.get("contest") or configured_contest)
        if str(observation.get("class", "")).startswith("tic-") and (
            level == "uik" or observation.get("level") == "uik-direct-protocol"
        ):
            tik_tvd = str(observation["entity_id"])
            payload = body(store, observation)
            protocol = aggregate_protocol(
                payload, commission_name=tik_names.get(tik_tvd, "")
            )
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
                    "uik_name": relation["uik_name"],
                    "tik_tvd": str(relation["tik_tvd"]),
                    "tik_name": tik_names.get(str(relation["tik_tvd"]), ""),
                    **{
                        key: relation.get(key)
                        for key in (
                            "region",
                            "region_code",
                            "region_tvd",
                            "region_name",
                            "district_number",
                            "oik_tvd",
                            "oik_name",
                        )
                    },
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
                    "uik_name": relation["uik_name"],
                    "tik_tvd": tik_tvd,
                    "tik_name": tik_names.get(tik_tvd, ""),
                    **{
                        key: relation.get(key)
                        for key in (
                            "region",
                            "region_code",
                            "region_tvd",
                            "region_name",
                            "district_number",
                            "oik_tvd",
                            "oik_name",
                        )
                    },
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
    registry_products = build_registry_products(
        hierarchy,
        observations,
        store,
        relations,
        records,
        tik_protocols,
    )
    result = {
        "schema_version": 5,
        "election": hierarchy["election"],
        "election_vrn": hierarchy["election_vrn"],
        "contests": hierarchy["contests"],
        "records": records,
        "complete_uik_count": sum(required.issubset(record) for record in records),
        "sources": [
            {
                "tik_tvd": key,
                "tik_name": tik_names.get(key, ""),
                **relation_by_tik[key],
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
        "district_hierarchy": {
            "applicable": has_single_member_districts(hierarchy),
            "districts": len(district_numbers),
            "complete": not has_single_member_districts(hierarchy)
            or len(district_numbers) == 225,
        },
        **registry_products,
    }
    json_write(args.output, result)
    invalid = bool(
        failures
        or build_errors
        or incomplete
        or missing
        or conflicts
        or len(complete_tiks) != hierarchy["tiks"]
        or not registry_products["registry_gates"]["passed"]
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
                "registry_gates_passed": registry_products["registry_gates"][
                    "passed"
                ],
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
    regions: dict[str, dict[str, Any]] = {}
    for relation in protocols["relations"]:
        region_tvd = str(relation.get("region_tvd") or "")
        row = regions.setdefault(
            region_tvd,
            {
                "regionCode": str(relation.get("region_code") or ""),
                "regionTvd": region_tvd,
                "regionName": str(relation.get("region_name") or ""),
                "tiks": set(),
                "uiks": set(),
                **{contest: set() for contest in hierarchy["contests"]},
            },
        )
        row["tiks"].add(str(relation["tik_tvd"]))
        row["uiks"].add(str(relation["uik_tvd"]))
    for record in protocols["records"]:
        row = regions[str(record.get("region_tvd") or "")]
        for contest in hierarchy["contests"]:
            if {f"{contest}_accounting", f"{contest}_votes"}.issubset(record):
                row[contest].add(str(record["uik_tvd"]))
    output = []
    for region_tvd, row in sorted(regions.items()):
        output.append(
            {
                "region": row["regionCode"],
                "regionCode": row["regionCode"],
                "regionTvd": region_tvd,
                "regionName": row["regionName"],
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
        "schema_version": 3,
        "election": hierarchy["election"],
        "regions": output,
        "registry_gates": protocols.get("registry_gates", {}),
        "summary": {
            "regions": len(output),
            "districts": int(
                protocols.get("district_hierarchy", {}).get("districts", 0)
            ),
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
            "registry_sources_complete": bool(
                protocols.get("registry_gates", {}).get("sources_complete")
            ),
            "registry_gates_passed": bool(
                protocols.get("registry_gates", {}).get("passed")
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
        and result["summary"]["registry_gates_passed"]
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
