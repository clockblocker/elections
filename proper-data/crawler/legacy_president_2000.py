from __future__ import annotations

import argparse
import copy
import json
import re
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

try:
    from .common import json_write, sha256_bytes
    from .shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from .transport import FetchConfig, Fetcher, ResponseStore
except ImportError:
    from common import json_write, sha256_bytes
    from shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from transport import FetchConfig, Fetcher, ResponseStore


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RAW_DIR = REPO_ROOT / "data/raw/president-2000-legacy"
DEFAULT_REPORT_DIR = REPO_ROOT / "reports/generated/president-2000-legacy"
DEFAULT_OUTPUT = REPO_ROOT / "proper-data/2000-president/results.json"
OLE_MAGIC = bytes.fromhex("D0CF11E0A1B11AE1")
EXPECTED_REGIONS = 89
EXPECTED_TIKS = 2_736
ACCOUNTING_LINES = tuple(
    [str(number) for number in range(1, 13)]
    + ["12а", "13", "14", "14а", "15", "16", "17"]
)
CHOICE_LINES = tuple(str(number) for number in range(18, 30))


@dataclass(frozen=True)
class Asset:
    key: str
    original_url: str
    archive_url: str
    sha256: str
    byte_length: int
    kind: str


BASE = "http://old.cikrf.ru/banners/vib_arhiv/president/2000/"
ASSETS = (
    Asset(
        "index",
        BASE + "index.html",
        "https://web.archive.org/web/20180218000942id_/"
        "http://old.cikrf.ru:80/banners/vib_arhiv/president/2000/index.html",
        "45add725848dfb1cbf82fb00f096819772475079f45b3d0a1409f5775e716483",
        5_937,
        "html-index",
    ),
    Asset(
        "summary",
        BASE + "files/2000-Svodnaya_CIK.xls",
        "https://web.archive.org/web/20190412174520id_/" + BASE
        + "files/2000-Svodnaya_CIK.xls",
        "a707ee286e7789009e2bcdcc0a8e2e23677080285309edc36921ab44c50c5574",
        56_320,
        "xls-summary",
    ),
    Asset(
        "protocol",
        BASE + "files/2000-Protokol_CIK.doc",
        "https://web.archive.org/web/20190412174521id_/" + BASE
        + "files/2000-Protokol_CIK.doc",
        "2815f790dbaac18ea037bfbb0916342eb17341780af976d5fcf983122a4cb226",
        57_344,
        "doc-protocol",
    ),
    Asset(
        "tik",
        BASE + "files/2000-TIK-usech.xls",
        "https://web.archive.org/web/20190412174522id_/" + BASE
        + "files/2000-TIK-usech.xls",
        "c5e1f46e883926743113ba1adfc8fa7645f3ab276e56a2df85fd4cd90cfeb7f2",
        674_304,
        "xls-tik",
    ),
    Asset(
        "correction",
        BASE + "post_1149_pr_2000.html",
        "https://web.archive.org/web/20190412174522id_/" + BASE
        + "post_1149_pr_2000.html",
        "be895c1b8a1d1ded056cf2fbdeda1eb4889410dfa9799f038e7365a1b989312f",
        28_839,
        "html-correction",
    ),
)
ASSET_BY_KEY = {asset.key: asset for asset in ASSETS}


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


class TextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


@dataclass(frozen=True)
class CorrectionRule:
    scope: str
    line: str
    before: int
    after: int
    section: str


def _rules(
    scope: str, section: str, values: Iterable[tuple[str, int, int]]
) -> list[CorrectionRule]:
    return [CorrectionRule(scope, line, before, after, section) for line, before, after in values]


