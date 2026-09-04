from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

CURRENT_2026 = Path(__file__).parents[2] / "2026"
sys.path.insert(0, str(CURRENT_2026))

from build_declaration_corpus import build_corpus, extract_district_numbers, main


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class DeclarationCorpusTests(unittest.TestCase):
    def test_unifies_sources_duplicates_and_conservative_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            declarations = root / "declarations"
            report_index = declarations / "index.json"
            party_index = declarations / "party-lists" / "index.json"
            regions_dir = declarations / "regions"
            catalog = root / "regional-declaration-sources.json"
            output = declarations / "corpus.json"
            shared_url = "https://official.test/files/shared.pdf"

            write_json(
                report_index,
                {
                    "declarations": [
                        {
                            "reportId": "report-1",
                            "financialReportId": "financial-1",
                            "reportType": "77",
                            "subjectRf": "77",
                            "fileName": "Иванов.pdf",
                            "archive": {
                                "source": {
                                    "url": shared_url,
                                    "retrievedAt": "2026-09-04T00:00:00Z",
                                    "provenance": "live-official",
                                }
                            },
                            "document": {
                                "path": "files/financial-1/Иванов.pdf",
                                "sha256": "a" * 64,
                                "byteLength": 10,
                            },
                        }
                    ]
                },
            )
            write_json(
                party_index,
                {
                    "documents": [
                        {
                            "partyId": "party-1",
                            "party": "Партия",
                            "category": "large_transactions",
                            "title": "Крупные сделки",
                            "url": "https://official.test/files/party.pdf",
                            "path": "files/party-1/large.pdf",
                            "sha256": "b" * 64,
                            "source": {"provenance": "live-official"},
                        }
                    ]
                },
            )
            write_json(
                regions_dir / "77" / "index.json",
                {
                    "region": {"code": "77", "name": "Москва"},
                    "complete": False,
                    "pages": [
                        {
                            "title": "Одномандатный избирательный округ № 195",
                            "url": "https://moscow.test/election/2026/",
                        },
                        {
                            "title": "Выборы 2026",
                            "url": "https://moscow.test/okrug/196/",
                        },
                    ],
                    "documents": [
                        {
                            "documentId": "regional-1",
                            "category": "inaccuracy",
                            "title": "Выявленные факты недостоверности",
                            "officialUrl": shared_url,
                            "path": "files/inaccuracy.pdf",
                            "sha256": "a" * 64,
                            "sourcePages": [
                                {
                                    "title": "Округ № 195",
                                    "url": "https://moscow.test/election/2026/",
                                }
                            ],
                            "source": {"provenance": "live-official"},
                        }
                    ],
                },
            )
            write_json(
                catalog,
                {
                    "schemaVersion": 1,
                    "regions": [
                        {
                            "code": "77",
                            "name": "Москва",
                            "seedUrls": ["https://moscow.test/"],
                            "probeStatus": {"status": "confirmed", "resolved": True},
                            "evidence": ["known page"],
                        },
                        {
                            "code": "78",
                            "name": "Санкт-Петербург",
                            "seedUrls": ["https://spb.test/"],
                            "probeStatus": "pending",
                        },
                    ],
                },
            )

            corpus = build_corpus(
                report_77_index=report_index,
                party_list_index=party_index,
                regions_dir=regions_dir,
                catalog_path=catalog,
                output=output,
                generated_at="2026-09-04T00:00:00Z",
            )

            self.assertEqual(corpus["summary"]["documentCount"], 3)
            self.assertEqual(
                corpus["summary"]["bySource"],
                {
                    "cik_federal_party_lists": 1,
                    "cik_report_77": 1,
                    "regional_commission": 1,
                },
            )
            self.assertEqual(
                corpus["summary"]["byCategory"],
                {"inaccuracy": 1, "income_property": 1, "large_transactions": 1},
            )
            self.assertEqual(
                corpus["coverage"]["districts"]["explicitNumbers"], [195, 196]
            )
            self.assertNotIn(2026, corpus["coverage"]["districts"]["explicitNumbers"])
            self.assertEqual(len(corpus["duplicates"]["bySha256"]), 1)
            self.assertEqual(len(corpus["duplicates"]["byUrl"]), 1)
            unresolved = corpus["coverage"]["regions"]["unresolvedRegions"]
            self.assertEqual([item["code"] for item in unresolved], ["77", "78"])
            self.assertIn(
                "regional crawler index is incomplete", unresolved[0]["reasons"]
            )
            self.assertIn("no regional crawler index", unresolved[1]["reasons"])
            self.assertIn(
                "catalog probe status is unresolved", unresolved[1]["reasons"]
            )
            self.assertTrue(corpus["metadataOnly"])
            self.assertTrue(all("path" in document for document in corpus["documents"]))
            self.assertEqual(json.loads(output.read_text(encoding="utf-8")), corpus)

    def test_district_parser_requires_an_explicit_district_label(self) -> None:
        self.assertEqual(
            extract_district_numbers("Округ № 112", "https://example.test/"), [112]
        )
        self.assertEqual(
            extract_district_numbers(None, "https://example.test/district/113/"),
            [113],
        )
        self.assertEqual(
            extract_district_numbers(
                None,
                "https://example.test/odnomandatnomu-izbiratelnomu-okrugu-195/",
            ),
            [195],
        )
        self.assertEqual(
            extract_district_numbers(None, "https://example.test/?districtNum=114"),
            [114],
        )
        self.assertEqual(
            extract_district_numbers(
                "Кандидаты на выборах 20.09.2026", "https://example.test/2026/77/"
            ),
            [],
        )
        self.assertEqual(
            extract_district_numbers("Округ № 999", "https://example.test/"), []
        )

    def test_cli_accepts_all_path_overrides_and_missing_sources_are_visible(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "out" / "corpus.json"
            status = main(
                [
                    "--report-77-index",
                    str(root / "missing-report.json"),
                    "--party-list-index",
                    str(root / "missing-parties.json"),
                    "--regions-dir",
                    str(root / "missing-regions"),
                    "--no-catalog",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(status, 0)
            corpus = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(corpus["summary"]["documentCount"], 0)
            self.assertFalse(corpus["sourceIndexes"][0]["present"])
            self.assertFalse(corpus["sourceIndexes"][1]["present"])


if __name__ == "__main__":
    unittest.main()
