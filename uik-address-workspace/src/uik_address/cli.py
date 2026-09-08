"""Command-line orchestration for the 2026 UIK address dataset."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Iterable, Sequence
from pathlib import Path

from .assemble import assemble_rows, write_coverage, write_csv, write_public_csv
from .backbone import coverage_summary, extract_backbone, write_backbone_jsonl
from .cec import crawl_cec_contacts
from .gaps import write_gaps, write_subject_coverage
from .io import read_jsonl
from .models import BackboneRow, CommissionContact
from .moscow import crawl_moscow_contacts
from .regional import (
    load_catalog,
    reparse_cached_regions,
    run_regional_crawl,
    run_supplemental_crawl,
)

WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = WORKSPACE_ROOT.parent
DEFAULT_DATA_ROOT = REPOSITORY_ROOT / "proper-data" / "2026"
DEFAULT_WORK_ROOT = WORKSPACE_ROOT / "work"
DEFAULT_SUPPLEMENTAL_CATALOG = WORKSPACE_ROOT / "official-precinct-sources.json"


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _read_json_if_present(path: Path) -> object | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _backbone(data_root: Path, work_root: Path) -> tuple[list[BackboneRow], dict[str, object]]:
    rows = extract_backbone(data_root / "uiks")
    summary = coverage_summary(rows)
    write_backbone_jsonl(work_root / "backbone.jsonl", rows)
    _write_json(work_root / "backbone-summary.json", summary)
    return rows, summary


def _load_backbone(path: Path) -> list[BackboneRow]:
    return [BackboneRow.from_dict(value) for value in read_jsonl(path)]


def _load_contacts(paths: Iterable[Path]) -> list[CommissionContact]:
    contacts: list[CommissionContact] = []
    for path in paths:
        if path.is_file():
            contacts.extend(CommissionContact.from_dict(value) for value in read_jsonl(path))
    return contacts


def _assemble(
    backbone: Iterable[BackboneRow], contacts: Iterable[CommissionContact], work_root: Path
) -> dict[str, object]:
    output_rows, coverage = assemble_rows(backbone, contacts)
    write_csv(work_root / "uik-addresses-2026.csv", output_rows)
    write_public_csv(work_root / "uik-addresses-2026-public.csv", output_rows)
    write_gaps(work_root / "gaps.csv", output_rows)
    write_subject_coverage(work_root / "subject-coverage.csv", output_rows)
    write_coverage(work_root / "coverage.json", coverage)
    return coverage


def _proxy(args: argparse.Namespace) -> str | None:
    return args.proxy_url or os.environ.get("PROPER_DATA_PROXY_URL") or None


def _add_common_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)


def _add_network(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--proxy-url", help="HTTP(S) proxy; defaults to PROPER_DATA_PROXY_URL")
    parser.add_argument(
        "--timeout", type=float, default=30.0, help="per-request timeout in seconds"
    )
    parser.add_argument("--refresh", action="store_true", help="ignore verified response caches")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="uik-address",
        description="Crawl and assemble evidence-backed 2026 UIK/TIK contacts.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    backbone = subparsers.add_parser("backbone", help="extract the Duma-2026 UIK/TIK backbone")
    _add_common_paths(backbone)

    cec = subparsers.add_parser("crawl-cec", help="crawl the current CEC commission gateway")
    _add_common_paths(cec)
    _add_network(cec)
    cec.add_argument("--unfiltered", action="store_true", help="also crawl the unfiltered index")
    cec.add_argument(
        "--subject-code",
        action="append",
        help="scope searches to a subject and fill absent API subject codes; repeatable",
    )
    cec.add_argument(
        "--all-backbone-subjects",
        action="store_true",
        help="scope searches to every subject present in work/backbone.jsonl",
    )
    cec.add_argument("--no-report-42", action="store_true")
    cec.add_argument("--page-size", type=int, default=1_000)

    regional = subparsers.add_parser("crawl-regional", help="crawl official regional sites")
    _add_common_paths(regional)
    _add_network(regional)
    regional.add_argument("--max-pages-per-region", type=int, default=30)
    regional.add_argument("--concurrency", type=int, default=6)
    regional.add_argument(
        "--region-code", action="append", help="crawl only this region; repeatable"
    )
    regional.add_argument(
        "--cache-only", action="store_true", help="rebuild outputs without network requests"
    )

    supplemental = subparsers.add_parser(
        "crawl-supplemental", help="crawl curated current precinct documents"
    )
    _add_common_paths(supplemental)
    _add_network(supplemental)
    supplemental.add_argument("--catalog", type=Path, default=DEFAULT_SUPPLEMENTAL_CATALOG)
    supplemental.add_argument("--max-pages-per-region", type=int)
    supplemental.add_argument("--concurrency", type=int, default=6)
    supplemental.add_argument(
        "--region-code", action="append", help="crawl only this region; repeatable"
    )
    supplemental.add_argument(
        "--cache-only", action="store_true", help="rebuild outputs without network requests"
    )

    moscow = subparsers.add_parser(
        "crawl-moscow", help="look up 2026 Moscow polling places by exact UIK number"
    )
    _add_common_paths(moscow)
    _add_network(moscow)
    moscow.add_argument("--concurrency", type=int, default=4)
    moscow.add_argument(
        "--request-limit",
        type=int,
        help="maximum new requests this run (for bounded probes and resumable batches)",
    )

    reparse = subparsers.add_parser(
        "reparse-regional", help="reparse preserved regional artifacts without network requests"
    )
    _add_common_paths(reparse)
    reparse.add_argument(
        "--region-code", action="append", help="reparse only this region; repeatable"
    )

    assemble = subparsers.add_parser("assemble", help="join a backbone with contact JSONL files")
    _add_common_paths(assemble)
    assemble.add_argument(
        "--contacts",
        type=Path,
        action="append",
        help="contact JSONL; repeatable (defaults to CEC, regional, and Moscow outputs)",
    )

    run = subparsers.add_parser("run", help="run backbone, all crawlers, and assembly")
    _add_common_paths(run)
    _add_network(run)
    run.add_argument("--max-pages-per-region", type=int, default=30)
    run.add_argument("--concurrency", type=int, default=6)
    run.add_argument("--region-code", action="append", help="crawl only this region; repeatable")
    run.add_argument("--page-size", type=int, default=1_000)
    run.add_argument("--no-report-42", action="store_true")
    run.add_argument("--skip-cec", action="store_true")
    run.add_argument("--skip-regional", action="store_true")
    run.add_argument("--skip-supplemental", action="store_true")
    run.add_argument("--skip-moscow", action="store_true")
    return parser


def _print_result(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _compact_crawl_summary(kind: str, summary: object) -> object:
    if not isinstance(summary, dict):
        return summary
    if kind == "cec":
        search = summary.get("search")
        scopes = search if isinstance(search, dict) else {}
        return {
            "complete": summary.get("complete"),
            "contacts": summary.get("contacts"),
            "enrichment": summary.get("enrichment"),
            "errors": summary.get("errors"),
            "search_scopes": len(scopes),
            "search_rows": sum(
                int(value.get("rows") or 0) for value in scopes.values() if isinstance(value, dict)
            ),
        }
    if kind == "regional":
        return {key: value for key, value in summary.items() if key != "regionCoverage"}
    if kind == "moscow":
        return summary
    return summary


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_root = args.data_root.resolve()
    work_root = args.work_root.resolve()

    if args.command == "backbone":
        _, summary = _backbone(data_root, work_root)
        _print_result(summary)
        return 0

    if args.command == "crawl-cec":
        subject_codes = args.subject_code
        if args.all_backbone_subjects:
            subject_codes = sorted(
                {row.subject_code for row in _load_backbone(work_root / "backbone.jsonl")},
                key=lambda code: (0, int(code)) if code.isdigit() else (1, code),
            )
        result = crawl_cec_contacts(
            work_root / "cec",
            proxy_url=_proxy(args),
            timeout=args.timeout,
            refresh=args.refresh,
            include_report42=not args.no_report_42,
            include_unfiltered=args.unfiltered,
            subject_codes=subject_codes,
            page_size=args.page_size,
        )
        _print_result(_compact_crawl_summary("cec", result.summary))
        return 0

    if args.command == "crawl-regional":
        summary = run_regional_crawl(
            data_root / "regional-declaration-sources.json",
            work_root / "regional",
            proxy_url=_proxy(args),
            timeout=args.timeout,
            max_pages_per_region=args.max_pages_per_region,
            concurrency=args.concurrency,
            refresh=args.refresh,
            cache_only=args.cache_only,
            region_codes=args.region_code,
        )
        _print_result(_compact_crawl_summary("regional", summary))
        return 0

    if args.command == "crawl-supplemental":
        summary = run_supplemental_crawl(
            args.catalog.resolve(),
            work_root / "supplemental",
            proxy_url=_proxy(args),
            timeout=args.timeout,
            max_pages_per_region=args.max_pages_per_region,
            concurrency=args.concurrency,
            refresh=args.refresh,
            cache_only=args.cache_only,
            region_codes=args.region_code,
        )
        _print_result(_compact_crawl_summary("regional", summary))
        return 0

    if args.command == "reparse-regional":
        summary = reparse_cached_regions(
            load_catalog(data_root / "regional-declaration-sources.json"),
            work_root / "regional",
            region_codes=args.region_code,
        )
        _print_result(_compact_crawl_summary("regional", summary))
        return 0

    if args.command == "crawl-moscow":
        summary = crawl_moscow_contacts(
            _load_backbone(work_root / "backbone.jsonl"),
            work_root / "moscow",
            proxy_url=_proxy(args),
            timeout=args.timeout,
            concurrency=args.concurrency,
            refresh=args.refresh,
            request_limit=args.request_limit,
        )
        _print_result(summary)
        return 0

    if args.command == "assemble":
        contact_paths = args.contacts or [
            work_root / "cec" / "contacts.jsonl",
            work_root / "regional" / "contacts.jsonl",
            work_root / "supplemental" / "contacts.jsonl",
            work_root / "moscow" / "contacts.jsonl",
        ]
        coverage = _assemble(
            _load_backbone(work_root / "backbone.jsonl"),
            _load_contacts(contact_paths),
            work_root,
        )
        crawls = {
            kind: summary
            for kind, path in (
                ("cec", work_root / "cec" / "summary.json"),
                ("regional", work_root / "regional" / "summary.json"),
                ("supplemental", work_root / "supplemental" / "summary.json"),
                ("moscow", work_root / "moscow" / "summary.json"),
            )
            if (summary := _read_json_if_present(path)) is not None
        }
        _write_json(
            work_root / "run-summary.json",
            {
                "backbone": _read_json_if_present(work_root / "backbone-summary.json"),
                "crawls": crawls,
                "coverage": coverage,
            },
        )
        _print_result(coverage)
        return 0

    backbone_rows, backbone_summary = _backbone(data_root, work_root)
    crawl_summaries: dict[str, object] = {}
    if not args.skip_cec:
        cec_result = crawl_cec_contacts(
            work_root / "cec",
            proxy_url=_proxy(args),
            timeout=args.timeout,
            refresh=args.refresh,
            include_report42=not args.no_report_42,
            include_unfiltered=True,
            subject_codes=sorted(
                {row.subject_code for row in backbone_rows},
                key=lambda code: (0, int(code)) if code.isdigit() else (1, code),
            ),
            page_size=args.page_size,
        )
        crawl_summaries["cec"] = cec_result.summary
    if not args.skip_regional:
        crawl_summaries["regional"] = run_regional_crawl(
            data_root / "regional-declaration-sources.json",
            work_root / "regional",
            proxy_url=_proxy(args),
            timeout=args.timeout,
            max_pages_per_region=args.max_pages_per_region,
            concurrency=args.concurrency,
            refresh=args.refresh,
            region_codes=args.region_code,
        )
    supplemental_codes = {region.code for region in load_catalog(DEFAULT_SUPPLEMENTAL_CATALOG)}
    selected_supplemental_codes = sorted(
        supplemental_codes
        & {str(code).lstrip("0") or "0" for code in (args.region_code or supplemental_codes)}
    )
    if not args.skip_supplemental and selected_supplemental_codes:
        crawl_summaries["supplemental"] = run_supplemental_crawl(
            DEFAULT_SUPPLEMENTAL_CATALOG,
            work_root / "supplemental",
            proxy_url=_proxy(args),
            timeout=args.timeout,
            max_pages_per_region=args.max_pages_per_region,
            concurrency=args.concurrency,
            refresh=args.refresh,
            region_codes=selected_supplemental_codes if args.region_code else None,
        )
    if not args.skip_moscow:
        crawl_summaries["moscow"] = crawl_moscow_contacts(
            backbone_rows,
            work_root / "moscow",
            proxy_url=_proxy(args),
            timeout=args.timeout,
            concurrency=min(args.concurrency, 8),
            refresh=args.refresh,
        )
    contacts = _load_contacts(
        [
            work_root / "cec" / "contacts.jsonl",
            work_root / "regional" / "contacts.jsonl",
            work_root / "supplemental" / "contacts.jsonl",
            work_root / "moscow" / "contacts.jsonl",
        ]
    )
    coverage = _assemble(backbone_rows, contacts, work_root)
    result = {"backbone": backbone_summary, "crawls": crawl_summaries, "coverage": coverage}
    _write_json(work_root / "run-summary.json", result)
    _print_result(
        {
            "backbone": backbone_summary,
            "crawls": {
                kind: _compact_crawl_summary(kind, summary)
                for kind, summary in crawl_summaries.items()
            },
            "coverage": coverage,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
