from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import re
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from zipfile import BadZipFile, ZipFile

try:
    from .common import decode_text, extract_tree_nodes, json_write
    from .decode_script_result import decode_script_tables
    from .export_uik_results import indexed_uiks, transpose_table
    from .pipeline import (
        DUMA_VRN,
        classify_result,
        coverage_report,
        extract_report_links,
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
        DUMA_VRN,
        classify_result,
        coverage_report,
        extract_report_links,
        hierarchy_summary,
        make_plan,
        reconcile,
        tree_endpoint,
    )
    from shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from transport import FetchConfig, Fetcher, ResponseStore


DEFAULT_RAW = Path("data/raw/gas-duma-2021")
DEFAULT_REPORTS = Path("reports/generated/gas-duma-2021")
DUMA_WINNERS_OFFICIAL_URL = (
    "http://cikrf.ru/upload/decree-of-cec/61-467-8-pril.docx"
)
DUMA_WINNERS_ARCHIVE_URL = (
    "https://web.archive.org/web/20210926045732id_/"
    "http://www.cikrf.ru/upload/decree-of-cec/61-467-8-pril.docx"
)
OIK_BREADCRUMB_RE = re.compile(r"^ОИК\s*№\s*(\d+)$", re.IGNORECASE)
DISTRICT_NUMBER_RE = re.compile(r"округ\s*№\s*(\d+)\s*$", re.IGNORECASE)
WORD_NAMESPACE = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
}


def _normalized_text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


def _normalized_gas_text(value: str) -> str:
    # Some live GAS pages expose Java-style escaping in otherwise plain HTML.
    # It is a rendering artifact, not part of the official entity name.
    return _normalized_text(value).replace('\\"', '"')


class _LinkTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._href: str | None = None
        self._text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.base_href: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        if tag.casefold() == "base" and attributes.get("href"):
            self.base_href = attributes["href"]
        elif tag.casefold() == "a":
            self._href = attributes.get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, _normalized_text("".join(self._text))))
            self._href = None
            self._text = []


def extract_oik_breadcrumbs(payload: bytes, source_url: str) -> list[dict[str, Any]]:
    source, _ = decode_text(payload)
    parser = _LinkTextParser()
    parser.feed(source)
    result: dict[tuple[int, str], dict[str, Any]] = {}
    for href, label in parser.links:
        match = OIK_BREADCRUMB_RE.fullmatch(label)
        if not match:
            continue
        url = urllib.parse.urljoin(parser.base_href or source_url, href)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        oik_tvd = query.get("tvd")
        if not oik_tvd:
            continue
        key = (int(match.group(1)), str(oik_tvd))
        result[key] = {
            "district_number": key[0],
            "oik_tvd": key[1],
            "url": url,
        }
    return [result[key] for key in sorted(result)]


