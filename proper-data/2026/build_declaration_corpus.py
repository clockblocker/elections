"""Build one metadata-only corpus index for 2026 candidate disclosures.

The builder joins the central report-77 files, consolidated federal party-list
files, and every regional crawler index.  It deliberately references the
downloaded documents in place: no PDF, spreadsheet, or archive is copied.

Coverage in this file is evidence, not an estimate of represented candidates.
In particular, a consolidated PDF can describe hundreds of people, while an
individual report-77 file normally describes one.  District numbers are only
reported when an official regional page title or URL labels the number as an
electoral district.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import urllib.parse
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATASET_ROOT = Path(__file__).resolve().parent
DEFAULT_REPORT_77_INDEX = DATASET_ROOT / "declarations" / "index.json"
DEFAULT_PARTY_LIST_INDEX = DATASET_ROOT / "declarations" / "party-lists" / "index.json"
DEFAULT_REGIONS_DIR = DATASET_ROOT / "declarations" / "regions"
DEFAULT_CATALOG = DATASET_ROOT / "regional-declaration-sources.json"
DEFAULT_OUTPUT = DATASET_ROOT / "declarations" / "corpus.json"

CYRILLIC_DISTRICT_RE = re.compile(
    r"(?:одномандатн\w*\s+избирательн\w*\s+округ\w*|"
    r"избирательн\w*\s+округ\w*|округ\w*)"
    r"\s*(?:№|n(?:o|umber)?\.?|номер)\s*(\d{1,3})(?!\d)",
    re.IGNORECASE,
)
URL_DISTRICT_RE = re.compile(
    r"(?:^|[/_.-])(?:district|okrug(?:a|u|e|om)?|oik)(?:[/_.=-]|%2f|%3d)+"
    r"(?:number|num)?(?:[/_.=-]|%2f|%3d)*(\d{1,3})(?=$|[/_.?&#-])",
    re.IGNORECASE,
)
CYRILLIC_URL_DISTRICT_RE = re.compile(
    r"(?:^|[/_.-])округ\w*(?:[/_.=-])+(\d{1,3})(?=$|[/_.?&#-])",
    re.IGNORECASE,
)
DISTRICT_QUERY_KEYS = frozenset(
    {
        "district",
        "districtid",
        "districtnum",
        "districtnumber",
        "okrug",
        "okrugnum",
        "oik",
        "округ",
    }
)
UNRESOLVED_STATUS_RE = re.compile(
    r"(?:unresolved|unprobed|unknown|pending|blocked|fail|error|missing|"
    r"not[ _-]?found|todo|partial|unsupported|недоступ|не\s+найден|не\s+провер)",
    re.IGNORECASE,
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"{path} must contain a JSON object")
    return value


def _records(value: dict[str, Any], key: str, path: Path) -> list[dict[str, Any]]:
    records = value.get(key)
    if not isinstance(records, list):
        raise TypeError(f"{path}: {key} must be an array")
    if not all(isinstance(record, dict) for record in records):
        raise TypeError(f"{path}: every {key} entry must be an object")
    return records


def _portable_path(path: Path, output: Path) -> str:
    """Return a path resolved from the corpus file's directory."""
    return Path(os.path.relpath(path.resolve(), output.parent.resolve())).as_posix()


def _source_index(path: Path, output: Path) -> str:
    return _portable_path(path, output)


def _document_path(index_path: Path, raw_path: Any, output: Path) -> str | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    path = Path(raw_path)
    if not path.is_absolute():
        path = index_path.parent / path
    return _portable_path(path, output)


def _strings(values: Iterable[Any]) -> list[str]:
    return list(
        dict.fromkeys(value for value in values if isinstance(value, str) and value)
    )


def _source_urls(*values: Any) -> list[str]:
    candidates: list[Any] = []
    for value in values:
        if isinstance(value, dict):
            candidates.extend(
                value.get(key)
                for key in ("url", "officialUrl", "finalUrl", "requestedUrl")
            )
        elif isinstance(value, (list, tuple)):
            candidates.extend(value)
        else:
            candidates.append(value)
    return _strings(candidates)


