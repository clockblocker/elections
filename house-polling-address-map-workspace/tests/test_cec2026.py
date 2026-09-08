from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import requests

from house_polling_address_map.cec2026 import (
    Cec2026Error,
    Cec2026Probe,
    Cec2026Protocol,
)
from house_polling_address_map.store import PollingMapStore


class Response:
    def __init__(self, url: str, value: Any, status: int = 200) -> None:
        self.url = url
        self.status_code = status
        self.content = json.dumps(value, ensure_ascii=False).encode()
        self.headers = {"Content-Type": "application/json"}


class Session:
    def __init__(self, *, multiple: bool = False, voting_date: str = "2026-09-20") -> None:
        self.multiple = multiple
        self.voting_date = voting_date
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.trust_env = True

    def request(self, method: str, url: str, **kwargs: Any) -> Response:
        self.calls.append((method, url, kwargs))
        if url.endswith("/graphql"):
            document = kwargs["json"]["query"]
            if "ADDRESSSearchFulltext" in document:
                rows = [
                    {"id": "address-1", "fullName": "Россия, Тест, Дом 1", "isRetro": False}
                ]
                if self.multiple:
                    rows.append(
                        {"id": "address-2", "fullName": "Россия, Тест, Дом 2", "isRetro": False}
                    )
                return Response(url, {"data": {"ADDRESSSearchFulltext": rows}})
            return Response(
                url,
                {
                    "data": {
                        "nodeADDRESSById": {
                            "id": "address-1",
                            "parents": [
                                {"id": "1", "VCADDRESS_NAME": "Россия"},
                                {"id": "region-25", "VCADDRESS_NAME": "Приморский край"},
                            ],
                        }
                    }
                },
            )
        if url.endswith("/challenge/get"):
            return Response(url, {"jsTask": "return 19 + 23;", "pubToken": "public-token"})
        if url.endswith("/challenge/solve"):
            self.assert_solve(kwargs)
            return Response(url, {"apiKey": "ephemeral-api-key"})
        if "/addresses/elections?" in url:
            return Response(
                url,
                {
                    "content": [
                        {
                            "id": 587813923,
                            "externalId": "duma-2026",
                            "name": "State Duma",
                            "votingDate": self.voting_date,
                        }
                    ]
                },
            )
        if "/addresses/commissionClassifiers?" in url:
            return Response(
                url,
                {
                    "uik": [
                        {
                            "externalId": "uik-external",
                            "number": 840,
                            "subjectRf": "25",
                        }
                    ]
                },
            )
        if "/addresses/commissionOrgs?" in url:
            return Response(url, {"content": [], "empty": True})
        raise AssertionError(url)

    @staticmethod
    def assert_solve(kwargs: dict[str, Any]) -> None:
        assert kwargs["json"]["pubToken"] == "public-token"
        assert kwargs["json"]["answer"] == "42"
        assert kwargs["json"]["fingerprint"]


def protocol() -> Cec2026Protocol:
    return Cec2026Protocol(
        "http://example.test/address/graphql",
        "http://example.test/api",
        587813923,
        "duma-2026",
        "2026-09-20",
    )


class Cec2026ProbeTest(unittest.TestCase):
    def test_probes_one_address_and_does_not_store_authentication_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Session()
            with PollingMapStore(":memory:", Path(directory) / "raw") as store:
                result = Cec2026Probe(
                    protocol(),
                    store,
                    proxy_url="socks5h://user:secret@proxy.test:1080",
                    session=session,
                ).probe("Россия, Тест, Дом 1")

                self.assertEqual("address-1", result.selected_address_id)
                self.assertEqual(("address-1", "1", "region-25"), result.address_ids)
                self.assertEqual(587813923, result.election["id"])
                self.assertEqual(840, result.uiks[0]["number"])
                self.assertEqual((), result.commission_orgs)
                self.assertEqual(5, len(result.captures))
                stored = store._connection.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0]
                self.assertEqual(5, stored)

            self.assertFalse(session.trust_env)
            for _method, _url, kwargs in session.calls:
                self.assertEqual(
                    {
                        "http": "socks5h://user:secret@proxy.test:1080",
                        "https": "socks5h://user:secret@proxy.test:1080",
                    },
                    kwargs["proxies"],
                )
            stored_bodies = b"".join(
                path.read_bytes()
                for path in (Path(directory) / "raw").rglob("*")
                if path.is_file()
            )
            self.assertNotIn(b"ephemeral-api-key", stored_bodies)
            self.assertNotIn(b"public-token", stored_bodies)

    def test_multiple_current_candidates_stops_before_guessing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            session = Session(multiple=True)
            with PollingMapStore(":memory:", Path(directory) / "raw") as store:
                result = Cec2026Probe(protocol(), store, session=session).probe("Тест")
            self.assertIsNone(result.selected_address_id)
            self.assertEqual(2, len(result.candidates))
            self.assertEqual(1, len(result.captures))
            self.assertEqual(1, len(session.calls))

    def test_refuses_mismatched_election_metadata(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            PollingMapStore(":memory:", Path(directory) / "raw") as store,
        ):
            probe = Cec2026Probe(
                protocol(), store, session=Session(voting_date="2027-09-19")
            )
            with self.assertRaisesRegex(Cec2026Error, "unexpected votingDate"):
                probe.probe("Россия, Тест, Дом 1")

    def test_redacts_proxy_credentials_from_transport_errors(self) -> None:
        class BrokenSession:
            trust_env = True

            def request(self, *_args: Any, **_kwargs: Any) -> Response:
                raise requests.ConnectionError(
                    "socks5h://user:secret@proxy.test:1080 rejected user and secret"
                )

        with (
            tempfile.TemporaryDirectory() as directory,
            PollingMapStore(":memory:", Path(directory) / "raw") as store,
        ):
            probe = Cec2026Probe(
                protocol(),
                store,
                proxy_url="socks5h://user:secret@proxy.test:1080",
                session=BrokenSession(),
            )
            with self.assertRaises(Cec2026Error) as raised:
                probe.probe("Россия, Тест, Дом 1")
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("user", str(raised.exception))
        self.assertIn("<configured proxy>", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
