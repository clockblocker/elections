from __future__ import annotations

import sys
import unittest
from pathlib import Path

CRAWLER = Path(__file__).parents[1]
sys.path.insert(0, str(CRAWLER))

from current2026 import (
    _campaign_scope,
    _candidate,
    _contest_sets,
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
        self.assertEqual([node["externalId"] for node in path], ["root", "district", "uik"])
        self.assertEqual(path[-2]["number"], 7)
        self.assertEqual(nodes["uik"]["sources"][0]["sha256"], "b" * 64)


if __name__ == "__main__":
    unittest.main()