def _identity(
    kind: str,
    native: dict[str, Any],
    raw_path: Any,
    urls: list[str],
    region_code: Any,
) -> str:
    payload = json.dumps(
        {
            "kind": kind,
            "native": native,
            "path": str(raw_path) if raw_path is not None else None,
            "urls": urls,
            "regionCode": str(region_code) if region_code is not None else None,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"document-{hashlib.sha256(payload).hexdigest()[:24]}"


def _normalized_document(
    *,
    kind: str,
    index_path: Path,
    output: Path,
    category: str,
    raw_path: Any,
    sha256: Any,
    urls: list[str],
    title: Any = None,
    region_code: Any = None,
    provenance: Any = None,
    retrieved_at: Any = None,
    byte_length: Any = None,
    media_type: Any = None,
    native: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    native = {key: value for key, value in (native or {}).items() if value is not None}
    path = _document_path(index_path, raw_path, output)
    result: dict[str, Any] = {
        "documentId": _identity(kind, native, raw_path, urls, region_code),
        "source": {
            "kind": kind,
            "indexPath": _source_index(index_path, output),
            "url": urls[0] if urls else None,
            "provenance": provenance,
            "retrievedAt": retrieved_at,
        },
        "category": category,
        "title": title if isinstance(title, str) and title else None,
        "regionCode": str(region_code) if region_code is not None else None,
        "path": path,
        "sha256": sha256 if isinstance(sha256, str) and sha256 else None,
        "byteLength": byte_length if isinstance(byte_length, int) else None,
        "mediaType": media_type if isinstance(media_type, str) else None,
        "urls": urls,
        "native": native,
    }
    if extra:
        result.update(extra)
    return result


def _report_77_documents(
    index: dict[str, Any], index_path: Path, output: Path
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in _records(index, "declarations", index_path):
        document = row.get("document") if isinstance(row.get("document"), dict) else {}
        archive = row.get("archive") if isinstance(row.get("archive"), dict) else {}
        source = (
            archive.get("source") if isinstance(archive.get("source"), dict) else {}
        )
        urls = _source_urls(source)
        result.append(
            _normalized_document(
                kind="cik_report_77",
                index_path=index_path,
                output=output,
                category="income_property",
                raw_path=document.get("path"),
                sha256=document.get("sha256"),
                urls=urls,
                title=row.get("fileName"),
                region_code=row.get("subjectRf"),
                provenance=source.get("provenance"),
                retrieved_at=source.get("retrievedAt"),
                byte_length=document.get("byteLength"),
                native={
                    "reportId": row.get("reportId"),
                    "financialReportId": row.get("financialReportId"),
                    "reportType": row.get("reportType"),
                    "electionId": row.get("electionId"),
                    "uploadTime": row.get("uploadTime"),
                },
                extra={"archive": archive or None},
            )
        )
    return result


def _party_list_documents(
    index: dict[str, Any], index_path: Path, output: Path
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in _records(index, "documents", index_path):
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        urls = _source_urls(row.get("url"), source)
        result.append(
            _normalized_document(
                kind="cik_federal_party_lists",
                index_path=index_path,
                output=output,
                category=str(row.get("category") or "unclassified"),
                raw_path=row.get("path"),
                sha256=row.get("sha256"),
                urls=urls,
                title=row.get("title") or row.get("originalFilename"),
                provenance=source.get("provenance"),
                retrieved_at=source.get("retrievedAt"),
                byte_length=row.get("byteLength"),
                media_type=source.get("contentType"),
                native={"partyId": row.get("partyId")},
                extra={
                    "party": row.get("party"),
                    "originalFilename": row.get("originalFilename"),
                },
            )
        )
    return result


def _regional_documents(
    index: dict[str, Any], index_path: Path, output: Path
) -> list[dict[str, Any]]:
    region = index.get("region") if isinstance(index.get("region"), dict) else {}
    code = region.get("code") or index_path.parent.name
    result: list[dict[str, Any]] = []
    for row in _records(index, "documents", index_path):
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        source_pages = row.get("sourcePages")
        if not isinstance(source_pages, list):
            source_pages = []
        urls = _source_urls(row.get("officialUrl"), source)
        result.append(
            _normalized_document(
                kind="regional_commission",
                index_path=index_path,
                output=output,
                category=str(row.get("category") or "unclassified"),
                raw_path=row.get("path"),
                sha256=row.get("sha256"),
                urls=urls,
                title=row.get("title") or row.get("originalFilename"),
                region_code=code,
                provenance=source.get("provenance"),
                retrieved_at=source.get("retrievedAt"),
                byte_length=row.get("byteLength"),
                media_type=row.get("mediaType"),
                native={"regionalDocumentId": row.get("documentId")},
                extra={
                    "originalFilename": row.get("originalFilename"),
                    "sourcePages": source_pages,
                },
            )
        )
    return result


def extract_district_numbers(title: str | None, url: str | None) -> list[int]:
    """Extract only explicitly labelled State Duma district numbers."""
    numbers: set[int] = set()
    for match in CYRILLIC_DISTRICT_RE.finditer(title or ""):
        numbers.add(int(match.group(1)))
    if url:
        decoded = urllib.parse.unquote(url)
        for match in URL_DISTRICT_RE.finditer(decoded):
            numbers.add(int(match.group(1)))
        for match in CYRILLIC_URL_DISTRICT_RE.finditer(decoded):
            numbers.add(int(match.group(1)))
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(decoded).query)
        for key, values in query.items():
            normalized_key = re.sub(r"[\W_]", "", key.casefold())
            if normalized_key not in DISTRICT_QUERY_KEYS:
                continue
            for value in values:
                if re.fullmatch(r"\d{1,3}", value):
                    numbers.add(int(value))
    return sorted(number for number in numbers if 1 <= number <= 225)


def _district_evidence(
    regional_indexes: list[tuple[Path, dict[str, Any]]], output: Path
) -> list[dict[str, Any]]:
    evidence: dict[tuple[int, str, str, str], dict[str, Any]] = {}
    for index_path, index in regional_indexes:
        region = index.get("region") if isinstance(index.get("region"), dict) else {}
        code = str(region.get("code") or index_path.parent.name)
        pages: list[dict[str, Any]] = []
        raw_pages = index.get("pages")
        if isinstance(raw_pages, list):
            pages.extend(page for page in raw_pages if isinstance(page, dict))
        for document in index.get("documents", []):
            if not isinstance(document, dict):
                continue
            source_pages = document.get("sourcePages")
            if isinstance(source_pages, list):
                pages.extend(page for page in source_pages if isinstance(page, dict))
        for page in pages:
            title = page.get("title") if isinstance(page.get("title"), str) else ""
            url = page.get("url") if isinstance(page.get("url"), str) else ""
            for number in extract_district_numbers(title, url):
                key = (number, code, title, url)
                evidence[key] = {
                    "districtNumber": number,
                    "regionCode": code,
                    "pageTitle": title or None,
                    "pageUrl": url or None,
                    "indexPath": _source_index(index_path, output),
                }
    return sorted(
        evidence.values(),
        key=lambda item: (
            item["districtNumber"],
            item["regionCode"],
            item["pageUrl"] or "",
            item["pageTitle"] or "",
        ),
    )


def _is_unresolved_probe_status(value: Any) -> bool:
    if isinstance(value, str):
        return bool(UNRESOLVED_STATUS_RE.search(value))
    if isinstance(value, dict):
        if value.get("resolved") is False or value.get("complete") is False:
            return True
        return any(
            _is_unresolved_probe_status(value.get(key))
            for key in ("status", "state", "result")
            if key in value
        )
    if isinstance(value, list):
        return any(_is_unresolved_probe_status(item) for item in value)
    return False


def _catalog_regions(catalog_path: Path | None) -> list[dict[str, Any]]:
    if catalog_path is None or not catalog_path.is_file():
        return []
    catalog = _read_object(catalog_path)
    if catalog.get("schemaVersion") != 1:
        raise ValueError(f"{catalog_path}: unsupported catalog schemaVersion")
    return _records(catalog, "regions", catalog_path)


def _region_coverage(
    catalog_regions: list[dict[str, Any]],
    regional_indexes: list[tuple[Path, dict[str, Any]]],
    output: Path,
) -> dict[str, Any]:
    catalog_by_code: dict[str, dict[str, Any]] = {}
    for region in catalog_regions:
        code = str(region.get("code", "")).strip()
        if not code:
            raise ValueError("catalog region is missing code")
        if code in catalog_by_code:
            raise ValueError(f"catalog contains duplicate region code {code}")
        catalog_by_code[code] = region
    indexes_by_code: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, index in regional_indexes:
        region = index.get("region") if isinstance(index.get("region"), dict) else {}
        code = str(region.get("code") or path.parent.name)
        indexes_by_code[code] = (path, index)

    statuses: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    for code in sorted(set(catalog_by_code) | set(indexes_by_code)):
        catalog = catalog_by_code.get(code, {})
        indexed = indexes_by_code.get(code)
        reasons: list[str] = []
        if indexed is None:
            reasons.append("no regional crawler index")
        else:
            regional_index = indexed[1]
            if regional_index.get("complete") is not True:
                reasons.append("regional crawler index is incomplete")
            raw_documents = regional_index.get("documents")
            if not isinstance(raw_documents, list):
                raw_documents = []
            document_categories = {
                item.get("category") for item in raw_documents if isinstance(item, dict)
            }
            category_coverage = regional_index.get("categoryCoverage")
            if not isinstance(category_coverage, dict):
                category_coverage = {}
            for category in ("declaration", "inaccuracy"):
                evidence = category_coverage.get(category)
                if not isinstance(evidence, dict):
                    evidence = {}
                evidenced = category in document_categories or any(
                    isinstance(evidence.get(key), int) and evidence[key] > 0
                    for key in (
                        "signalPageCount",
                        "explicitEmptyPageCount",
                        "documentCount",
                    )
                )
                if not evidenced:
                    reasons.append(
                        f"no {category} publication evidence in regional index"
                    )
        if _is_unresolved_probe_status(catalog.get("probeStatus")):
            reasons.append("catalog probe status is unresolved")
        name = catalog.get("name")
        if indexed:
            indexed_region = indexed[1].get("region")
            if not name and isinstance(indexed_region, dict):
                name = indexed_region.get("name")
        status = {
            "code": code,
            "name": name,
            "catalogued": code in catalog_by_code,
            "indexPath": _source_index(indexed[0], output) if indexed else None,
            "indexComplete": indexed[1].get("complete") if indexed else None,
            "documentCounts": indexed[1].get("documentCounts") if indexed else None,
            "categoryCoverage": (
                indexed[1].get("categoryCoverage") if indexed else None
            ),
            "probeStatus": catalog.get("probeStatus"),
            "evidence": catalog.get("evidence"),
            "baseUrl": catalog.get("baseUrl"),
            "seedUrls": catalog.get("seedUrls"),
            "status": "unresolved" if reasons else "index_crawl_complete",
            "unresolvedReasons": reasons,
        }
        statuses.append(status)
        if reasons:
            unresolved.append({"code": code, "name": name, "reasons": reasons})
    return {
        "cataloguedCount": len(catalog_by_code),
        "indexedCount": len(indexes_by_code),
        "indexedCompleteCount": sum(
            index.get("complete") is True for _, index in indexes_by_code.values()
        ),
        "statuses": statuses,
        "unresolvedRegions": unresolved,
    }


def _duplicate_groups(
    documents: list[dict[str, Any]], field: str
) -> list[dict[str, Any]]:
    grouped: defaultdict[str, set[str]] = defaultdict(set)
    for document in documents:
        raw_values = document.get(field)
        values = raw_values if isinstance(raw_values, list) else [raw_values]
        for value in values:
            if isinstance(value, str) and value:
                grouped[value].add(document["documentId"])
    label = "url" if field == "urls" else field
    return [
        {label: value, "documentIds": sorted(document_ids)}
        for value, document_ids in sorted(grouped.items())
        if len(document_ids) > 1
    ]


def build_corpus(
    *,
    report_77_index: Path = DEFAULT_REPORT_77_INDEX,
    party_list_index: Path = DEFAULT_PARTY_LIST_INDEX,
    regions_dir: Path = DEFAULT_REGIONS_DIR,
    catalog_path: Path | None = DEFAULT_CATALOG,
    output: Path = DEFAULT_OUTPUT,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Build and write the unified corpus manifest."""
    source_statuses: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []

    for kind, path, loader in (
        ("cik_report_77", report_77_index, _report_77_documents),
        ("cik_federal_party_lists", party_list_index, _party_list_documents),
    ):
        present = path.is_file()
        source_statuses.append(
            {
                "kind": kind,
                "indexPath": _source_index(path, output),
                "present": present,
            }
        )
        if present:
            documents.extend(loader(_read_object(path), path, output))

    regional_indexes = [
        (path, _read_object(path))
        for path in sorted(regions_dir.glob("*/index.json"))
        if path.is_file()
    ]
    for path, index in regional_indexes:
        documents.extend(_regional_documents(index, path, output))
    source_statuses.append(
        {
            "kind": "regional_commission",
            "indexesRoot": _portable_path(regions_dir, output),
            "present": bool(regional_indexes),
            "indexCount": len(regional_indexes),
        }
    )

    documents.sort(
        key=lambda item: (
            item["source"]["kind"],
            item["regionCode"] or "",
            item["category"],
            item["path"] or "",
            item["documentId"],
        )
    )
    by_source = Counter(document["source"]["kind"] for document in documents)
    by_category = Counter(document["category"] for document in documents)
    by_region = Counter(
        document["regionCode"]
        for document in documents
        if document["regionCode"] is not None
    )
    hash_duplicates = _duplicate_groups(documents, "sha256")
    url_duplicates = _duplicate_groups(documents, "urls")
    district_evidence = _district_evidence(regional_indexes, output)
    district_numbers = sorted({item["districtNumber"] for item in district_evidence})
    catalog_regions = _catalog_regions(catalog_path)

    result = {
        "schemaVersion": 1,
        "dataset": "cik-2026-candidate-declaration-corpus",
        "generatedAt": generated_at or datetime.now(timezone.utc).isoformat(),
        "metadataOnly": True,
        "sourceIndexes": source_statuses,
        "summary": {
            "documentCount": len(documents),
            "bySource": dict(sorted(by_source.items())),
            "byCategory": dict(sorted(by_category.items())),
            "byRegion": dict(sorted(by_region.items())),
            "documentsWithoutRegion": sum(
                document["regionCode"] is None for document in documents
            ),
            "exactDuplicateHashGroups": len(hash_duplicates),
            "exactDuplicateUrlGroups": len(url_duplicates),
        },
        "coverage": {
            "interpretation": (
                "Document counts are not candidate counts. District coverage is "
                "reported only from explicit electoral-district labels in official "
                "regional page titles or URLs. A complete regional crawl only means "
                "that its configured bounded crawl finished without recorded errors; "
                "it does not prove that every candidate was published."
            ),
            "districts": {
                "explicitCount": len(district_numbers),
                "explicitNumbers": district_numbers,
                "evidence": district_evidence,
            },
            "regions": _region_coverage(catalog_regions, regional_indexes, output),
        },
        "duplicates": {
            "bySha256": hash_duplicates,
            "byUrl": url_duplicates,
        },
        "documents": documents,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.part")
    temporary.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(output)
    return result


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Build a metadata-only index over all crawled 2026 declarations."
    )
    result.add_argument("--report-77-index", type=Path, default=DEFAULT_REPORT_77_INDEX)
    result.add_argument(
        "--party-list-index", type=Path, default=DEFAULT_PARTY_LIST_INDEX
    )
    result.add_argument("--regions-dir", type=Path, default=DEFAULT_REGIONS_DIR)
    result.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    result.add_argument("--no-catalog", action="store_true")
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    corpus = build_corpus(
        report_77_index=args.report_77_index,
        party_list_index=args.party_list_index,
        regions_dir=args.regions_dir,
        catalog_path=None if args.no_catalog else args.catalog,
        output=args.output,
    )
    regions = corpus["coverage"]["regions"]
    print(
        f"documents={corpus['summary']['documentCount']}; "
        f"regions={regions['indexedCount']}/{regions['cataloguedCount']}; "
        f"explicit_districts={corpus['coverage']['districts']['explicitCount']}; "
        f"unresolved_regions={len(regions['unresolvedRegions'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
