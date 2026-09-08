from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from house_polling_address_map.cec import CecLookup, CecProtocol, CecTransportError
from house_polling_address_map.models import AddressIdentity, BuildingAddress
from house_polling_address_map.resolver import resolve_pending
from house_polling_address_map.store import PollingMapStore


class ResolverTest(unittest.TestCase):
    def test_resolves_one_terminal_suggestion_and_is_resumable(self) -> None:
        suggestion = [{"id": "address-1", "name": "Новосибирск, Ленина, 1", "leaf": True}]
        committee = {
            "name": "Участковая избирательная комиссия №10",
            "subjCode": "54",
            "address": {"address": "Комиссия, 2"},
            "votingAddress": {"address": "Школа, 3", "phone": "123"},
        }

        def transport(request, timeout):
            value = suggestion if "/search/" in request.full_url else committee
            return 200, json.dumps(value, ensure_ascii=False).encode()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with PollingMapStore(root / "map.sqlite3", root / "raw") as store:
                address = BuildingAddress(
                    AddressIdentity("gar", "one"), "54", "Новосибирск, Ленина, 1"
                )
                store.upsert_building(address)
                lookup = CecLookup(
                    CecProtocol(
                        "https://example.invalid/search/{query}/",
                        resolve_url="https://example.invalid/committee/{address_id}",
                    ),
                    transport=transport,
                )
                self.assertEqual(1, resolve_pending(store, lookup, delay_seconds=0)["resolved"])
                self.assertEqual(0, resolve_pending(store, lookup, delay_seconds=0)["resolved"])
                evidence = store.assignments_for(address.identity)
                self.assertEqual("54:10", evidence[0].station_id)
                self.assertIsNotNone(evidence[0].provenance.raw_sha256)
                mapping = next(store.iter_latest_mappings())
                self.assertEqual("Школа, 3", mapping.polling_place_address)
                self.assertEqual(address.identity.key, mapping.address_id)

    def test_preserves_multiple_terminal_matches_as_ambiguous(self) -> None:
        suggestions = [
            {"id": "one", "name": "Москва, Ленина, 1А", "leaf": True},
            {"id": "two", "name": "Москва, Ленина, 1Б", "leaf": True},
        ]

        def transport(request, timeout):
            return 200, json.dumps(suggestions, ensure_ascii=False).encode()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with PollingMapStore(root / "map.sqlite3", root / "raw") as store:
                address = BuildingAddress(AddressIdentity("gar", "two"), "77", "Москва, Ленина, 1")
                store.upsert_building(address)
                lookup = CecLookup(
                    CecProtocol("https://example.invalid/search/{query}/"), transport=transport
                )
                counts = resolve_pending(store, lookup, delay_seconds=0)
                self.assertEqual(1, counts["ambiguous"])
                self.assertEqual(
                    "ambiguous", store.assignments_for(address.identity)[0].status.value
                )

    def test_restart_retries_failed_transport_once_per_run(self) -> None:
        suggestion = [{"id": "address-1", "name": "Омск, Ленина, 1", "leaf": True}]
        committee = {
            "name": "Участковая избирательная комиссия №5",
            "subjCode": "55",
            "votingAddress": {"address": "Омск, школа №1"},
        }
        calls = 0

        def transport(request, timeout):
            nonlocal calls
            calls += 1
            if calls <= 3:
                raise CecTransportError("temporary failure")
            value = suggestion if "/search/" in request.full_url else committee
            return 200, json.dumps(value, ensure_ascii=False).encode()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with PollingMapStore(root / "map.sqlite3", root / "raw") as store:
                address = BuildingAddress(AddressIdentity("gar", "retry"), "55", "Омск, Ленина, 1")
                store.upsert_building(address)
                lookup = CecLookup(
                    CecProtocol(
                        "https://example.invalid/search/{query}/",
                        resolve_url="https://example.invalid/committee/{address_id}",
                    ),
                    transport=transport,
                )

                self.assertEqual(1, resolve_pending(store, lookup, delay_seconds=0)["failed"])
                self.assertEqual(1, store.coverage("55").failed)
                self.assertEqual(1, resolve_pending(store, lookup, delay_seconds=0)["resolved"])

                self.assertEqual(1, store.coverage("55").resolved)
                self.assertEqual(2, len(store.assignments_for(address.identity)))


if __name__ == "__main__":
    unittest.main()