CORRECTION_RULES = (
    *_rules("national", "national", (
        ("1", 109_372_046, 109_372_043), ("2", 105_304_935, 105_305_901),
        ("4", 30_123_191, 30_124_155), ("5", 71_489_033, 71_489_035),
        ("8", 71_382_878, 71_382_872), ("9", 74_369_773, 74_369_754),
        ("10", 701_003, 701_016), ("12", 127_749, 127_760),
        ("15", 1_018_562, 1_018_568), ("14а", 137_589, 137_320),
        ("17", 61_834, 61_599), ("20", 2_026_513, 2_026_509),
        ("21", 21_928_471, 21_928_468), ("22", 758_966, 758_967),
        ("23", 98_175, 98_177), ("24", 39_740_434, 39_740_467),
        ("25", 319_263, 319_189), ("27", 2_217_361, 2_217_364),
        ("28", 4_351_452, 4_351_450), ("29", 1_414_648, 1_414_673),
    )),
    *_rules("Белгородская область", "belgorod", (
        ("1", 1_146_884, 1_146_887), ("12", 1_522, 1_524),
        ("24", 400_873, 400_946), ("25", 3_015, 2_941),
        ("27", 12_763, 12_764),
    )),
    *_rules("Калининградская область", "kaliningrad", (
        ("1", 737_938, 737_934), ("2", 695_521, 695_527),
        ("4", 199_704, 199_707), ("5", 481_447, 481_450),
        ("8", 480_756, 480_753), ("9", 490_993, 490_976),
        ("10", 4_126, 4_140), ("12", 760, 769), ("15", 6_358, 6_364),
        ("20", 17_958, 17_954), ("21", 116_272, 116_269),
        ("22", 4_267, 4_268), ("23", 676, 678), ("24", 297_843, 297_830),
        ("27", 8_154, 8_156), ("28", 31_017, 31_015),
    )),
    *_rules("Тульская область", "tula", (
        ("4", 412_566, 412_567), ("5", 871_219, 871_218),
        ("8", 870_040, 870_038), ("9", 926_624, 926_622),
        ("24", 448_942, 448_915), ("29", 20_248, 20_273),
    )),
    *_rules("overseas", "uik5", (("14а", 2, 0),)),
    *_rules("overseas", "uik143", (("14а", 1, 0),)),
    *_rules("overseas", "uik203", (("2", 1_000, 1_960), ("4", 295, 1_255))),
    *_rules("overseas", "uik286", (("14а", 1, 0),)),
    *_rules("overseas", "uik289", (("8", 2_251, 2_250), ("10", 13, 12))),
    *_rules("overseas", "uik355", (("1", 112, 110),)),
    *_rules("overseas", "uik372", (("14а", 265, 0), ("17", 235, 0))),
)


SECTION_MARKERS = {
    "national": ("следующие уточненные данные", "3. Внести в Сводную таблицу"),
    "belgorod": ("Протокола Избирательной комиссии Белгородской области:", "Протокола Избирательной комиссии Калининградской области:"),
    "kaliningrad": ("Протокола Избирательной комиссии Калининградской области:", "Протокола Избирательной комиссии Тульской области:"),
    "tula": ("Протокола Избирательной комиссии Тульской области:", "Протокола участковой избирательной комиссии избирательного участка N 5"),
    "uik5": ("избирательного участка N 5,", "избирательного участка N 143,"),
    "uik143": ("избирательного участка N 143,", "избирательного участка N 203,"),
    "uik203": ("избирательного участка N 203,", "избирательного участка N 286,"),
    "uik286": ("избирательного участка N 286,", "избирательного участка N 289,"),
    "uik289": ("избирательного участка N 289,", "избирательного участка N 355,"),
    "uik355": ("избирательного участка N 355,", "избирательного участка N 372,"),
    "uik372": ("избирательного участка N 372,", "4. Предложить"),
}


