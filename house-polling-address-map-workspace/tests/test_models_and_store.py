import tempfile
import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from house_polling_address_map.models import (
    AddressIdentity,
    AssignmentEvidence,
    AssignmentStatus,
    BuildingAddress,
    PollingStation,
    Provenance,
    SourceKind,
)
from house_polling_address_map.store import PollingMapStore

NOW = datetime(2026, 9, 8, 10, 30, tzinfo=UTC)


def building(value: str = "gar-building-1", region: str = "54") -> BuildingAddress:
    return BuildingAddress(
        identity=AddressIdentity("GAR", value),
        region_code=region,
        formatted_address="Новосибирск, Красный проспект, д. 1",
        locality=" Новосибирск ",
        street="Красный проспект",
        house="1",
    )


def station(number: int = 100, region: str = "54") -> PollingStation:
    return PollingStation(
        region_code=region,
        uik_number=number,
        polling_place_address="Новосибирск, Красный проспект, д. 3",
        commission_address=" Новосибирск, Красный проспект, д. 5 ",
    )


def provenance(*, retrieved_at: datetime = NOW, raw_sha256: str | None = None) -> Provenance:
    return Provenance(
        source=SourceKind.CEC_LOOKUP,
        source_url="https://example.test/lookup",
        retrieved_at=retrieved_at,
        method="address-id lookup",
        raw_sha256=raw_sha256,
    )


class ModelsTest(unittest.TestCase):
    def test_domain_values_normalize_identity_and_are_immutable(self) -> None:
        address = building()
        self.assertEqual(address.identity, AddressIdentity.from_key("gar:gar-building-1"))
        self.assertEqual("gar:gar-building-1", address.identity.key)
        self.assertEqual("Новосибирск", address.locality)
        self.assertEqual("54:100", station().station_id)
        with self.assertRaises(FrozenInstanceError):
            address.house = "2"  # type: ignore[misc]

    def test_domain_values_reject_invalid_identity(self) -> None:
        invalid_values = (
            (lambda: AddressIdentity("", "1"), "namespace"),
            (lambda: AddressIdentity("gar:bad", "1"), "namespace"),
            (lambda: station(0), "positive"),
            (lambda: station("100"), "positive"),  # type: ignore[arg-type]
            (lambda: provenance(retrieved_at=NOW.replace(tzinfo=None)), "timezone-aware"),
            (
                lambda: Provenance(
                    SourceKind.CEC_LOOKUP,
                    "https://example.test",
                    NOW,
                    "lookup",
                    "not-a-hash",
                ),
                "SHA-256",
            ),
        )
        for factory, message in invalid_values:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                factory()

    def test_assignment_enforces_status_and_inclusive_validity_interval(self) -> None:
        identity = building().identity
        with self.assertRaisesRegex(ValueError, "requires station_id"):
            AssignmentEvidence(identity, AssignmentStatus.RESOLVED, provenance())
        with self.assertRaisesRegex(ValueError, "must not specify"):
            AssignmentEvidence(
                identity,
                AssignmentStatus.NO_MATCH,
                provenance(),
                station_id="54:100",
            )
        with self.assertRaisesRegex(ValueError, "valid_from"):
            AssignmentEvidence(
                identity,
                AssignmentStatus.NO_MATCH,
                provenance(),
                valid_from=date(2026, 2, 1),
                valid_to=date(2026, 1, 31),
            )


class StoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.store = PollingMapStore(root / "map.sqlite3", root / "raw")

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_upserts_refresh_descriptions_without_duplicating_entities(self) -> None:
        original = building()
        self.store.upsert_building(original)
        self.store.upsert_building(
            BuildingAddress(
                original.identity,
                "54",
                "Новосибирск, Красный проспект, дом 1",
                locality="Новосибирск",
                street="Красный проспект",
                house="1",
            )
        )
        self.store.upsert_station(station())
        self.store.upsert_station(PollingStation("54", 100, "Новосибирск, Красный проспект, д. 7"))
        self.assertEqual(1, self.store.coverage("54").total)
        building_row = self.store._connection.execute("SELECT * FROM buildings").fetchone()
        station_row = self.store._connection.execute("SELECT * FROM polling_stations").fetchone()
        self.assertTrue(building_row["formatted_address"].endswith("дом 1"))
        self.assertTrue(station_row["polling_place_address"].endswith("д. 7"))

    def test_raw_responses_are_content_addressed_deduplicated_and_verified(self) -> None:
        body = b'{"uik": 100}'
        expected = sha256(body).hexdigest()
        self.assertEqual(expected, self.store.put_raw_response(body, media_type="application/json"))
        self.assertEqual(expected, self.store.put_raw_response(body))
        self.assertEqual(body, self.store.read_raw_response(expected))
        count = self.store._connection.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]
        self.assertEqual(1, count)
        row = self.store._connection.execute(
            "SELECT relative_path FROM raw_responses WHERE sha256 = ?", (expected,)
        ).fetchone()
        (self.store.raw_directory / row["relative_path"]).write_bytes(b"corrupt")
        with self.assertRaisesRegex(OSError, "integrity"):
            self.store.read_raw_response(expected)

    def test_record_assignment_is_idempotent_and_round_trips(self) -> None:
        address = building()
        uik = station()
        self.store.upsert_building(address)
        self.store.upsert_station(uik)
        raw_hash = self.store.put_raw_response(b"raw")
        evidence = AssignmentEvidence(
            address=address.identity,
            station_id=uik.station_id,
            status=AssignmentStatus.RESOLVED,
            provenance=provenance(raw_sha256=raw_hash),
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 12, 31),
            note="CEC response",
        )
        self.assertEqual(evidence.evidence_id, self.store.record_assignment(evidence))
        self.assertEqual(evidence.evidence_id, self.store.record_assignment(evidence))
        self.assertEqual((evidence,), self.store.assignments_for(address.identity))
        count = self.store._connection.execute("SELECT COUNT(*) FROM assignments").fetchone()[0]
        self.assertEqual(1, count)

    def test_record_assignment_rejects_unknown_references(self) -> None:
        evidence = AssignmentEvidence(
            building().identity,
            AssignmentStatus.RESOLVED,
            provenance(),
            station_id=station().station_id,
        )
        with self.assertRaisesRegex(ValueError, "unknown building"):
            self.store.record_assignment(evidence)

    def test_coverage_uses_latest_applicable_evidence_and_counts_pending(self) -> None:
        mapped = building("mapped")
        pending = building("pending")
        other_region = building("other", "77")
        for address in (mapped, pending, other_region):
            self.store.upsert_building(address)
        uik = station()
        self.store.upsert_station(uik)
        old = AssignmentEvidence(
            mapped.identity,
            AssignmentStatus.RESOLVED,
            provenance(retrieved_at=NOW - timedelta(days=2)),
            station_id=uik.station_id,
            valid_from=date(2025, 1, 1),
            valid_to=date(2025, 12, 31),
        )
        current_failure = AssignmentEvidence(
            mapped.identity,
            AssignmentStatus.FAILED,
            provenance(retrieved_at=NOW),
            valid_from=date(2026, 1, 1),
        )
        self.store.record_assignment(old)
        self.store.record_assignment(current_failure)
        current = self.store.coverage("54", as_of=date(2026, 6, 1))
        historical = self.store.coverage("54", as_of=date(2025, 6, 1))
        self.assertEqual((2, 1, 1), (current.total, current.failed, current.pending))
        self.assertEqual((2, 1, 1), (historical.total, historical.resolved, historical.pending))
        self.assertEqual(0.5, historical.resolved_fraction)
        self.assertEqual(
            (old,), self.store.assignments_for(mapped.identity, as_of=date(2025, 1, 1))
        )

    def test_coverage_latest_result_is_not_double_counted(self) -> None:
        address = building()
        self.store.upsert_building(address)
        self.store.record_assignment(
            AssignmentEvidence(
                address.identity,
                AssignmentStatus.NO_MATCH,
                provenance(retrieved_at=NOW - timedelta(hours=1)),
            )
        )
        self.store.record_assignment(
            AssignmentEvidence(
                address.identity,
                AssignmentStatus.AMBIGUOUS,
                provenance(retrieved_at=NOW),
            )
        )
        coverage = self.store.coverage()
        self.assertEqual(coverage.total, coverage.attempted)
        self.assertEqual(1, coverage.total)
        self.assertEqual(1, coverage.ambiguous)
        self.assertEqual(
            0, sum((coverage.no_match, coverage.resolved, coverage.failed, coverage.pending))
        )


if __name__ == "__main__":
    unittest.main()