class _CandidateRegistryParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.table_depth = 0
        self.row: list[dict[str, Any]] | None = None
        self.cell: dict[str, Any] | None = None
        self.rows: list[list[dict[str, Any]]] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = dict(attrs)
        folded = tag.casefold()
        if folded == "table":
            if self.in_table:
                self.table_depth += 1
            elif str(attributes.get("id", "")).startswith("candidates-220-"):
                self.in_table = True
                self.table_depth = 1
            return
        if not self.in_table:
            return
        if folded == "tr":
            self.row = []
        elif folded in ("td", "th") and self.row is not None:
            self.cell = {"text": [], "hrefs": []}
        elif folded == "a" and self.cell is not None and attributes.get("href"):
            self.cell["hrefs"].append(str(attributes["href"]))
        elif folded == "br" and self.cell is not None:
            self.cell["text"].append(" ")

    def handle_data(self, data: str) -> None:
        if self.cell is not None:
            self.cell["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        folded = tag.casefold()
        if not self.in_table:
            return
        if folded in ("td", "th") and self.cell is not None and self.row is not None:
            self.row.append(
                {
                    "text": _normalized_text("".join(self.cell["text"])),
                    "hrefs": self.cell["hrefs"],
                }
            )
            self.cell = None
        elif folded == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif folded == "table":
            self.table_depth -= 1
            if self.table_depth == 0:
                self.in_table = False


def parse_candidate_registry(payload: bytes, source_url: str = "") -> dict[str, Any]:
    source, encoding = decode_text(payload)
    parser = _CandidateRegistryParser()
    parser.feed(source)
    candidates: list[dict[str, Any]] = []
    for row in parser.rows:
        if len(row) < 10 or not row[0]["text"].isdigit():
            continue
        href = next(iter(row[1]["hrefs"]), "")
        url = urllib.parse.urljoin(source_url, href)
        query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        candidate_vibid = query.get("vibid")
        district_text = row[4]["text"]
        if not candidate_vibid or not district_text.isdigit():
            continue
        candidates.append(
            {
                "candidate_vibid": str(candidate_vibid),
                "full_name": _normalized_text(row[1]["text"]),
                "district_number": int(district_text),
                "nominating_entity": _normalized_gas_text(row[3]["text"]),
                "registration_status": _normalized_text(row[6]["text"]),
                "registry_election_status": _normalized_text(row[9]["text"]),
                "is_elected": False,
            }
        )
    return {
        "valid_candidate_registry": bool(candidates)
        and DUMA_VRN in source
        and "candidates-220-" in source,
        "encoding": encoding,
        "candidate_count": len(candidates),
        "district_numbers": sorted({item["district_number"] for item in candidates}),
        "candidates": candidates,
    }


def parse_official_winners(payload: bytes) -> dict[str, Any]:
    """Parse the immutable 225-winner appendix to CEC Resolution 61/467-8."""
    try:
        with ZipFile(io.BytesIO(payload)) as archive:
            root = ET.fromstring(archive.read("word/document.xml"))
    except (BadZipFile, KeyError, ET.ParseError):
        return {"valid_winner_registry": False, "winner_count": 0, "winners": []}
    candidates: list[tuple[int, list[dict[str, Any]]]] = []
    for table in root.findall(".//w:tbl", WORD_NAMESPACE):
        rows: list[dict[str, Any]] = []
        for row in table.findall("./w:tr", WORD_NAMESPACE):
            paragraphs = []
            for paragraph in row.findall(".//w:p", WORD_NAMESPACE):
                text = _normalized_text(
                    "".join(
                        node.text or ""
                        for node in paragraph.findall(".//w:t", WORD_NAMESPACE)
                    )
                )
                if text:
                    paragraphs.append(text)
            if len(paragraphs) < 2:
                continue
            match = DISTRICT_NUMBER_RE.search(paragraphs[0])
            if match:
                rows.append(
                    {
                        "district_number": int(match.group(1)),
                        "district_name": paragraphs[0],
                        "full_name": paragraphs[1],
                    }
                )
        if rows:
            candidates.append((len(rows), rows))
    winners = max(candidates, default=(0, []))[1]
    numbers = [item["district_number"] for item in winners]
    valid = (
        len(winners) == 225
        and len(set(numbers)) == 225
        and set(numbers) == set(range(1, 226))
    )
    return {
        "valid_winner_registry": valid,
        "winner_count": len(winners),
        "winners": winners,
    }


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
    selected_regions = set(args.region or [])
    nodes = {
        node.node_id: node
        for node in root_nodes
        if node.node_id
        and (
            not selected_regions
            or node.parent_id is None
            or node.region in selected_regions
        )
    }
    queue = [
        node
        for node in root_nodes
        if node.load_on_demand
        and (not selected_regions or node.region in selected_regions)
    ]
    processed: set[str] = set()

    def expand(node: Any) -> tuple[Any, list[Any]]:
        record = fetcher.fetch(
            tree_endpoint({"vrn": node.vrn, "tvd": node.tvd}), refresh=args.refresh
        )
        if "body_path" not in record or int(record.get("status", 0)) != 200:
            return node, []
        children, _ = extract_tree_nodes(
            _body(store, record), record.get("final_url", ""), node.node_id
        )
        return node, [child for child in children if child.node_id != node.node_id]

    def next_pending() -> Any | None:
        while queue:
            node = queue.pop(0)
            if not node.node_id or node.node_id in processed:
                continue
            processed.add(node.node_id)
            return node
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures: dict[concurrent.futures.Future[Any], Any] = {}
        while True:
            while len(futures) < args.concurrency:
                node = next_pending()
                if node is None:
                    break
                futures[pool.submit(expand, node)] = node
            if not futures:
                break
            done, _ = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                futures.pop(future)
                _, children = future.result()
                for child in children:
                    if not child.node_id:
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
                                "pending": len(queue) + len(futures),
                            }
                        ),
                        flush=True,
                    )
    output_nodes = [vars(nodes[key]) for key in sorted(nodes)]
    summary = hierarchy_summary(output_nodes, include_oik=True)
    by_id = {str(node["node_id"]): node for node in output_nodes}
    tik_ids = sorted(
        {str(item["tik_tvd"]) for item in summary["uik_to_tik"]}
    )

    def resolve_report_links(
        tik_id: str,
    ) -> tuple[str, dict[str, str], list[dict[str, Any]]]:
        node = by_id[tik_id]
        record = fetcher.fetch(str(node["url"]), refresh=args.refresh)
        if "body_path" not in record or int(record.get("status", 0)) != 200:
            return tik_id, {}, []
        payload = _body(store, record)
        source_url = str(record.get("final_url", node["url"]))
        links = extract_report_links(
            payload, source_url
        )
        return (
            tik_id,
            {
                str(item["report_type"]): str(item["url"])
                for item in links
                if int(item["report_type"]) in (233, 464)
            },
            extract_oik_breadcrumbs(payload, source_url),
        )

    report_links: dict[str, dict[str, str]] = {}
    breadcrumbs_by_tik: dict[str, list[dict[str, Any]]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(resolve_report_links, tik_id) for tik_id in tik_ids]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            tik_id, links, breadcrumbs = future.result()
            report_links[tik_id] = links
            breadcrumbs_by_tik[tik_id] = breadcrumbs
            if index % 100 == 0 or index == len(futures):
                print(
                    json.dumps(
                        {
                            "report_link_requests": index,
                            "total": len(futures),
                            "resolved": sum(
                                {"233", "464"}.issubset(value)
                                for value in report_links.values()
                            ),
                        }
                    ),
                    flush=True,
                )
    links_complete = all(
        {"233", "464"}.issubset(report_links.get(tik_id, {})) for tik_id in tik_ids
    )
    breadcrumb_errors = [
        {
            "tik_tvd": tik_id,
            "expected_oik_tvd": next(
                (
                    str(item.get("oik_tvd"))
                    for item in summary["uik_to_tik"]
                    if str(item["tik_tvd"]) == tik_id
                ),
                "",
            ),
            "breadcrumbs": breadcrumbs_by_tik.get(tik_id, []),
        }
        for tik_id in tik_ids
        if len(breadcrumbs_by_tik.get(tik_id, [])) != 1
    ]
    relation_by_tik = {
        str(item["tik_tvd"]): item for item in summary["uik_to_tik"]
    }
    for tik_id, breadcrumbs in breadcrumbs_by_tik.items():
        if len(breadcrumbs) == 1 and str(breadcrumbs[0]["oik_tvd"]) != str(
            relation_by_tik[tik_id].get("oik_tvd")
        ):
            breadcrumb_errors.append(
                {
                    "tik_tvd": tik_id,
                    "expected_oik_tvd": relation_by_tik[tik_id].get("oik_tvd"),
                    "breadcrumbs": breadcrumbs,
                }
            )
    district_links: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for breadcrumbs in breadcrumbs_by_tik.values():
        if len(breadcrumbs) == 1:
            district_links[str(breadcrumbs[0]["oik_tvd"])].append(breadcrumbs[0])
    district_identities: dict[str, dict[str, Any]] = {}
    district_conflicts: list[dict[str, Any]] = []
    for oik_tvd, links in sorted(district_links.items()):
        numbers = {int(item["district_number"]) for item in links}
        urls = {str(item["url"]) for item in links}
        if len(numbers) != 1 or len(urls) != 1:
            district_conflicts.append(
                {
                    "oik_tvd": oik_tvd,
                    "district_numbers": sorted(numbers),
                    "urls": sorted(urls),
                }
            )
            continue
        district_identities[oik_tvd] = {
            "district_number": next(iter(numbers)),
            "oik_tvd": oik_tvd,
            "oik_url": next(iter(urls)),
        }
    oiks_by_number: defaultdict[int, list[str]] = defaultdict(list)
    for oik_tvd, item in district_identities.items():
        oiks_by_number[int(item["district_number"])].append(oik_tvd)
    district_conflicts.extend(
        {
            "district_number": district_number,
            "oik_tvds": sorted(values),
            "error": "district-number-is-not-a-bijection",
        }
        for district_number, values in sorted(oiks_by_number.items())
        if len(values) != 1
    )

    def resolve_candidate_registry(
        district: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        record = fetcher.fetch(str(district["oik_url"]), refresh=args.refresh)
        result = {**district, "navigation": record}
        if "body_path" not in record or int(record.get("status", 0)) != 200:
            return str(district["oik_tvd"]), result
        links = extract_report_links(
            _body(store, record),
            str(record.get("final_url", district["oik_url"])),
            report_kind={220: ("oik", "candidate-registry")},
        )
        exact = sorted({str(item["url"]) for item in links})
        if len(exact) == 1:
            result["candidate_registry_url"] = exact[0]
        else:
            result["candidate_registry_urls"] = exact
        return str(district["oik_tvd"]), result

    resolved_districts: dict[str, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [
            pool.submit(resolve_candidate_registry, district)
            for district in district_identities.values()
        ]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            oik_tvd, district = future.result()
            resolved_districts[oik_tvd] = district
            if index % 25 == 0 or index == len(futures):
                print(
                    json.dumps(
                        {
                            "oik_navigation_requests": index,
                            "total": len(futures),
                            "candidate_registry_links": sum(
                                "candidate_registry_url" in item
                                for item in resolved_districts.values()
                            ),
                        }
                    ),
                    flush=True,
                )
    district_catalog: list[dict[str, Any]] = []
    for oik_tvd, district in sorted(
        resolved_districts.items(), key=lambda item: item[1]["district_number"]
    ):
        oik = by_id.get(oik_tvd, {})
        region = by_id.get(str(oik.get("parent_id")), {})
        district_catalog.append(
            {
                "district_number": int(district["district_number"]),
                "oik_tvd": oik_tvd,
                "oik_name": str(oik.get("text") or ""),
                "oik_url": str(district["oik_url"]),
                "region_code": str(region.get("region") or oik.get("region") or ""),
                "region_tvd": str(region.get("node_id") or ""),
                "region_name": str(region.get("text") or ""),
                **(
                    {"candidate_registry_url": district["candidate_registry_url"]}
                    if district.get("candidate_registry_url")
                    else {}
                ),
            }
        )
    district_number_by_oik = {
        item["oik_tvd"]: item["district_number"] for item in district_catalog
    }
    for relation in summary["uik_to_tik"]:
        if relation.get("oik_tvd") in district_number_by_oik:
            relation["district_number"] = district_number_by_oik[
                str(relation["oik_tvd"])
            ]
    candidate_links_complete = (
        not breadcrumb_errors
        and not district_conflicts
        and len(district_catalog) == int(summary["districts"])
        and len({item["district_number"] for item in district_catalog})
        == len(district_catalog)
        and all(item.get("candidate_registry_url") for item in district_catalog)
    )
    result = {
        "schema_version": 2,
        "root_source": str(args.root_html),
        "root_encoding": encoding,
        "nodes": output_nodes,
        **summary,
        "report_links": dict(sorted(report_links.items())),
        "report_links_complete": links_complete,
        "district_catalog": district_catalog,
        "district_breadcrumb_errors": breadcrumb_errors,
        "district_identity_conflicts": district_conflicts,
        "candidate_registry_links_complete": candidate_links_complete,
    }
    json_write(args.output, result)
    print(
        json.dumps(
            {
                key: result[key]
                for key in (
                    "regions",
                    "districts",
                    "tiks",
                    "uiks",
                    "complete",
                    "report_links_complete",
                    "candidate_registry_links_complete",
                )
            },
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
        report_links=hierarchy.get("report_links"),
        candidate_registries=hierarchy.get("district_catalog"),
        include_oik=True,
    )
    result["requests"].append(
        {
            "class": "election-winners-docx",
            "report_type": "cec-resolution-61-467-8",
            "entity_id": DUMA_VRN,
            "region": "0",
            "url": DUMA_WINNERS_ARCHIVE_URL,
            "official_url": DUMA_WINNERS_OFFICIAL_URL,
            "url_source": "official-wayback",
        }
    )
    result["request_classes"] = dict(
        sorted(Counter(item["class"] for item in result["requests"]).items())
    )
    result["url_sources"] = dict(
        sorted(Counter(item["url_source"] for item in result["requests"]).items())
    )
    result["estimated_requests"] = len(result["requests"])
    result["rate"] = args.rate
    result["concurrency"] = args.concurrency
    result["raw_dir"] = str(args.raw_dir)
    result["manifest"] = str(args.raw_dir / "manifest.json")
    result["coordination_dir"] = str(args.coordination_dir)
    result["output_layout"] = (
        "proper-data/2021-duma/protocol/{tic,uik}/{type}/"
        "region-{region}[-part-{batch}].ts"
    )
    exact_links_complete = sum(
        result["url_sources"].get(kind, 0)
        for kind in ("official-navigation", "official-wayback")
    ) == result["estimated_requests"]
    expected_district_requests = len(
        {
            str(item.get("oik_tvd"))
            for item in hierarchy.get("district_catalog", [])
            if item.get("oik_tvd")
            and (not args.region or str(item.get("region_code")) in set(args.region))
        }
    )
    candidate_links_complete = (
        result["request_classes"].get("district-220", 0)
        == expected_district_requests
        and expected_district_requests > 0
    )
    # A full plan must retain all discovered OIKs. Regional probe plans retain
    # exactly the OIK subset for the requested region codes.
    if not args.region:
        candidate_links_complete = candidate_links_complete and (
            expected_district_requests == int(result["hierarchy"].get("districts", 0))
        )
    result["exact_report_links_complete"] = exact_links_complete
    result["candidate_registry_links_complete"] = candidate_links_complete
    result["ready"] = (
        result["hierarchy"]["complete"]
        and exact_links_complete
        and candidate_links_complete
    )
    if result["ready"]:
        result["resume_command"] = (
            "proper-app/.venv/bin/python proper-data/crawler/duma2021.py "
            f"crawl --plan {args.output}"
        )
    else:
        result["blocked_reason"] = (
            "hierarchy has unresolved load-on-demand nodes"
            if not result["hierarchy"]["complete"]
            else "official navigation report or candidate-registry links are incomplete; resume discovery"
        )
        result["resume_command"] = (
            "proper-app/.venv/bin/python proper-data/crawler/duma2021.py discover --root-html data/raw/duma-2021-single-member-cec/index/a52134a1b6d1e88209d6.html --output reports/generated/gas-duma-2021/hierarchy.json"
        )
    json_write(args.output, result)
    printable = {
        key: result[key]
        for key in (
            "hierarchy",
            "request_classes",
            "url_sources",
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
    if not plan_data.get("ready"):
        raise SystemExit(
            "refusing crawl: plan lacks a complete hierarchy or exact official "
            "report links; resume discovery first"
        )
    store, fetcher = _settings(args)
    requests = plan_data["requests"]
    if args.request_class:
        selected_classes = set(args.request_class)
        requests = [item for item in requests if item["class"] in selected_classes]
    if args.only_failures:
        requests = [
            item
            for item in requests
            if not store.verified(item["url"])
            or int(store.verified(item["url"]).get("status", 0)) not in range(200, 300)
        ]
    stats: Counter[str] = Counter()
    decoded: Counter[str] = Counter()
    retry_count = 0
    permanent_failures = 0
    started = time.monotonic()
    observations: list[dict[str, Any]] = []

    def run(item: dict[str, Any]) -> dict[str, Any]:
        # ``--only-failures`` is an explicit new retry pass. Without this refresh,
        # preserved permanent-4xx observations would be selected above and then
        # immediately returned as cache hits by the generic resume behavior.
        record = fetcher.fetch(
            item["url"], refresh=args.refresh or args.only_failures
        )
        classification = {}
        if record.get("body_path"):
            if item.get("class") == "election-winners-docx":
                classification = parse_official_winners(_body(store, record))
                classification.update(
                    {
                        "valid_result": classification["valid_winner_registry"],
                        "kind_matches_requested_type": True,
                        "level": "election-winner-registry",
                        "ballot": "winner-registry",
                    }
                )
            elif int(item["report_type"]) == 220:
                classification = parse_candidate_registry(
                    _body(store, record), str(record.get("final_url", item["url"]))
                )
                classification.update(
                    {
                        "valid_result": classification[
                            "valid_candidate_registry"
                        ]
                        and classification["district_numbers"]
                        == [int(item["district_number"])],
                        "kind_matches_requested_type": True,
                        "level": "district-candidate-registry",
                        "ballot": "candidate-registry",
                    }
                )
            else:
                classification = classify_result(
                    _body(store, record), int(item["report_type"])
                )
        return {**item, **record, **classification}

    total = len(requests)
    request_iterator = iter(requests)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures: set[concurrent.futures.Future[Any]] = set()
        for _ in range(min(args.concurrency, total)):
            futures.add(pool.submit(run, next(request_iterator)))
        index = 0
        while futures:
            done, futures = concurrent.futures.wait(
                futures, return_when=concurrent.futures.FIRST_COMPLETED
            )
            for future in done:
                index += 1
                row = future.result()
                observations.append(row)
                stats[str(row.get("status", row.get("error_class", "error")))] += 1
                if row.get("retry_count"):
                    retry_count += int(row["retry_count"])
                status = int(row.get("status", 0))
                if 400 <= status < 500 and status != 429:
                    permanent_failures += 1
                if row.get("valid_result"):
                    ballot = str(row.get("ballot", "unknown"))
                    decoded[f"{ballot}_tables"] += 1
                    decoded[f"{ballot}_uiks"] += len(row.get("uik_numbers", []))
                try:
                    item = next(request_iterator)
                except StopIteration:
                    pass
                else:
                    futures.add(pool.submit(run, item))
                if index % args.progress_every == 0 or index == total:
                    elapsed = max(0.001, time.monotonic() - started)
                    rps = index / elapsed
                    remaining = total - index
                    print(
                        json.dumps(
                            {
                                "completed": index,
                                "total": total,
                                "rolling_rps": round(rps, 2),
                                "statuses": dict(stats),
                                "retries": retry_count,
                                "permanent_failures": permanent_failures,
                                "decoded_party_tables": decoded["party_tables"],
                                "decoded_candidate_tables": decoded[
                                    "candidate_tables"
                                ],
                                "party_uiks_covered": decoded["party_uiks"],
                                "candidate_uiks_covered": decoded["candidate_uiks"],
                                "reconciliation_failures": "pending-offline-build",
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
    observations = list(crawl_report["observations"])
    for path in args.additional_crawl_report or []:
        observations.extend(_load(path)["observations"])
    report = coverage_report(hierarchy["nodes"], observations)
    if args.protocols:
        protocols = _load(args.protocols)
        failed_tiks = {
            str(item["tik_tvd"])
            for item in protocols.get("reconciliation_failures", [])
        }
        region_tiks: defaultdict[str, set[str]] = defaultdict(set)
        for relation in hierarchy.get(
            "uik_to_tik",
            hierarchy_summary(hierarchy["nodes"], include_oik=True)["uik_to_tik"],
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
    observations = list(crawl_report["observations"])
    for path in args.additional_crawl_report or []:
        observations.extend(_load(path)["observations"])
    store = ResponseStore(args.raw_dir)
    tree = {"nodes": hierarchy["nodes"]}
    paired: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    sources: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    failures: list[dict[str, Any]] = []
    direct: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    conflicts: list[dict[str, Any]] = []
    tik_names = {str(node["node_id"]): str(node["text"]) for node in hierarchy["nodes"]}
    relations = hierarchy.get(
        "uik_to_tik",
        hierarchy_summary(hierarchy["nodes"], include_oik=True)["uik_to_tik"],
    )
    district_catalog_seed = list(hierarchy.get("district_catalog", []))
    district_by_number = {
        int(item["district_number"]): item
        for item in district_catalog_seed
        if item.get("district_number") is not None
    }
    registry_candidates: defaultdict[int, list[dict[str, Any]]] = defaultdict(list)
    registry_sources: dict[int, dict[str, Any]] = {}
    candidate_registry_errors: list[dict[str, Any]] = []
    winner_registry_errors: list[dict[str, Any]] = []
    official_winners: dict[int, str] = {}
    winner_registry_source: dict[str, Any] | None = None
    for observation in observations:
        if observation.get("class") != "election-winners-docx":
            continue
        if (
            winner_registry_source is not None
            or not observation.get("body_path")
            or not observation.get("sha256")
            or int(observation.get("status", 0)) != 200
        ):
            winner_registry_errors.append(
                {"error": "duplicate-missing-or-unhashed-winner-registry-source"}
            )
            continue
        parsed_winners = parse_official_winners(_body(store, observation))
        if not parsed_winners["valid_winner_registry"]:
            winner_registry_errors.append(
                {
                    "error": "invalid-winner-registry-source",
                    "winner_count": parsed_winners["winner_count"],
                }
            )
            continue
        official_winners = {
            int(item["district_number"]): _normalized_text(item["full_name"])
            for item in parsed_winners["winners"]
        }
        winner_registry_source = {
            "official_url": observation.get("official_url")
            or DUMA_WINNERS_OFFICIAL_URL,
            "final_url": observation.get("final_url"),
            "sha256": observation["sha256"],
            "retrieved_at": observation.get("retrieved_at"),
            "provenance": observation.get("provenance"),
            "resolution": "61/467-8",
            "resolution_date": "2021-09-24",
        }
    for observation in observations:
        if str(observation.get("report_type", "")) != "220":
            continue
        district_number = int(observation.get("district_number", 0))
        if (
            not district_number
            or not observation.get("body_path")
            or not observation.get("sha256")
            or int(observation.get("status", 0)) != 200
        ):
            candidate_registry_errors.append(
                {
                    "district_number": district_number or None,
                    "oik_tvd": observation.get("oik_tvd")
                    or observation.get("entity_id"),
                    "error": "missing-or-unhashed-type-220-source",
                }
            )
            continue
        parsed = parse_candidate_registry(
            _body(store, observation),
            str(observation.get("final_url") or observation.get("url") or ""),
        )
        if (
            not parsed["valid_candidate_registry"]
            or parsed["district_numbers"] != [district_number]
        ):
            candidate_registry_errors.append(
                {
                    "district_number": district_number,
                    "oik_tvd": observation.get("oik_tvd")
                    or observation.get("entity_id"),
                    "error": "invalid-or-wrong-district-type-220-source",
                    "parsed_district_numbers": parsed["district_numbers"],
                }
            )
            continue
        if district_number in registry_sources:
            candidate_registry_errors.append(
                {
                    "district_number": district_number,
                    "error": "duplicate-type-220-source",
                }
            )
            continue
        registry_candidates[district_number] = parsed["candidates"]
        registry_sources[district_number] = {
            "official_url": observation.get("url")
            or observation.get("requested_url"),
            "final_url": observation.get("final_url"),
            "sha256": observation["sha256"],
            "retrieved_at": observation.get("retrieved_at"),
            "provenance": observation.get("provenance"),
        }
    winner_candidate_by_district: dict[int, str] = {}
    for district_number, winner_name in sorted(official_winners.items()):
        matched = [
            item
            for item in registry_candidates.get(district_number, [])
            if _normalized_text(item["full_name"]) == winner_name
            and item["registration_status"].casefold() == "зарегистрирован"
        ]
        if len(matched) != 1:
            winner_registry_errors.append(
                {
                    "district_number": district_number,
                    "full_name": winner_name,
                    "matched_candidate_vibids": [
                        item["candidate_vibid"] for item in matched
                    ],
                    "error": "immutable-winner-not-matched-to-one-registered-candidate",
                }
            )
            continue
        winner_candidate_by_district[district_number] = str(
            matched[0]["candidate_vibid"]
        )
    for district_number, candidates in registry_candidates.items():
        winner_vibid = winner_candidate_by_district.get(district_number)
        for candidate in candidates:
            candidate["is_elected"] = candidate["candidate_vibid"] == winner_vibid
    registered_by_name: dict[tuple[int, str], str] = {}
    candidate_identity_errors: list[dict[str, Any]] = []
    for district_number, candidates in sorted(registry_candidates.items()):
        for candidate in candidates:
            if candidate["registration_status"].casefold() != "зарегистрирован":
                continue
            key = (district_number, _normalized_text(candidate["full_name"]))
            previous = registered_by_name.get(key)
            if previous and previous != candidate["candidate_vibid"]:
                candidate_identity_errors.append(
                    {
                        "district_number": district_number,
                        "full_name": candidate["full_name"],
                        "candidate_vibids": sorted(
                            {previous, candidate["candidate_vibid"]}
                        ),
                        "error": "ambiguous-registered-candidate-name",
                    }
                )
            registered_by_name[key] = candidate["candidate_vibid"]

    def candidate_keyed_votes(
        district_number: int, votes: dict[str, int], context: dict[str, Any]
    ) -> dict[str, int]:
        keyed: dict[str, int] = {}
        for full_name, count in votes.items():
            candidate_vibid = registered_by_name.get(
                (district_number, _normalized_text(full_name))
            )
            if not candidate_vibid:
                candidate_identity_errors.append(
                    {
                        **context,
                        "district_number": district_number,
                        "full_name": full_name,
                        "error": "unmatched-result-candidate",
                    }
                )
                continue
            keyed[candidate_vibid] = int(count)
        return keyed

    relation_by_uik = {str(item["uik_tvd"]): item for item in relations}
    region_by_tik = {
        str(item["tik_tvd"]): str(item.get("region", "")) for item in relations
    }
    tik_protocols: defaultdict[str, dict[str, Any]] = defaultdict(dict)
    for observation in observations:
        if observation.get("class") == "election-winners-docx":
            continue
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
            if kind == "candidate":
                relation = relation_by_uik[uik_tvd]
                direct[uik_tvd][kind]["votes"] = candidate_keyed_votes(
                    int(relation["district_number"]),
                    direct[uik_tvd][kind]["votes"],
                    {"uik_tvd": uik_tvd, "report_type": report_type},
                )
            continue
        tik_tvd = str(observation["entity_id"])
        kind = "party" if report_type == 233 else "candidate"
        records = transpose_table(
            {"rows": rows}, indexed_uiks(tree, tik_tvd), option_kind=kind
        )
        for record in records:
            item = paired[str(record["uik_tvd"])]
            relation = relation_by_uik[str(record["uik_tvd"])]
            item.update(
                {
                    "uik_number": record["uik_number"],
                    "uik_tvd": str(record["uik_tvd"]),
                    "tik_tvd": tik_tvd,
                    "tik_name": tik_names.get(tik_tvd, ""),
                    "region": str(relation.get("region", "")),
                    "region_code": str(
                        relation.get("region_code") or relation.get("region") or ""
                    ),
                    "region_tvd": str(relation.get("region_tvd") or ""),
                    "region_name": str(relation.get("region_name") or ""),
                    "district_number": relation.get("district_number"),
                    "oik_tvd": str(relation.get("oik_tvd") or ""),
                    "oik_name": str(relation.get("oik_name") or ""),
                    f"{kind}_accounting": record["accounting"],
                    f"{kind}_votes": record[f"{kind}_votes"],
                }
            )
            if kind == "candidate" and relation.get("district_number") is not None:
                item[f"{kind}_votes"] = candidate_keyed_votes(
                    int(relation["district_number"]),
                    item[f"{kind}_votes"],
                    {
                        "uik_tvd": str(record["uik_tvd"]),
                        "tik_tvd": tik_tvd,
                        "report_type": report_type,
                    },
                )
        aggregate = _aggregate(rows)
        tik_relation = relation_by_uik[str(records[0]["uik_tvd"])] if records else {}
        if kind == "candidate" and tik_relation.get("district_number") is not None:
            aggregate["votes"] = candidate_keyed_votes(
                int(tik_relation["district_number"]),
                aggregate["votes"],
                {"tik_tvd": tik_tvd, "report_type": report_type},
            )
        tik_protocols[tik_tvd][kind] = {
            **aggregate,
            "uik_tvds": [str(record["uik_tvd"]) for record in records],
            "uik_count": len(records),
            "district_number": tik_relation.get("district_number"),
            "oik_tvd": tik_relation.get("oik_tvd"),
        }
        normalized = [
            {
                "accounting": record["accounting"],
                "votes": paired[str(record["uik_tvd"])][f"{kind}_votes"],
            }
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
    available = [record for _, record in sorted(paired.items())]
    incomplete = [
        {"uik_tvd": key, "missing": sorted(required - set(record))}
        for key, record in sorted(paired.items())
        if not required.issubset(record)
    ]
    missing_hierarchy_uiks = [
        {**relation, "cause": "absent-from-tik-result-columns"}
        for relation in relations
        if str(relation["uik_tvd"]) not in paired
    ]
    district_numbers = [
        int(item["district_number"])
        for item in district_catalog_seed
        if item.get("district_number") is not None
    ]
    oik_tvds = [
        str(item["oik_tvd"])
        for item in district_catalog_seed
        if item.get("oik_tvd")
    ]
    relation_assignment_errors = [
        {
            "uik_tvd": item.get("uik_tvd"),
            "tik_tvd": item.get("tik_tvd"),
            "error": "missing-region-or-district-assignment",
        }
        for item in relations
        if not all(
            item.get(key) is not None and str(item.get(key)) != ""
            for key in (
                "region_code",
                "region_tvd",
                "region_name",
                "district_number",
                "oik_tvd",
                "oik_name",
                "tik_tvd",
                "tik_name",
                "uik_tvd",
            )
        )
    ]
    tik_districts: defaultdict[str, set[int]] = defaultdict(set)
    for item in relations:
        if item.get("district_number") is not None:
            tik_districts[str(item["tik_tvd"])].add(int(item["district_number"]))
    relation_assignment_errors.extend(
        {
            "tik_tvd": tik_tvd,
            "district_numbers": sorted(numbers),
            "error": "tik-not-assigned-to-exactly-one-district",
        }
        for tik_tvd, numbers in tik_districts.items()
        if len(numbers) != 1
    )
    district_vote_totals: defaultdict[int, defaultdict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )
    for tik_tvd, values in tik_protocols.items():
        candidate = values.get("candidate")
        numbers = tik_districts.get(tik_tvd, set())
        if not candidate or len(numbers) != 1:
            continue
        district_number = next(iter(numbers))
        for candidate_vibid, count in candidate["votes"].items():
            district_vote_totals[district_number][candidate_vibid] += int(count)
    elected_errors: list[dict[str, Any]] = []
    winner_errors: list[dict[str, Any]] = []
    district_catalog: list[dict[str, Any]] = []
    for district_number, seed in sorted(district_by_number.items()):
        candidates = registry_candidates.get(district_number, [])
        elected = [item for item in candidates if item["is_elected"]]
        if len(elected) != 1:
            elected_errors.append(
                {
                    "district_number": district_number,
                    "elected_candidate_vibids": [
                        item["candidate_vibid"] for item in elected
                    ],
                    "error": "expected-exactly-one-elected-candidate",
                }
            )
            winner_vibid = ""
        else:
            winner_vibid = str(elected[0]["candidate_vibid"])
        totals = district_vote_totals.get(district_number, {})
        if totals:
            high = max(totals.values())
            highest = sorted(
                candidate_vibid
                for candidate_vibid, count in totals.items()
                if count == high
            )
        else:
            highest = []
        if len(highest) != 1 or highest[0] != winner_vibid:
            winner_errors.append(
                {
                    "district_number": district_number,
                    "official_winner_candidate_vibid": winner_vibid,
                    "highest_vote_candidate_vibids": highest,
                    "error": "official-winner-disagrees-with-unique-highest-total",
                }
            )
        if (
            candidates
            and district_number in registry_sources
            and winner_registry_source is not None
        ):
            district_catalog.append(
                {
                    **seed,
                    "winner_candidate_vibid": winner_vibid,
                    "candidates": candidates,
                    "source": registry_sources[district_number],
                    "winner_source": winner_registry_source,
                }
            )
    identity_gate = (
        len(district_numbers) == 225
        and len(set(district_numbers)) == 225
        and set(district_numbers) == set(range(1, 226))
        and len(oik_tvds) == 225
        and len(set(oik_tvds)) == 225
    )
    assignment_gate = (
        not relation_assignment_errors
        and len(relations) == int(hierarchy.get("uiks", len(relations)))
        and len(tik_districts) == int(hierarchy.get("tiks", len(tik_districts)))
    )
    source_gate = (
        not candidate_registry_errors
        and len(registry_sources) == 225
        and all(source.get("sha256") for source in registry_sources.values())
    )
    gates = {
        "exactly_225_unique_district_numbers_and_oik_tvds": identity_gate,
        "every_tik_and_uik_assigned_to_one_district": assignment_gate,
        "every_result_candidate_matched_to_registered_official_candidate": not candidate_identity_errors,
        "exactly_one_official_elected_candidate_per_district": not elected_errors
        and not winner_registry_errors
        and len(winner_candidate_by_district) == 225,
        "official_winner_agrees_with_unique_highest_district_vote_total": not winner_errors
        and len(district_vote_totals) == 225,
        "no_missing_or_unhashed_type_220_source": source_gate,
    }
    gates["passed"] = all(gates.values())
    result = {
        "schema_version": 3,
        "election": "2021-duma",
        "records": available,
        "complete_uik_count": len(complete),
        "sources": [
            {
                "tik_tvd": key,
                "tik_name": tik_names.get(key, ""),
                "region": region_by_tik.get(key, ""),
                **value,
            }
            for key, value in sorted(sources.items())
            if {"party", "candidate"}.issubset(value)
        ],
        "tik_protocols": [
            {"tik_tvd": key, **value}
            for key, value in sorted(tik_protocols.items())
            if {"party", "candidate"}.issubset(value)
        ],
        "reconciliation_failures": failures,
        "duplicate_conflicts": conflicts,
        "incomplete_uiks": incomplete,
        "missing_hierarchy_uiks": missing_hierarchy_uiks,
        "relations": relations,
        "districts": district_catalog,
        "district_gates": gates,
        "district_gate_errors": {
            "candidate_registry": candidate_registry_errors,
            "candidate_identity": candidate_identity_errors,
            "winner_registry": winner_registry_errors,
            "relation_assignment": relation_assignment_errors,
            "elected": elected_errors,
            "winner": winner_errors,
        },
    }
    json_write(args.output, result)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "complete_uiks": len(complete),
                "incomplete_uiks": len(incomplete),
                "missing_hierarchy_uiks": len(missing_hierarchy_uiks),
                "reconciliation_failures": len(failures),
                "duplicate_conflicts": len(conflicts),
                "districts": len(district_catalog),
                "district_gates_passed": gates["passed"],
            },
            indent=2,
        )
    )
    return (
        2
        if incomplete
        or missing_hierarchy_uiks
        or failures
        or conflicts
        or not gates["passed"]
        else 0
    )


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


def recover_gaps(args: argparse.Namespace) -> int:
    """Fetch exact direct protocols for UIKs absent from one or both TIK tables."""
    hierarchy = _load(args.hierarchy)
    protocols = _load(args.protocols)
    target_ids = sorted(
        {
            str(item["uik_tvd"])
            for item in protocols.get("missing_hierarchy_uiks", [])
        }
        | {
            str(item["uik_tvd"])
            for item in protocols.get("incomplete_uiks", [])
        }
    )
    by_id = {str(item["node_id"]): item for item in hierarchy["nodes"]}
    relations = {
        str(item["uik_tvd"]): item
        for item in hierarchy.get(
            "uik_to_tik",
            hierarchy_summary(hierarchy["nodes"], include_oik=True)["uik_to_tik"],
        )
    }
    store, fetcher = _settings(args)

    def recover(uik_tvd: str) -> list[dict[str, Any]]:
        node = by_id[uik_tvd]
        relation = relations[uik_tvd]
        navigation = fetcher.fetch(str(node["url"]), refresh=args.refresh)
        if "body_path" not in navigation:
            return [
                {
                    "region": relation["region"],
                    "tik_tvd": relation["tik_tvd"],
                    "entity_id": uik_tvd,
                    **navigation,
                }
            ]
        links = extract_report_links(
            _body(store, navigation), str(navigation.get("final_url", node["url"]))
        )
        observations = []
        for link in links:
            report_type = int(link["report_type"])
            if report_type not in (242, 463):
                continue
            record = fetcher.fetch(str(link["url"]), refresh=args.refresh)
            payload = _body(store, record) if record.get("body_path") else b""
            observations.append(
                {
                    "region": relation["region"],
                    "tik_tvd": relation["tik_tvd"],
                    "entity_id": uik_tvd,
                    "report_type": report_type,
                    "url": link["url"],
                    **record,
                    **classify_result(payload, report_type),
                }
            )
        return observations

    observations: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = [pool.submit(recover, uik_tvd) for uik_tvd in target_ids]
        for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
            observations.extend(future.result())
            print(
                json.dumps(
                    {
                        "completed_uiks": index,
                        "total_uiks": len(target_ids),
                        "direct_observations": len(observations),
                    }
                ),
                flush=True,
            )
    observations.sort(key=lambda item: str(item.get("url", item.get("requested_url"))))
    result = {
        "schema_version": 1,
        "purpose": "recover-hierarchy-result-gaps",
        "target_uiks": target_ids,
        "observations": observations,
    }
    json_write(args.report, result)
    valid = sum(
        bool(item.get("valid_result") and item.get("kind_matches_requested_type"))
        for item in observations
    )
    complete_observation_set = len(observations) == 2 * len(target_ids) and all(
        item.get("status") == 200 for item in observations
    )
    print(
        json.dumps(
            {
                "report": str(args.report),
                "target_uiks": len(target_ids),
                "observations": len(observations),
                "valid_direct_protocols": valid,
                "official_error_documents": sum(
                    bool(item.get("error_signals")) for item in observations
                ),
            },
            indent=2,
        )
    )
    return 0 if complete_observation_set else 2


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
    command.add_argument("--request-class", action="append")
    command.add_argument("--progress-every", type=int, default=100)
    command.add_argument("--report", type=Path, default=DEFAULT_REPORTS / "crawl.json")
    command = sub.add_parser("validate")
    command.set_defaults(func=validate)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--crawl-report", type=Path, required=True)
    command.add_argument("--additional-crawl-report", type=Path, action="append")
    command.add_argument("--protocols", type=Path)
    command.add_argument(
        "--output", type=Path, default=DEFAULT_REPORTS / "coverage.json"
    )
    command = sub.add_parser("build")
    command.set_defaults(func=build)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--crawl-report", type=Path, required=True)
    command.add_argument("--additional-crawl-report", type=Path, action="append")
    command.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    command.add_argument("--tik", action="append")
    command.add_argument(
        "--output", type=Path, default=DEFAULT_REPORTS / "protocols.json"
    )
    command = sub.add_parser("probe", parents=[common])
    command.set_defaults(func=probe)
    command.add_argument("--spec", type=Path, required=True)
    command.add_argument("--report", type=Path, default=DEFAULT_REPORTS / "probe.json")
    command = sub.add_parser("recover-gaps", parents=[common])
    command.set_defaults(func=recover_gaps)
    command.add_argument("--hierarchy", type=Path, required=True)
    command.add_argument("--protocols", type=Path, required=True)
    command.add_argument(
        "--report", type=Path, default=DEFAULT_REPORTS / "direct-recovery.json"
    )
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
