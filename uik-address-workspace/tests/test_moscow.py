from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from uik_address.models import BackboneRow, SourceEvidence
from uik_address.moscow import MOSCOW_ENDPOINT, crawl_moscow_contacts, parse_moscow_response
from uik_address.regional import FetchResponse

NOW = "2026-09-08T12:00:00+00:00"
SOURCE = SourceEvidence("https://cik.test/tree", NOW, "a" * 64, 200, "cec-2026")


def lookup_url(number: int) -> str:
    return f"{MOSCOW_ENDPOINT}?number={number}&config=1"


def payload(number: int, *, date: str = "2026-09-20T00:00:00") -> bytes:
    return json.dumps(
        {
            "id": f"id-{number}",
            "number": number,
            "locality": "Тверской",
            "street": "ПЕТРОВКА УЛ.",
            "house": "23/10 стр. 21",
            "phoneNumber": "+7 925 000-00-00",
            "institutionName": "ГБОУ школа № 2054",
            "localityVotingInstitution": "Тверской",
            "votingInstitutionStreet": "ПЕТРОВКА УЛ.",
            "votingInstitutionHouse": "23/10 стр. 21",
            "votingInstitutionPhone": "+7 925 111-11-11",
            "votingInstitutionName": "ГБОУ школа № 2054",
            "votingDate": date,
        },
        ensure_ascii=False,
    ).encode()


def row(number: int, *, subject_code: str = "77") -> BackboneRow:
    return BackboneRow(
        subject_code,
        "город Москва",
        "",
        "tik-id",
        1,
        "район Тверской",
        f"uik-{number}",
        number,
        SOURCE,
    )


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected network request: {url}")
        return self.responses[url]


class MoscowParserTests(unittest.TestCase):
    def test_accepts_only_exact_2026_polling_place(self) -> None:
        body = payload(146)
        contact, reason = parse_moscow_response(
            body,
            requested_number=146,
            url=lookup_url(146),
            retrieved_at=NOW,
        )
        self.assertEqual("", reason)
        assert contact is not None
        self.assertEqual(146, contact.commission_number)
        self.assertEqual(
            "Тверской, ПЕТРОВКА УЛ., 23/10 стр. 21, ГБОУ школа № 2054",
            contact.voting_address,
        )
        self.assertEqual("moscow_api_2026", contact.source.source_type)
        self.assertEqual(hashlib.sha256(body).hexdigest(), contact.source.sha256)

        wrong_number, reason = parse_moscow_response(
            body,
            requested_number=147,
            url=lookup_url(147),
            retrieved_at=NOW,
        )
        self.assertIsNone(wrong_number)
        self.assertIn("returned UIK 146", reason)

        wrong_date, reason = parse_moscow_response(
            payload(146, date="2025-09-14T00:00:00"),
            requested_number=146,
            url=lookup_url(146),
            retrieved_at=NOW,
        )
        self.assertIsNone(wrong_date)
        self.assertIn("not 2026-09-20", reason)

        wrong_host, reason = parse_moscow_response(
            body,
            requested_number=146,
            url="https://example.test/lookup?number=146",
            retrieved_at=NOW,
        )
        self.assertIsNone(wrong_host)
        self.assertIn("outside", reason)


class MoscowCrawlerTests(unittest.TestCase):
    def test_archives_results_and_reuses_200_and_404_cache(self) -> None:
        responses = {
            lookup_url(146): FetchResponse(
                lookup_url(146), 200, payload(146), "application/json", NOW
            ),
            lookup_url(147): FetchResponse(
                lookup_url(147),
                200,
                payload(147, date="2025-09-14T00:00:00"),
                "application/json",
                NOW,
            ),
            lookup_url(9002): FetchResponse(
                lookup_url(9002), 404, b'{"error":"not found"}', "application/json", NOW
            ),
        }
        fetcher = FakeFetcher(responses)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            summary = crawl_moscow_contacts(
                [row(146), row(147), row(9002), row(1, subject_code="50")],
                output,
                fetcher=fetcher,
                concurrency=2,
            )
            self.assertEqual(3, summary["networkRequests"])
            self.assertEqual(1, summary["contacts"])
            self.assertEqual(2, summary["unresolved"])
            self.assertTrue(summary["complete"])
            manifest = json.loads((output / "manifest.json").read_text())
            self.assertEqual(
                [146, 147, 9002], [item["uikNumber"] for item in manifest["artifacts"]]
            )
            for artifact in manifest["artifacts"]:
                raw = output / artifact["rawPath"]
                self.assertTrue(raw.is_file())
                self.assertEqual(artifact["sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())

            cached_fetcher = FakeFetcher({})
            cached = crawl_moscow_contacts(
                [row(146), row(147), row(9002)], output, fetcher=cached_fetcher
            )
            self.assertEqual([], cached_fetcher.calls)
            self.assertEqual(0, cached["networkRequests"])
            self.assertEqual(3, cached["cachedResponses"])
            self.assertEqual(1, cached["contacts"])

    def test_transient_http_error_is_archived_but_refetched(self) -> None:
        url = lookup_url(146)
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first_fetcher = FakeFetcher(
                {url: FetchResponse(url, 503, b"temporarily unavailable", "text/plain", NOW)}
            )
            first = crawl_moscow_contacts([row(146)], output, fetcher=first_fetcher, max_attempts=1)
            self.assertEqual(1, first["unresolved"])
            self.assertEqual(1, first["remaining"])
            self.assertFalse(first["complete"])
            self.assertTrue((output / "manifest.json").is_file())

            second_fetcher = FakeFetcher(
                {url: FetchResponse(url, 200, payload(146), "application/json", NOW)}
            )
            second = crawl_moscow_contacts([row(146)], output, fetcher=second_fetcher)
            self.assertEqual([url], second_fetcher.calls)
            self.assertEqual(1, second["contacts"])
            self.assertTrue(second["complete"])

    def test_request_limit_leaves_a_resumable_remainder(self) -> None:
        responses = {
            lookup_url(number): FetchResponse(
                lookup_url(number), 200, payload(number), "application/json", NOW
            )
            for number in (1, 2)
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = crawl_moscow_contacts(
                [row(1), row(2)],
                output,
                fetcher=FakeFetcher(responses),
                request_limit=1,
            )
            self.assertEqual(1, first["contacts"])
            self.assertEqual(1, first["remaining"])
            self.assertFalse(first["complete"])

            second_fetcher = FakeFetcher(responses)
            second = crawl_moscow_contacts(
                [row(1), row(2)], output, fetcher=second_fetcher, request_limit=1
            )
            self.assertEqual([lookup_url(2)], second_fetcher.calls)
            self.assertEqual(2, second["contacts"])
            self.assertTrue(second["complete"])


if __name__ == "__main__":
    unittest.main()
