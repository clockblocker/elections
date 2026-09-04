from __future__ import annotations

import io
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

CURRENT_2026 = Path(__file__).parents[2] / "2026"
sys.path.insert(0, str(CURRENT_2026))

from current2026 import (
    _campaign_scope,
    _candidate,
    _contest_sets,
    _extract_declaration_archive,
    _fetch_declaration_listing,
    _ingest_classifier,
    _path_for_node,
)


def election(system: str = "1") -> dict:
    return {
        "id": 123,
        "externalId": "11111111-1111-4111-8111-111111111111",
        "name": "Test election",
        "kind": {"externalId": "2", "value": "Выборы депутатов"},
        "systemType": {"externalId": system, "value": "test"},
    }


class CurrentCandidateTests(unittest.TestCase):
    def test_official_levels_are_grouped_by_campaign_scope(self):
        self.assertEqual(_campaign_scope("1"), "federal")
        self.assertEqual(_campaign_scope("2"), "regional")
        self.assertEqual(_campaign_scope("3"), "municipal")
        self.assertEqual(_campaign_scope("6"), "municipal")
        self.assertEqual(_campaign_scope(None), "other_or_unclassified")

    def test_registration_status_marks_ballot_eligibility(self):
        raw = {
            "id": "candidate-1",
            "fullName": "Иванов Иван Иванович",
            "districtNum": 7,
            "nomination": "выдвинут",
            "enrollment": "выбывший (после регистрации) кандидат",
        }
        value = _candidate(raw, election(), "majoritarian", {"sha256": "a" * 64})
        self.assertEqual(
            value["registrationStatus"]["code"], "withdrawn_after_registration"
        )
        self.assertFalse(value["ballotEligible"])

    def test_majoritarian_associations_do_not_become_a_party_ballot(self):
        candidate = {
            "candidateId": "candidate-1",
            "scope": "majoritarian",
            "districtNumber": 1,
            "ballotEligible": True,
        }
        association = {
            "associationId": "party-1",
            "name": "Party",
            "registrationMark": "not_marked_registered",
        }
        result = _contest_sets(election("1"), [candidate], [association])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["ballotType"], "candidate")

    def test_mixed_election_keeps_registered_and_unresolved_party_marks(self):
        associations = [
            {
                "associationId": "party-1",
                "name": "Registered",
                "registrationMark": "registered",
            },
            {
                "associationId": "party-2",
                "name": "Unresolved",
                "registrationMark": "not_marked_registered",
            },
        ]
        result = _contest_sets(election("3"), [], associations)
        self.assertEqual(result[0]["registeredAssociationIds"], ["party-1"])
        self.assertEqual(result[0]["unresolvedAssociationIds"], ["party-2"])


class CurrentClassifierTests(unittest.TestCase):
    def test_fake_placeholder_is_not_expanded(self):
        tree = {
            "id": 1,
            "externalId": "fake",
            "name": "Placeholder",
            "type": 9,
            "hasChildren": True,
        }
        record = {
            "requested_url": "http://official.test/tree",
            "retrieved_at": "2026-08-30T00:00:00Z",
            "sha256": "c" * 64,
            "status": 200,
        }
        nodes: dict[str, dict] = {}
        pending: list[str] = []
        _ingest_classifier(tree, record, nodes, pending)
        self.assertEqual(pending, [])

    def test_ingest_and_path_preserve_official_district(self):
        tree = {
            "id": 1,
            "externalId": "root",
            "name": "Root",
            "type": 0,
            "hasChildren": True,
            "children": [
                {
                    "id": 2,
                    "externalId": "district",
                    "name": "Округ №7",
                    "type": 3,
                    "number": 7,
                    "hasChildren": True,
                    "children": [
                        {
                            "id": 3,
                            "externalId": "uik",
                            "name": "УИК №42",
                            "type": 5,
                            "number": 42,
                            "hasChildren": False,
                        }
                    ],
                }
            ],
        }
        record = {
            "requested_url": "http://official.test/tree",
            "retrieved_at": "2026-08-30T00:00:00Z",
            "sha256": "b" * 64,
            "status": 200,
        }
        nodes: dict[str, dict] = {}
        pending: list[str] = []
        _ingest_classifier(tree, record, nodes, pending)
        by_id = {node["id"]: node for node in nodes.values()}
        path = _path_for_node(nodes["uik"], by_id)
        self.assertEqual(
            [node["externalId"] for node in path], ["root", "district", "uik"]
        )
        self.assertEqual(path[-2]["number"], 7)
        self.assertEqual(nodes["uik"]["sources"][0]["sha256"], "b" * 64)


class CurrentDeclarationTests(unittest.TestCase):
    @staticmethod
    def declaration_row(identifier: str, file_name: str) -> dict:
        return {
            "id": f"report-{identifier}",
            "reportType": "77",
            "body": {"id": identifier, "fileName": file_name},
        }

    def test_listing_paginates_and_checks_the_official_total(self):
        rows = [
            self.declaration_row("file-1", "Иванов 01.01.2026.PDF"),
            self.declaration_row("file-2", "Петров 01.01.2026.XLSX"),
        ]

        class FakeApi:
            def request_json(self, _method, _path, *, body, refresh):
                page = body["page"]
                return {
                    "content": [rows[page - 1]],
                    "totalPages": 2,
                    "totalSize": 2,
                }, {
                    "status": 200,
                    "official_url": f"http://official.test/page/{page}",
                    "requested_url": f"http://official.test/page/{page}",
                    "sha256": str(page) * 64,
                }

        actual, sources = _fetch_declaration_listing(
            FakeApi(), "election-id", page_size=1, refresh=False
        )
        self.assertEqual(actual, rows)
        self.assertEqual(len(sources), 2)

    def test_archive_is_validated_and_extracted_atomically(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("Иванов 01.01.2026.PDF", b"%PDF-test")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "file-id" / "Иванов 01.01.2026.PDF"
            result = _extract_declaration_archive(
                payload.getvalue(),
                expected_file_name=destination.name,
                destination=destination,
            )
            self.assertEqual(destination.read_bytes(), b"%PDF-test")
            self.assertEqual(result["byteLength"], 9)
            self.assertEqual(len(result["sha256"]), 64)

    def test_archive_member_must_match_the_official_listing(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("unexpected.pdf", b"%PDF-test")
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaisesRegex(RuntimeError, "differs from its listing"),
        ):
            _extract_declaration_archive(
                payload.getvalue(),
                expected_file_name="expected.pdf",
                destination=Path(directory) / "expected.pdf",
            )


if __name__ == "__main__":
    unittest.main()
