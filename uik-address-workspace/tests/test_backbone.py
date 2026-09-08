from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from uik_address.backbone import (
    ABROAD_SUBJECT_NAME,
    DEFAULT_DUMA_ELECTION_ID,
    BackboneDataError,
    coverage_summary,
    extract_backbone,
    write_backbone_jsonl,
)


def _source(token: str) -> dict[str, object]:
    return {
        "url": f"https://example.test/{token}",
        "retrievedAt": "2026-08-30T14:50:11+00:00",
        "sha256": token * 64,
        "status": 200,
        "provenance": "live-official",
    }


def _uik(
    region: int,
    number: int,
    *,
    election_id: str = DEFAULT_DUMA_ELECTION_ID,
    paths: list[dict[str, object]] | None = None,
    territorial_status: object = None,
) -> dict[str, object]:
    if paths is None:
        paths = [
            {
                "uikClassifierId": f"uik-{region}-{number}",
                "territorial": {
                    "classifierId": f"tik-{region}",
                    "number": region + 10,
                    "name": f"ТИК {region}",
                },
                "source": _source(str(region)),
            }
        ]
    return {
        "uikKey": f"{region}:{number}",
        "regionCode": str(region),
        "territorialStatus": territorial_status,
        "uikNumber": number,
        "assignments": [
            {
                "electionId": election_id,
                "officialClassifierPaths": paths,
            }
        ],
    }


class BackboneFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.uiks = self.root / "2026" / "uiks"
        self.elections = self.root / "2026" / "elections"
        self.baseline = self.root / "2024-president" / "coverage.json"
        self.uiks.mkdir(parents=True)
        self.elections.mkdir(parents=True)
        self.baseline.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _write(path: Path, value: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def test_extracts_only_duma_and_sorts_numeric_region_codes(self) -> None:
        self._write(self.uiks / "region-10.json", [_uik(10, 7)])
        self._write(
            self.uiks / "region-2.json",
            [
                _uik(2, 20),
                _uik(2, 3),
                _uik(2, 99, election_id="another-election"),
            ],
        )
        self._write(
            self.elections / "region-2.json",
            [{"regionCode": "02", "region": "Республика Альфа"}],
        )
        self._write(
            self.baseline,
            {
                "regions": [
                    {"regionCode": "10", "regionName": "Республика Бета"},
                    {"regionCode": "2", "regionName": "старое имя"},
                ]
            },
        )

        rows = extract_backbone(self.uiks)

        self.assertEqual(
            [(row.subject_code, row.uik_number) for row in rows],
            [("2", 3), ("2", 20), ("10", 7)],
        )
        self.assertEqual(rows[0].subject_name, "Республика Альфа")
        self.assertEqual(rows[2].subject_name, "Республика Бета")
        self.assertEqual(rows[0].source.source_type, "live-official")
        self.assertEqual(rows[0].source.sha256, "2" * 64)

    def test_reference_names_override_automatic_sources_and_zero_is_abroad(self) -> None:
        self._write(self.uiks / "region-2.json", [_uik(2, 1)])
        self._write(self.uiks / "region-0.json", [_uik(0, 9001)])
        self._write(
            self.elections / "region-2.json",
            [{"regionCode": "2", "region": "automatic"}],
        )
        reference = self.root / "reference.json"
        self._write(
            reference,
            {
                "regions": [
                    {"code": "02", "name": "explicit"},
                    {"code": "0", "name": "must not replace fixed label"},
                ]
            },
        )

        rows = extract_backbone(self.uiks, reference_regions_path=reference)
        names = {row.subject_code: row.subject_name for row in rows}

        self.assertEqual(names["2"], "explicit")
        self.assertEqual(names["0"], ABROAD_SUBJECT_NAME)

    def test_preserves_territorial_status_as_canonical_compact_json(self) -> None:
        status = {
            "unReference": "https://docs.un.org/A/RES/68/262",
            "internationalRecognition": "part_of_ukraine",
        }
        self._write(
            self.uiks / "region-94.json",
            [_uik(94, 1, territorial_status=status)],
        )

        row = extract_backbone(self.uiks)[0]

        self.assertEqual(
            row.territorial_status,
            (
                '{"internationalRecognition":"part_of_ukraine",'
                '"unReference":"https://docs.un.org/A/RES/68/262"}'
            ),
        )

    def test_rejects_ambiguous_path_and_non_territorial_record(self) -> None:
        valid_path = {
            "uikClassifierId": "uik-2-1",
            "territorial": {"classifierId": "tik-2", "number": 12, "name": "ТИК 2"},
            "source": _source("2"),
        }
        self._write(self.uiks / "region-2.json", [_uik(2, 1, paths=[valid_path, valid_path])])
        with self.assertRaisesRegex(BackboneDataError, "exactly one official classifier path"):
            extract_backbone(self.uiks)

        invalid_path = dict(valid_path)
        invalid_path["territorial"] = {
            "type": 3,
            "classifierId": "district-id",
            "number": 1,
            "name": "district",
        }
        self._write(self.uiks / "region-2.json", [_uik(2, 1, paths=[invalid_path])])
        with self.assertRaisesRegex(BackboneDataError, "must have type 4"):
            extract_backbone(self.uiks)

    def test_jsonl_writer_and_coverage_are_deterministic(self) -> None:
        self._write(self.uiks / "region-10.json", [_uik(10, 8)])
        self._write(self.uiks / "region-2.json", [_uik(2, 4)])
        reference = self.root / "reference.json"
        self._write(reference, [{"regionCode": "2", "regionName": "known"}])
        rows = extract_backbone(self.uiks, reference_regions_path=reference)
        destination = self.root / "out" / "backbone.jsonl"

        count = write_backbone_jsonl(destination, reversed(rows))
        written = [
            json.loads(line) for line in destination.read_text(encoding="utf-8").splitlines()
        ]
        summary = coverage_summary(rows)

        self.assertEqual(count, 2)
        self.assertEqual([item["subject_code"] for item in written], ["2", "10"])
        self.assertEqual(summary["uik_count"], 2)
        self.assertEqual(summary["tik_count"], 2)
        self.assertEqual(summary["subject_names_resolved"], 1)
        self.assertEqual(summary["missing_subject_name_codes"], ["10"])
        self.assertEqual(summary["rows_with_source_url"], 2)


if __name__ == "__main__":
    unittest.main()
