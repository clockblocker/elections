from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from uik_address.assemble import (
    assemble_rows,
    normalize_name,
    public_rows,
    write_csv,
    write_public_csv,
)
from uik_address.gaps import gap_rows, subject_coverage_rows, write_gaps
from uik_address.models import BackboneRow, CommissionContact, SourceEvidence

SOURCE = SourceEvidence(
    "https://official.test/source", "2026-09-08T00:00:00Z", "a" * 64, 200, "cec-commission-org"
)


class AssembleTests(unittest.TestCase):
    def row(self) -> BackboneRow:
        return BackboneRow(
            "77", "город Москва", "", "tik-id", 79, "район Арбат", "uik-id", 1, SOURCE
        )

    def test_matches_uik_by_region_and_number_and_tik_by_unique_name(self) -> None:
        contacts = [
            CommissionContact(
                "77", "5", 1, "УИК №1", "u", "commission", "1", "voting", "2", SOURCE
            ),
            CommissionContact(
                "77",
                "ТИК",
                None,
                "Территориальная избирательная комиссия район Арбат",
                "t",
                "tik address",
                "3",
                "",
                "",
                SOURCE,
            ),
        ]
        rows, coverage = assemble_rows([self.row()], contacts)
        self.assertEqual("tik address", rows[0]["tik_address"])
        self.assertEqual("voting", rows[0]["uik_voting_address"])
        self.assertEqual("unique_normalized_name", rows[0]["tik_match_method"])
        self.assertEqual(1, coverage["tik_matched"])
        self.assertEqual(1, coverage["uik_matched"])

    def test_does_not_name_match_an_ambiguous_tik(self) -> None:
        contacts = [
            CommissionContact("77", "4", None, "район Арбат", "t1", "one", "", "", "", SOURCE),
            CommissionContact("77", "4", None, "район Арбат", "t2", "two", "", "", "", SOURCE),
        ]
        rows, _ = assemble_rows([self.row()], contacts)
        self.assertEqual("unmatched", rows[0]["tik_match_method"])

    def test_does_not_collapse_distinct_no_id_records_from_one_source(self) -> None:
        contacts = [
            CommissionContact("77", "4", None, "район Арбат", "", "one", "", "", "", SOURCE),
            CommissionContact("77", "4", None, "район Арбат", "", "two", "", "", "", SOURCE),
        ]
        rows, coverage = assemble_rows([self.row()], contacts)
        self.assertEqual("unmatched", rows[0]["tik_match_method"])
        self.assertEqual(0, coverage["tik_matched"])

    def test_matches_identical_name_contacts_from_duplicate_official_urls(self) -> None:
        duplicate_source = SourceEvidence(
            "https://official.test/duplicate",
            "2026-09-08T00:00:00Z",
            "d" * 64,
            200,
            "regional_html",
        )
        contacts = [
            CommissionContact("77", "4", None, "район Арбат", "", "one", "phone", "", "", SOURCE),
            CommissionContact(
                "77", "4", None, "район Арбат", "", "one", "phone", "", "", duplicate_source
            ),
        ]
        rows, _ = assemble_rows([self.row()], contacts)
        self.assertEqual("one", rows[0]["tik_address"])
        self.assertEqual("unique_normalized_name_duplicate_sources", rows[0]["tik_match_method"])

    def test_compatible_partial_records_are_not_a_conflict(self) -> None:
        contacts = [
            CommissionContact("77", "5", 1, "УИК №1", "u", "address", "", "", "", SOURCE),
            CommissionContact("77", "5", 1, "УИК №1", "u", "", "phone", "", "", SOURCE),
        ]
        rows, coverage = assemble_rows([self.row()], contacts)
        self.assertEqual("exact_region_number", rows[0]["uik_match_method"])
        self.assertEqual(0, coverage["contact_conflicts"])

    def test_conflicting_records_are_flagged_and_selection_is_deterministic(self) -> None:
        contacts = [
            CommissionContact("77", "5", 1, "УИК №1", "a", "one", "", "", "", SOURCE),
            CommissionContact("77", "5", 1, "УИК №1", "z", "two", "", "", "", SOURCE),
        ]
        forward, forward_coverage = assemble_rows([self.row()], contacts)
        reverse, reverse_coverage = assemble_rows([self.row()], reversed(contacts))
        self.assertEqual(forward, reverse)
        self.assertEqual(forward_coverage, reverse_coverage)
        self.assertEqual("exact_region_number_conflict_selected", forward[0]["uik_match_method"])
        self.assertEqual(1, forward_coverage["uik_match_conflicts"])
        public = next(iter(public_rows(forward)))
        self.assertEqual("", public["uik_voting_address"])
        self.assertEqual("", public["contact_source"])
        self.assertEqual("", public["src"])
        self.assertEqual("conflicted", public["legacy_status"])

    def test_higher_priority_amendment_supersedes_base_without_a_conflict(self) -> None:
        base = SourceEvidence(
            "https://official.test/base.pdf", "2026-08-01", "b" * 64, 200, "regional_pdf_2026"
        )
        amendment = SourceEvidence(
            "https://official.test/amendment",
            "2026-08-10",
            "a" * 64,
            200,
            "regional_html_2026_amendment",
        )
        contacts = [
            CommissionContact("77", "5", 1, "УИК №1", "", "", "", "Old", "", base),
            CommissionContact("77", "5", 1, "УИК №1", "", "", "", "New", "", amendment),
        ]
        rows, coverage = assemble_rows([self.row()], contacts)
        self.assertEqual("New", rows[0]["uik_voting_address"])
        self.assertEqual(0, coverage["contact_conflicts"])

    def test_public_rows_fail_closed_on_conflicting_polling_addresses(self) -> None:
        contacts = [
            CommissionContact("77", "5", 1, "УИК №1", "a", "", "", "one", "", SOURCE),
            CommissionContact("77", "5", 1, "УИК №1", "z", "", "", "two", "", SOURCE),
        ]
        audit, coverage = assemble_rows([self.row()], contacts)
        public = next(iter(public_rows(audit)))
        self.assertEqual(1, coverage["uik_match_conflicts"])
        self.assertEqual(0, coverage["uik_voting_address"])
        self.assertEqual(1, coverage["rows_without_uik_voting_address"])
        self.assertNotEqual("", audit[0]["uik_voting_address"])
        self.assertEqual("", public["uik_voting_address"])
        self.assertEqual("", public["uik_phone"])
        self.assertEqual("", public["contact_source"])

    def test_known_source_types_are_prioritized_but_empty_hits_do_not_mask_data(self) -> None:
        regional = SourceEvidence(
            "https://region.test", "2026-09-08", "b" * 64, 200, "regional_json"
        )
        parent = SourceEvidence(
            "https://cik.test", "2026-09-08", "c" * 64, 200, "cec-commission-parents"
        )
        populated = [
            CommissionContact("77", "5", 1, "УИК №1", "r", "regional", "", "", "", regional),
            CommissionContact("77", "5", 1, "УИК №1", "c", "cec", "", "", "", parent),
        ]
        rows, _ = assemble_rows([self.row()], populated)
        self.assertEqual("cec", rows[0]["uik_commission_address"])

        populated[1] = CommissionContact("77", "5", 1, "УИК №1", "c", "", "", "", "", parent)
        rows, _ = assemble_rows([self.row()], populated)
        self.assertEqual("regional", rows[0]["uik_commission_address"])

    def test_csv_is_excel_friendly_utf8_with_bom(self) -> None:
        rows, _ = assemble_rows([self.row()], [])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            write_csv(path, rows)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))
            with path.open(encoding="utf-8-sig", newline="") as source:
                parsed = list(csv.DictReader(source))
            self.assertEqual("город Москва", parsed[0]["subject_name"])
            self.assertIn(b"\r\n", path.read_bytes())

    def test_failed_csv_write_preserves_previous_file(self) -> None:
        rows, _ = assemble_rows([self.row()], [])

        def broken_rows():
            yield rows[0]
            raise RuntimeError("stopped")

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "out.csv"
            path.write_bytes(b"previous")
            with self.assertRaisesRegex(RuntimeError, "stopped"):
                write_csv(path, broken_rows())
            self.assertEqual(b"previous", path.read_bytes())
            self.assertEqual([], list(path.parent.glob(".*.part")))

    def test_public_csv_has_requested_columns_and_uik_provenance(self) -> None:
        contact = CommissionContact(
            "77", "5", 1, "УИК №1", "u", "адрес комиссии", "", "адрес голосования", "", SOURCE
        )
        rows, _ = assemble_rows([self.row()], [contact])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "public.csv"
            write_public_csv(path, rows)
            with path.open(encoding="utf-8-sig", newline="") as source:
                parsed = list(csv.DictReader(source))
        self.assertEqual(
            [
                "subject",
                "tik_name",
                "tik_number",
                "tik_address",
                "tik_phone",
                "uik_number",
                "uik_voting_address",
                "uik_phone",
                "contact_source",
                "contact_retrieved_at",
                "contact_status",
                "src",
                "legacy_status",
            ],
            list(parsed[0]),
        )
        self.assertEqual("адрес голосования", parsed[0]["uik_voting_address"])
        self.assertEqual(SOURCE.url, parsed[0]["contact_source"])
        self.assertEqual("200", parsed[0]["contact_status"])
        self.assertEqual(SOURCE.url, parsed[0]["src"])
        self.assertEqual("current-2026", parsed[0]["legacy_status"])
        self.assertEqual(SOURCE.url, rows[0]["uik_address_src"])
        self.assertEqual("current-2026", rows[0]["uik_address_legacy_status"])

    def test_public_rows_label_legacy_and_missing_sources(self) -> None:
        legacy_source = SourceEvidence(
            "https://official.test/archive",
            "2026-09-08T00:00:00Z",
            "b" * 64,
            200,
            "regional-legacy-html",
        )
        legacy_contact = CommissionContact(
            "77", "5", 1, "УИК №1", "u", "", "", "legacy polling place", "", legacy_source
        )
        legacy_rows, _ = assemble_rows([self.row()], [legacy_contact])
        legacy_public = next(iter(public_rows(legacy_rows)))
        self.assertEqual(legacy_source.url, legacy_public["src"])
        self.assertEqual("legacy-only", legacy_public["legacy_status"])

        missing_rows, _ = assemble_rows([self.row()], [])
        missing_public = next(iter(public_rows(missing_rows)))
        self.assertEqual("", missing_public["src"])
        self.assertEqual("missing", missing_public["legacy_status"])

    def test_coverage_has_stable_zero_keys_and_fallback_counts(self) -> None:
        _, empty = assemble_rows([], [])
        self.assertEqual(0, empty["rows"])
        self.assertEqual(0, empty["uik_voting_address"])
        self.assertEqual(0, empty["contact_conflicts"])

        tik = CommissionContact(
            "77", "4", 79, "район Арбат", "t", "tik address", "phone", "", "", SOURCE
        )
        _, coverage = assemble_rows([self.row()], [tik])
        self.assertEqual(1, coverage["rows_without_uik_voting_address"])
        self.assertEqual(1, coverage["rows_without_uik_voting_address_with_tik_contact"])
        self.assertEqual(1, coverage["rows_without_uik_voting_address_with_tik_phone"])
        self.assertEqual(1, coverage["rows_without_uik_voting_address_with_tik_address_and_phone"])
        self.assertEqual(1, coverage["rows_with_uik_voting_address_or_tik_contact"])
        self.assertEqual(0, coverage["rows_without_uik_voting_address_or_tik_contact"])

    def test_nonnumeric_regions_sort_deterministically(self) -> None:
        first = BackboneRow("zz", "Z", "", "t", 1, "T", "2", 1, SOURCE)
        second = BackboneRow("aa", "A", "", "t", 1, "T", "1", 1, SOURCE)
        rows, _ = assemble_rows([first, second], [])
        self.assertEqual(["aa", "zz"], [row["subject_code"] for row in rows])

    def test_name_normalization_removes_commission_boilerplate(self) -> None:
        self.assertEqual("район арбат", normalize_name("ТИК — район Арбат"))

    def test_gap_rows_rank_missing_fallback_work(self) -> None:
        first = self.row()
        second = BackboneRow("77", "город Москва", "", "tik-id", 79, "район Арбат", "u2", 2, SOURCE)
        rows, _ = assemble_rows([first, second], [])
        gaps = gap_rows(rows)
        self.assertEqual(1, len(gaps))
        self.assertEqual(2, gaps[0]["priority_missing_uiks"])

        coverage = subject_coverage_rows(rows)
        self.assertEqual("none", coverage[0]["coverage_status"])
        self.assertEqual(2, coverage[0]["uiks_missing_address_and_tik_fallback_count"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gaps.csv"
            write_gaps(path, rows)
            self.assertTrue(path.read_bytes().startswith(b"\xef\xbb\xbf"))


if __name__ == "__main__":
    unittest.main()
