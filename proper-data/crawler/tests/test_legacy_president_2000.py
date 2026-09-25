from __future__ import annotations

import sys
import unittest
from pathlib import Path

CRAWLER = Path(__file__).parents[1]
sys.path.insert(0, str(CRAWLER))

from legacy_president_2000 import (
    ACCOUNTING_LINES,
    ASSET_BY_KEY,
    CHOICE_LINES,
    CORRECTION_RULES,
    apply_resolution_corrections,
    assemble_dataset,
    discover_index_assets,
    parse_resolution_corrections,
    parse_summary_sheet,
    parse_tik_workbook,
    validate_asset,
)


class FakeSheet:
    def __init__(self, nrows: int, ncols: int) -> None:
        self.nrows = nrows
        self.ncols = ncols
        self.cells = [["" for _ in range(ncols)] for _ in range(nrows)]

    def cell_value(self, row: int, column: int):
        return self.cells[row][column]


class FakeBook:
    def __init__(self, sheets: list[FakeSheet]) -> None:
        self.sheets = sheets
        self.nsheets = len(sheets)

    def sheet_by_index(self, index: int) -> FakeSheet:
        return self.sheets[index]


class LegacyPresident2000Tests(unittest.TestCase):
    @staticmethod
    def correction_html() -> bytes:
        grouped = {}
        for rule in CORRECTION_RULES:
            grouped.setdefault(rule.section, []).append(rule)

        def pairs(section):
            return " ".join(
                f"{rule.after:,}".replace(",", " ")
                + " уточнено вместо "
                + f"{rule.before:,}".replace(",", " ")
                for rule in grouped[section]
            )

        text = "От 7 июля 2000 года № 106/1149-3 "
        text += "следующие уточненные данные " + pairs("national")
        text += " 3. Внести в Сводную таблицу "
        text += "Протокола Избирательной комиссии Белгородской области: " + pairs("belgorod")
        text += " Протокола Избирательной комиссии Калининградской области: " + pairs("kaliningrad")
        text += " Протокола Избирательной комиссии Тульской области: " + pairs("tula")
        for current, following in (
            ("uik5", "5"), ("uik143", "143"), ("uik203", "203"),
            ("uik286", "286"), ("uik289", "289"), ("uik355", "355"),
            ("uik372", "372"),
        ):
            text += f" Протокола участковой избирательной комиссии избирательного участка N {following}, "
            text += pairs(current)
        text += " 4. Предложить"
        return f"<html><body>{text}</body></html>".encode()

    def test_index_discovery_requires_all_four_official_assets(self):
        html = b"""
        <html><body>
          <a href="/banners/vib_arhiv/president/2000/files/2000-Protokol_CIK.doc">p</a>
          <a href="/banners/vib_arhiv/president/2000/files/2000-Svodnaya_CIK.xls">s</a>
          <a href="/banners/vib_arhiv/president/2000/files/2000-TIK-usech.xls">t</a>
          <a href="/banners/vib_arhiv/president/2000/post_1149_pr_2000.html">c</a>
        </body></html>
        """
        links = discover_index_assets(html)
        self.assertEqual(len(links), 4)
        with self.assertRaisesRegex(ValueError, "missing expected assets"):
            discover_index_assets(html.replace(b"2000-TIK-usech.xls", b"other.xls"))

    def test_content_validation_rejects_an_xls_lookalike(self):
        with self.assertRaisesRegex(ValueError, "content identity mismatch"):
            validate_asset(ASSET_BY_KEY["summary"], b"<html>not a workbook</html>")

    def test_summary_parser_skips_print_gutters_and_preserves_overseas(self):
        sheet = FakeSheet(42, 7)
        sheet.cells[9][2] = "Итого"
        sheet.cells[9][4] = "Регион А"
        sheet.cells[9][5] = "Регион Б"
        sheet.cells[9][6] = "За пределами Российской Федерации"
        for offset, line in enumerate(ACCOUNTING_LINES + CHOICE_LINES, start=10):
            sheet.cells[offset][0] = float(line) if line.isdigit() else line
            sheet.cells[offset][1] = f"Строка {line}"
            sheet.cells[offset][3] = float(line) if line.isdigit() else line
            sheet.cells[offset][4] = offset
            sheet.cells[offset][5] = offset + 1
            sheet.cells[offset][6] = offset + 2
            sheet.cells[offset][2] = offset * 3 + 3
        parsed = parse_summary_sheet(sheet)
        self.assertEqual([row["name"] for row in parsed["regions"]], ["Регион А", "Регион Б"])
        self.assertEqual(parsed["overseas"]["votes"]["29"], 42)
        self.assertEqual(parsed["national"]["accounting"]["1"], 33)

    def test_tik_parser_handles_spacer_columns_and_reconciled_totals(self):
        legend = FakeSheet(16, 2)
        region = FakeSheet(16, 5)
        region.cells[0][1] = "Регион А"
        region.cells[3][1] = "Итого"
        region.cells[3][2] = "ТИК Первая"
        region.cells[3][4] = "ТИК Вторая"
        for row, line in enumerate(CHOICE_LINES, start=4):
            legend.cells[row][0] = float(line)
            legend.cells[row][1] = f"Кандидат {line}"
            region.cells[row][0] = float(line)
            region.cells[row][1] = row * 3
            region.cells[row][2] = row
            region.cells[row][3] = float(line)  # repeated print gutter
            region.cells[row][4] = row * 2
        parsed = parse_tik_workbook(FakeBook([legend, region]))
        self.assertEqual(len(parsed["regions"][0]["tiks"]), 2)
        self.assertEqual(parsed["regions"][0]["aggregate_votes"]["24"], 30)
        self.assertEqual(
            parsed["regions"][0]["tiks"][1]["id"], "region-001-tik-002"
        )

    def test_resolution_parser_requires_every_pinned_correction(self):
        payload = self.correction_html()
        parsed = parse_resolution_corrections(payload)
        self.assertEqual(len(parsed), len(CORRECTION_RULES))
        self.assertIn(
            {
                "scope": "national", "line": "24", "before": 39_740_434,
                "after": 39_740_467, "delta": 33, "evidence_section": "national",
            },
            parsed,
        )
        broken = payload.replace(b"39 740 467", b"39 740 468", 1)
        with self.assertRaisesRegex(ValueError, "does not prove national line 24"):
            parse_resolution_corrections(broken)

    def test_corrections_preserve_workbook_version_and_publish_corrected_version(self):
        definitions = {line: f"line {line}" for line in CHOICE_LINES}
        accounting = {line: 0 for line in ACCOUNTING_LINES}
        votes = {line: 0 for line in CHOICE_LINES}
        workbook = {
            "accounting_definitions": {line: f"line {line}" for line in ACCOUNTING_LINES},
            "choice_definitions": definitions,
            "national": {"accounting": {**accounting, "9": 100}, "votes": {**votes, "24": 100}},
            "overseas": {"name": "overseas", "accounting": accounting, "votes": votes},
            "regions": [{"name": "Белгородская область", "accounting": accounting, "votes": {**votes, "24": 100}}],
        }
        changes = [{
            "scope": "Белгородская область", "line": "24", "before": 100,
            "after": 105, "delta": 5, "evidence_section": "fixture",
        }]
        corrected, applied = apply_resolution_corrections(workbook, changes)
        self.assertEqual(workbook["regions"][0]["votes"]["24"], 100)
        self.assertEqual(corrected["regions"][0]["votes"]["24"], 105)
        tik = {
            "choice_definitions": definitions,
            "regions": [{
                "workbook_name": "Белгородская область",
                "aggregate_votes": {**votes, "24": 100},
                "tiks": [{"id": "t1", "name": "TIK", "votes": {**votes, "24": 100}}],
            }],
        }
        dataset = assemble_dataset(workbook, corrected, tik, {}, applied)
        region = dataset["regions"][0]
        self.assertEqual(region["votes"]["24"], 105)
        self.assertEqual(region["workbook_pre_correction"]["votes"]["24"], 100)
        self.assertEqual(region["tik_aggregate_votes"]["24"], 100)
        self.assertEqual(region["correction_delta"]["votes"], {"24": 5})
        self.assertEqual(region["tik_reconciliation_version"], "workbook_pre_correction")
        self.assertEqual(
            dataset["result_versions"]["published"],
            "resolution-106/1149-3-corrected",
        )
        self.assertFalse(dataset["correction"]["corrected_region_tik_detail_available"])


if __name__ == "__main__":
    unittest.main()