def _canonical_original(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(
        ("http", parts.netloc.casefold().replace(":80", ""), parts.path, "", "")
    )


def discover_index_assets(payload: bytes) -> list[str]:
    """Return the exact official files linked from the captured CEC index."""
    parser = LinkParser()
    parser.feed(payload.decode("utf-8", errors="replace"))
    joined = {
        _canonical_original(urllib.parse.urljoin(BASE, href)) for href in parser.hrefs
    }
    expected = {_canonical_original(asset.original_url) for asset in ASSETS[1:]}
    missing = sorted(expected - joined)
    if missing:
        raise ValueError(f"official index is missing expected assets: {missing}")
    return sorted(expected)


def validate_asset(asset: Asset, payload: bytes) -> None:
    digest = sha256_bytes(payload)
    if len(payload) != asset.byte_length or digest != asset.sha256:
        raise ValueError(
            f"{asset.key} content identity mismatch: bytes={len(payload)}, sha256={digest}"
        )
    lowered = payload[:512].lower()
    if asset.kind.startswith(("xls-", "doc-")) and not payload.startswith(OLE_MAGIC):
        raise ValueError(f"{asset.key} is not an OLE workbook/document")
    if asset.kind.startswith("html-") and b"<html" not in lowered:
        raise ValueError(f"{asset.key} is not HTML")
    if asset.key == "index":
        discover_index_assets(payload)
    elif asset.key == "correction" and b"1149" not in payload:
        raise ValueError("correction page does not identify resolution 1149")


def _spaced_number(value: int) -> str:
    return r"\s*".join(re.escape(part) for part in f"{value:,}".replace(",", " ").split())


def parse_resolution_corrections(payload: bytes) -> list[dict[str, Any]]:
    """Parse the legally operative old/new pairs from Resolution 106/1149-3."""
    parser = TextParser()
    parser.feed(payload.decode("utf-8"))
    text = _text(" ".join(parser.parts))
    if "От 7 июля 2000 года № 106/1149-3" not in text:
        raise ValueError("correction asset is not CEC Resolution 106/1149-3")
    sections: dict[str, str] = {}
    for key, (start_marker, end_marker) in SECTION_MARKERS.items():
        start = text.find(start_marker)
        end = text.find(end_marker, start + len(start_marker))
        if start < 0 or end < 0:
            raise ValueError(f"resolution section {key} is missing")
        sections[key] = text[start:end]

    parsed = []
    for rule in CORRECTION_RULES:
        pattern = re.compile(
            rf"(?<!\d){_spaced_number(rule.after)}(?!\d)"
            rf"(?:(?!\bвместо\b).){{0,500}}?\bвместо\s+"
            rf"(?<!\d){_spaced_number(rule.before)}(?!\d)",
            re.IGNORECASE,
        )
        if not pattern.search(sections[rule.section]):
            raise ValueError(
                f"resolution does not prove {rule.scope} line {rule.line}: "
                f"{rule.before} -> {rule.after}"
            )
        parsed.append(
            {
                "scope": rule.scope,
                "line": rule.line,
                "before": rule.before,
                "after": rule.after,
                "delta": rule.after - rule.before,
                "evidence_section": rule.section,
            }
        )
    return parsed


def apply_resolution_corrections(
    summary: dict[str, Any], corrections: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    corrected = copy.deepcopy(summary)
    regions = {region["name"]: region for region in corrected["regions"]}
    applied: list[dict[str, Any]] = []
    for change in corrections:
        scope = str(change["scope"])
        if scope == "national":
            target = corrected["national"]
        elif scope == "overseas":
            target = corrected["overseas"]
        else:
            try:
                target = regions[scope]
            except KeyError as error:
                raise ValueError(f"resolution region is absent from workbook: {scope}") from error
        line = str(change["line"])
        family = "accounting" if line in ACCOUNTING_LINES else "votes"
        aggregate_before = target[family][line]
        if scope == "overseas":
            aggregate_after = aggregate_before + int(change["delta"])
        else:
            if aggregate_before != change["before"]:
                raise ValueError(
                    f"resolution/workbook mismatch for {scope} line {line}: "
                    f"{aggregate_before} != {change['before']}"
                )
            aggregate_after = int(change["after"])
        target[family][line] = aggregate_after
        applied.append(
            {
                **change,
                "aggregate_before": aggregate_before,
                "aggregate_after": aggregate_after,
            }
        )
    return corrected, applied


def _integer(value: Any, context: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{context} is not numeric: {value!r}")
    integer = int(value)
    if integer != value or integer < 0:
        raise ValueError(f"{context} is not a non-negative integer: {value!r}")
    return integer


def _line_id(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, int):
        return str(value)
    return re.sub(r"\s+", "", str(value)).casefold()


def _text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def parse_summary_sheet(sheet: Any) -> dict[str, Any]:
    if sheet.nrows < 42 or sheet.ncols < 4:
        raise ValueError("CEC summary workbook has unexpected dimensions")
    row_by_line: dict[str, int] = {}
    for row in range(sheet.nrows):
        line = _line_id(sheet.cell_value(row, 0))
        if line in ACCOUNTING_LINES + CHOICE_LINES:
            row_by_line[line] = row
    expected = set(ACCOUNTING_LINES + CHOICE_LINES)
    if set(row_by_line) != expected:
        raise ValueError(
            f"CEC summary line set mismatch: {sorted(set(row_by_line) ^ expected)}"
        )

    definitions = {
        line: _text(sheet.cell_value(row_by_line[line], 1))
        for line in ACCOUNTING_LINES + CHOICE_LINES
    }
    columns: list[tuple[int, str]] = []
    # Columns 0..3 are the printed line number, description, national total,
    # and the first repeated line-number gutter. Regional data starts at 4.
    for column in range(4, sheet.ncols):
        heading = _text(sheet.cell_value(9, column))
        if heading and heading not in {"Итого"} and not heading.isdigit():
            columns.append((column, heading))
    if len(columns) < 2:
        raise ValueError("CEC summary has no region/overseas columns")

    def totals(column: int, lines: Iterable[str]) -> dict[str, int]:
        return {
            line: _integer(
                sheet.cell_value(row_by_line[line], column),
                f"summary line {line}, column {column}",
            )
            for line in lines
        }

    regions = [
        {
            "name": heading,
            "accounting": totals(column, ACCOUNTING_LINES),
            "votes": totals(column, CHOICE_LINES),
        }
        for column, heading in columns[:-1]
    ]
    overseas_column, overseas_name = columns[-1]
    return {
        "accounting_definitions": {
            line: definitions[line] for line in ACCOUNTING_LINES
        },
        "choice_definitions": {line: definitions[line] for line in CHOICE_LINES},
        "national": {
            "accounting": totals(2, ACCOUNTING_LINES),
            "votes": totals(2, CHOICE_LINES),
        },
        "regions": regions,
        "overseas": {
            "name": overseas_name,
            "accounting": totals(overseas_column, ACCOUNTING_LINES),
            "votes": totals(overseas_column, CHOICE_LINES),
        },
    }


def parse_tik_workbook(book: Any) -> dict[str, Any]:
    if book.nsheets < 2:
        raise ValueError("TIK workbook has no regional sheets")
    legend = book.sheet_by_index(0)
    definitions: dict[str, str] = {}
    for row in range(legend.nrows):
        line = _line_id(legend.cell_value(row, 0))
        if line in CHOICE_LINES:
            definitions[line] = _text(legend.cell_value(row, 1))
    if set(definitions) != set(CHOICE_LINES):
        raise ValueError("TIK workbook legend does not contain vote lines 18..29")

    regions = []
    for region_index in range(1, book.nsheets):
        sheet = book.sheet_by_index(region_index)
        region_name = _text(sheet.cell_value(0, 1))
        if not region_name:
            raise ValueError(f"TIK sheet {region_index} has no region name")
        vote_rows = {
            _line_id(sheet.cell_value(row, 0)): row
            for row in range(sheet.nrows)
            if _line_id(sheet.cell_value(row, 0)) in CHOICE_LINES
        }
        if set(vote_rows) != set(CHOICE_LINES):
            raise ValueError(f"TIK sheet {region_index} has an invalid vote-row set")
        aggregate: dict[str, int] | None = None
        tiks = []
        for column in range(sheet.ncols):
            name = _text(sheet.cell_value(3, column))
            if not name:
                continue
            votes = {
                line: _integer(
                    sheet.cell_value(vote_rows[line], column),
                    f"TIK sheet {region_index}, {name}, line {line}",
                )
                for line in CHOICE_LINES
            }
            if name.casefold() == "итого":
                if aggregate is not None:
                    raise ValueError(f"TIK sheet {region_index} has two totals")
                aggregate = votes
            else:
                tiks.append(
                    {
                        "id": f"region-{region_index:03d}-tik-{len(tiks) + 1:03d}",
                        "name": name,
                        "votes": votes,
                    }
                )
        if aggregate is None or not tiks:
            raise ValueError(f"TIK sheet {region_index} has no total/TIK columns")
        regions.append(
            {
                "workbook_name": region_name,
                "aggregate_votes": aggregate,
                "tiks": tiks,
            }
        )
    return {"choice_definitions": definitions, "regions": regions}


def _open_workbook(payload: bytes) -> Any:
    try:
        import xlrd
    except ImportError as error:
        raise RuntimeError(
            "xlrd 2.x is required; run with `uv run --with 'xlrd>=2,<3' python ...`"
        ) from error
    return xlrd.open_workbook(file_contents=payload, on_demand=True)


def _source(asset: Asset, record: dict[str, Any]) -> dict[str, Any]:
    capture = re.search(r"/web/(\d{14})id_/", asset.archive_url)
    return {
        "official_url": asset.original_url,
        "archive_url": asset.archive_url,
        "archive_capture_timestamp": capture.group(1) if capture else None,
        "provenance": "wayback-official-capture",
        "sha256": record["sha256"],
        "byte_length": record["byte_length"],
    }


def assemble_dataset(
    workbook_summary: dict[str, Any],
    corrected_summary: dict[str, Any],
    tik: dict[str, Any],
    sources: dict[str, Any],
    applied_corrections: list[dict[str, Any]],
) -> dict[str, Any]:
    def delta(
        before: dict[str, dict[str, int]], after: dict[str, dict[str, int]]
    ) -> dict[str, dict[str, int]]:
        return {
            family: {
                line: after[family][line] - before[family][line]
                for line in after[family]
                if after[family][line] != before[family][line]
            }
            for family in ("accounting", "votes")
        }

    if workbook_summary["choice_definitions"] != tik["choice_definitions"]:
        raise ValueError("candidate legend differs between the two official workbooks")
    if len(workbook_summary["regions"]) != len(tik["regions"]):
        raise ValueError("summary and TIK workbooks have different region counts")
    regions = []
    for index, (workbook_region, corrected_region, tik_region) in enumerate(
        zip(
            workbook_summary["regions"],
            corrected_summary["regions"],
            tik["regions"],
            strict=True,
        ),
        start=1,
    ):
        regions.append(
            {
                "id": f"region-{index:03d}",
                "name": corrected_region["name"],
                "tik_workbook_name": tik_region["workbook_name"],
                "accounting": corrected_region["accounting"],
                "votes": corrected_region["votes"],
                "workbook_pre_correction": {
                    "accounting": workbook_region["accounting"],
                    "votes": workbook_region["votes"],
                },
                "correction_delta": delta(workbook_region, corrected_region),
                "tik_reconciliation_version": "workbook_pre_correction",
                "tik_aggregate_votes": tik_region["aggregate_votes"],
                "tiks": tik_region["tiks"],
            }
        )
    return {
        "schema_version": 1,
        "election": {
            "id": "2000-president",
            "name": "Выборы Президента Российской Федерации",
            "date": "2000-03-26",
            "supported_granularity": ["national", "region", "tik"],
            "uik_results_available_in_static_archive": False,
        },
        "result_versions": {
            "published": "resolution-106/1149-3-corrected",
            "workbook_pre_correction": (
                "Values transcribed from the two XLS workbooks before the 7 July "
                "2000 corrections; TIK rows reconcile to this version."
            ),
            "resolution-106/1149-3-corrected": (
                "Legally corrected aggregate values. Corrected TIK detail is not "
                "published by this static archive."
            ),
        },
        "sources": sources,
        "correction": {
            "applied": True,
            "resolution": "106/1149-3",
            "date": "2000-07-07",
            "source_key": "correction",
            "changes": applied_corrections,
            "corrected_region_tik_detail_available": False,
        },
        "accounting_lines": workbook_summary["accounting_definitions"],
        "choices": workbook_summary["choice_definitions"],
        "national": {
            **corrected_summary["national"],
            "workbook_pre_correction": workbook_summary["national"],
            "correction_delta": delta(
                workbook_summary["national"], corrected_summary["national"]
            ),
            "overseas_aggregate": {
                **corrected_summary["overseas"],
                "workbook_pre_correction": workbook_summary["overseas"],
                "correction_delta": delta(
                    workbook_summary["overseas"], corrected_summary["overseas"]
                ),
            },
        },
        "regions": regions,
    }


def validate_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    regions = dataset.get("regions", [])
    choices = tuple(dataset.get("choices", {}).keys())
    if len(regions) != EXPECTED_REGIONS:
        errors.append(f"expected {EXPECTED_REGIONS} regions, found {len(regions)}")
    if choices != CHOICE_LINES:
        errors.append("choice IDs are not the ordered official lines 18..29")
    tik_count = sum(len(region.get("tiks", [])) for region in regions)
    if tik_count != EXPECTED_TIKS:
        errors.append(f"expected {EXPECTED_TIKS} TIKs, found {tik_count}")

    national = dataset.get("national", {})
    overseas = national.get("overseas_aggregate", {})
    national_pre = national.get("workbook_pre_correction", {})
    overseas_pre = overseas.get("workbook_pre_correction", {})
    for line in ACCOUNTING_LINES:
        expected = sum(region["accounting"][line] for region in regions)
        expected += overseas.get("accounting", {}).get(line, 0)
        if national.get("accounting", {}).get(line) != expected:
            errors.append(f"corrected national accounting line {line} does not reconcile")
        expected_pre = sum(
            region["workbook_pre_correction"]["accounting"][line]
            for region in regions
        ) + overseas_pre.get("accounting", {}).get(line, 0)
        if national_pre.get("accounting", {}).get(line) != expected_pre:
            errors.append(f"workbook national accounting line {line} does not reconcile")
    for line in CHOICE_LINES:
        expected = sum(region["votes"][line] for region in regions)
        expected += overseas.get("votes", {}).get(line, 0)
        if national.get("votes", {}).get(line) != expected:
            errors.append(f"corrected national vote line {line} does not reconcile")
        expected_pre = sum(
            region["workbook_pre_correction"]["votes"][line]
            for region in regions
        ) + overseas_pre.get("votes", {}).get(line, 0)
        if national_pre.get("votes", {}).get(line) != expected_pre:
            errors.append(f"workbook national vote line {line} does not reconcile")
        for region in regions:
            tik_sum = sum(tik["votes"][line] for tik in region["tiks"])
            aggregate = region.get("tik_aggregate_votes", {}).get(line)
            if aggregate != tik_sum:
                errors.append(
                    f"{region['id']} TIK aggregate line {line} does not reconcile"
                )
            if region["workbook_pre_correction"]["votes"][line] != aggregate:
                errors.append(
                    f"{region['id']} workbook summary/TIK line {line} does not reconcile"
                )

    accounting_groups = [("national", national), ("overseas", overseas)] + [
        (region["id"], region) for region in regions
    ]
    for label, group in accounting_groups:
        for version, accounting, votes in (
            ("corrected", group.get("accounting", {}), group.get("votes", {})),
            (
                "workbook",
                group.get("workbook_pre_correction", {}).get("accounting", {}),
                group.get("workbook_pre_correction", {}).get("votes", {}),
            ),
        ):
            if not accounting:
                errors.append(f"{label} has no {version} accounting version")
                continue
            identity = accounting.get("7", 0) + accounting.get("8", 0)
            if identity != accounting.get("9", 0) + accounting.get("10", 0):
                errors.append(f"{label} {version} ballot identity fails")
            if sum(votes.values()) != accounting.get("9", 0):
                errors.append(f"{label} {version} valid votes do not reconcile")

    correction = dataset.get("correction", {})
    changes = correction.get("changes", [])
    if not correction.get("applied") or correction.get("resolution") != "106/1149-3":
        errors.append("Resolution 106/1149-3 is not explicitly applied")
    if correction.get("corrected_region_tik_detail_available") is not False:
        errors.append("corrected TIK-detail availability is not explicit")
    if len(changes) != len(CORRECTION_RULES):
        errors.append("correction change set is incomplete")
    expected_deltas: dict[tuple[str, str], int] = {}
    for change in changes:
        key = (str(change.get("scope")), str(change.get("line")))
        expected_deltas[key] = expected_deltas.get(key, 0) + int(change.get("delta", 0))

    versioned_groups = [("national", national), ("overseas", overseas)] + [
        (region["name"], region) for region in regions
    ]
    for scope, group in versioned_groups:
        pre = group.get("workbook_pre_correction", {})
        for family, lines in (("accounting", ACCOUNTING_LINES), ("votes", CHOICE_LINES)):
            for line in lines:
                actual_delta = group.get(family, {}).get(line, 0) - pre.get(family, {}).get(line, 0)
                expected_delta = expected_deltas.get((scope, line), 0)
                if actual_delta != expected_delta:
                    errors.append(
                        f"{scope} line {line} correction delta {actual_delta} != {expected_delta}"
                    )
                recorded_delta = group.get("correction_delta", {}).get(family, {}).get(line, 0)
                if recorded_delta != actual_delta:
                    errors.append(
                        f"{scope} line {line} recorded correction delta is not explicit"
                    )

    for region in regions:
        if region.get("tik_reconciliation_version") != "workbook_pre_correction":
            errors.append(f"{region['id']} TIK reconciliation version is not explicit")

    published = dataset.get("result_versions", {}).get("published")
    if published != "resolution-106/1149-3-corrected":
        errors.append("published result-version semantics are not explicit")

    fixed = {
        "national accounting line 9": (national.get("accounting", {}).get("9"), 74_369_754),
        "national Putin vote": (national.get("votes", {}).get("24"), 39_740_467),
        "national against-all vote": (national.get("votes", {}).get("29"), 1_414_673),
        "national election participants": (
            sum(national.get("accounting", {}).get(line, 0) for line in ("3", "5", "6")),
            75_181_073,
        ),
        "national voting participants": (
            sum(national.get("accounting", {}).get(line, 0) for line in ("7", "8")),
            75_070_770,
        ),
        "pre-correction national accounting line 9": (
            national_pre.get("accounting", {}).get("9"), 74_369_773
        ),
        "pre-correction national Putin vote": (
            national_pre.get("votes", {}).get("24"), 39_740_434
        ),
        "overseas Putin vote": (overseas.get("votes", {}).get("24"), 155_107),
        "overseas against-all vote": (overseas.get("votes", {}).get("29"), 4_886),
        "domestic Putin vote": (
            sum(region.get("votes", {}).get("24", 0) for region in regions),
            39_585_360,
        ),
        "domestic against-all vote": (
            sum(region.get("votes", {}).get("29", 0) for region in regions),
            1_409_787,
        ),
    }
    for label, (actual, expected) in fixed.items():
        if actual != expected:
            errors.append(f"{label}: expected {expected}, found {actual}")
    for asset in ASSETS:
        source = dataset.get("sources", {}).get(asset.key, {})
        if source.get("sha256") != asset.sha256:
            errors.append(f"{asset.key} source hash is not the pinned official capture")

    return {
        "passed": not errors,
        "errors": errors,
        "region_count": len(regions),
        "tik_count": tik_count,
        "choice_count": len(choices),
        "cross_workbook_region_vote_totals_reconciled": not any(
            "region-" in error and "workbook summary/TIK" in error for error in errors
        ),
        "corrections_applied": correction.get("applied") is True,
        "corrected_region_tik_deltas_bounded": not any(
            "correction delta" in error for error in errors
        ),
        "national_totals_reconciled": not any(
            "national " in error and "reconcile" in error for error in errors
        ),
    }


def _fetcher(args: argparse.Namespace, store: ResponseStore) -> Fetcher:
    return Fetcher(
        store,
        SharedRateLimiter(args.rate, coordination_dir=args.coordination_dir),
        FetchConfig(
            timeout=args.timeout,
            retries=args.retries,
            backoff_initial=args.backoff_initial,
            backoff_max=args.backoff_max,
        ),
    )


def _record_payload(store: ResponseStore, asset: Asset) -> tuple[dict[str, Any], bytes]:
    record = store.verified(asset.archive_url)
    if not record:
        raise FileNotFoundError(f"crawl {asset.key} first: {asset.archive_url}")
    payload = (store.root / record["body_path"]).read_bytes()
    validate_asset(asset, payload)
    return record, payload


def discover(args: argparse.Namespace) -> int:
    store = ResponseStore(args.raw_dir)
    fetcher = _fetcher(args, store)
    asset = ASSET_BY_KEY["index"]
    record = fetcher.fetch(asset.archive_url, refresh=args.refresh)
    if not record.get("body_path"):
        raise RuntimeError(f"index fetch failed: {record}")
    payload = (store.root / record["body_path"]).read_bytes()
    validate_asset(asset, payload)
    links = discover_index_assets(payload)
    result = {
        "schema_version": 1,
        "index": _source(asset, record),
        "discovered_original_urls": links,
        "assets": [asset.__dict__ for asset in ASSETS],
    }
    json_write(args.report, result)
    store.flush()
    print(json.dumps({"report": str(args.report), "assets": len(ASSETS)}, indent=2))
    return 0


def crawl(args: argparse.Namespace) -> int:
    discovery = json.loads(args.discovery.read_text(encoding="utf-8"))
    if discovery.get("discovered_original_urls") != sorted(
        _canonical_original(asset.original_url) for asset in ASSETS[1:]
    ):
        raise ValueError("discovery report does not contain the exact official asset set")
    store = ResponseStore(args.raw_dir)
    fetcher = _fetcher(args, store)
    observations = []
    for asset in ASSETS[1:]:
        record = fetcher.fetch(asset.archive_url, refresh=args.refresh)
        valid = False
        error = None
        if record.get("body_path"):
            try:
                validate_asset(asset, (store.root / record["body_path"]).read_bytes())
                valid = True
            except ValueError as exception:
                error = str(exception)
        else:
            error = str(record.get("error") or f"HTTP {record.get('status')}")
        observations.append(
            {"asset": asset.key, **record, "content_valid": valid, "validation_error": error}
        )
    store.flush()
    result = {"schema_version": 1, "observations": observations}
    json_write(args.report, result)
    passed = all(item["content_valid"] for item in observations)
    print(json.dumps({"report": str(args.report), "passed": passed}, indent=2))
    return 0 if passed else 2


def build(args: argparse.Namespace) -> int:
    store = ResponseStore(args.raw_dir)
    records: dict[str, dict[str, Any]] = {}
    payloads: dict[str, bytes] = {}
    for asset in ASSETS:
        records[asset.key], payloads[asset.key] = _record_payload(store, asset)
    summary_book = _open_workbook(payloads["summary"])
    tik_book = _open_workbook(payloads["tik"])
    try:
        summary = parse_summary_sheet(summary_book.sheet_by_index(0))
        tik = parse_tik_workbook(tik_book)
    finally:
        summary_book.release_resources()
        tik_book.release_resources()
    corrections = parse_resolution_corrections(payloads["correction"])
    corrected_summary, applied_corrections = apply_resolution_corrections(
        summary, corrections
    )
    dataset = assemble_dataset(
        summary,
        corrected_summary,
        tik,
        {
            asset.key: _source(asset, records[asset.key])
            for asset in ASSETS
        },
        applied_corrections,
    )
    validation = validate_dataset(dataset)
    dataset["validation"] = validation
    if not validation["passed"]:
        raise ValueError("dataset validation failed: " + "; ".join(validation["errors"][:10]))
    json_write(args.output, dataset)
    print(json.dumps({"output": str(args.output), **validation}, ensure_ascii=False, indent=2))
    return 0


def validate(args: argparse.Namespace) -> int:
    dataset = json.loads(args.input.read_text(encoding="utf-8"))
    result = validate_dataset(dataset)
    result["dataset"] = str(args.input)
    json_write(args.report, result)
    print(json.dumps({"report": str(args.report), **result}, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 2


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Restartable crawler for the official static 2000 presidential archive"
    )
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    common.add_argument("--rate", type=float, default=2.0)
    common.add_argument("--timeout", type=float, default=30.0)
    common.add_argument("--retries", type=int, default=4)
    common.add_argument("--backoff-initial", type=float, default=0.5)
    common.add_argument("--backoff-max", type=float, default=20.0)
    common.add_argument("--coordination-dir", type=Path, default=DEFAULT_COORDINATION_DIR)
    common.add_argument("--refresh", action="store_true")
    sub = result.add_subparsers(dest="command", required=True)
    command = sub.add_parser("discover", parents=[common])
    command.add_argument("--report", type=Path, default=DEFAULT_REPORT_DIR / "discovery.json")
    command.set_defaults(func=discover)
    command = sub.add_parser("crawl", parents=[common])
    command.add_argument("--discovery", type=Path, default=DEFAULT_REPORT_DIR / "discovery.json")
    command.add_argument("--report", type=Path, default=DEFAULT_REPORT_DIR / "crawl.json")
    command.set_defaults(func=crawl)
    command = sub.add_parser("build", parents=[common])
    command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    command.set_defaults(func=build)
    command = sub.add_parser("validate", parents=[common])
    command.add_argument("--input", type=Path, default=DEFAULT_OUTPUT)
    command.add_argument("--report", type=Path, default=DEFAULT_REPORT_DIR / "validation.json")
    command.set_defaults(func=validate)
    return result


def main() -> int:
    args = parser().parse_args()
    if args.rate <= 0 or args.retries < 0:
        raise SystemExit("rate must be positive and retries non-negative")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
