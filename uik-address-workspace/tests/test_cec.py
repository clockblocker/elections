from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import requests

from uik_address.cec import (
    CecApi,
    CecConfig,
    CecError,
    RawResponseStore,
    crawl_cec_contacts,
)
from uik_address.io import read_jsonl


class FakeResponse:
    def __init__(
        self,
        value: Any,
        status: int = 200,
        *,
        content_type: str = "application/json",
    ) -> None:
        self.status_code = status
        self.content = (
            value
            if isinstance(value, bytes)
            else json.dumps(value, ensure_ascii=False).encode("utf-8")
        )
        self.headers = {"Content-Type": content_type}


class FakeSession:
    def __init__(self, responses: list[FakeResponse | BaseException]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def authentication() -> list[FakeResponse]:
    return [
        FakeResponse({"pubToken": "one-use-public-token", "jsTask": "return 20 + 22;"}),
        FakeResponse({"apiKey": "secret-api-key"}),
    ]


class CecCrawlerTests(unittest.TestCase):
    def test_crawls_one_based_pages_and_keeps_commission_and_voting_addresses_distinct(
        self,
    ) -> None:
        responses: list[FakeResponse | BaseException] = authentication()
        responses.extend(
            [
                FakeResponse(
                    {
                        "content": [
                            {
                                "externalId": "uik-b",
                                "commissionName": "Участковая избирательная комиссия № 12",
                                "commissionType": "5",
                                "commissionNumber": 12,
                                "subjectRF": "077",
                                "phone": "search-phone",
                                "address": "search-address",
                            }
                        ],
                        "totalPages": 2,
                        "pageNumber": 1,
                    }
                ),
                FakeResponse(
                    {
                        "content": [
                            {
                                "externalId": "uik-a",
                                "commissionName": "УИК № 2",
                                "commissionType": "5",
                                "commissionNumber": 2,
                                "subjectRfCode": 50,
                            }
                        ],
                        "totalPages": 2,
                        "pageNumber": 2,
                    }
                ),
                # Enrichment is ordered by external id for deterministic requests.
                FakeResponse(
                    [
                        {
                            "systemExternalId": "uik-a",
                            "commissionName": "УИК № 2",
                            "commissionType": "5",
                            "commissionNumber": 2,
                            "postCode": "100000",
                            "locality": "Комиссия-город",
                            "customAddress": "Комиссия, д. 1",
                            "phone": "parent-phone",
                            "votingRoomPostCode": "200000",
                            "votingRoomLocality": "Голосование-город",
                            "customVotingRoomAddress": "Участок, д. 2",
                            "votingRoomPhone": "voting-phone",
                            "level": 0,
                        }
                    ]
                ),
                FakeResponse(
                    [
                        {
                            "systemExternalId": "uik-b",
                            "commissionName": "УИК № 12",
                            "commissionType": "5",
                            "commissionNumber": 12,
                            "locality": "Старый адрес комиссии",
                            "customAddress": "д. 3",
                            "level": 0,
                        }
                    ]
                ),
            ]
        )
        session = FakeSession(responses)
        with tempfile.TemporaryDirectory() as directory:
            result = crawl_cec_contacts(
                Path(directory),
                base_url="https://cec.test/api",
                commission_types=(5,),
                include_report42=False,
                page_size=1,
                session=session,
                sleep=lambda _: None,
            )

            self.assertEqual(["50", "77"], [item.subject_code for item in result.contacts])
            first = result.contacts[0]
            self.assertEqual("uik", first.commission_type)
            self.assertEqual("100000, Комиссия-город, Комиссия, д. 1", first.commission_address)
            self.assertEqual("200000, Голосование-город, Участок, д. 2", first.voting_address)
            self.assertEqual("parent-phone", first.commission_phone)
            self.assertEqual("voting-phone", first.voting_phone)
            self.assertEqual("cec-commission-parents", first.source.source_type)
            self.assertEqual(2, result.summary["contacts"]["total"])
            self.assertEqual(2, result.summary["contacts"]["with_commission_address"])
            self.assertEqual(1, result.summary["contacts"]["with_voting_address"])
            written = list(read_jsonl(result.contacts_path))
            self.assertEqual(["uik-a", "uik-b"], [row["external_id"] for row in written])

            search_calls = [
                call for call in session.calls if "/commissionOrg/search?" in call["url"]
            ]
            self.assertIn("page=1", search_calls[0]["url"])
            self.assertIn("page=2", search_calls[1]["url"])
            self.assertNotIn("page=0", " ".join(call["url"] for call in search_calls))

            manifest = result.manifest_path.read_text(encoding="utf-8")
            self.assertNotIn("secret-api-key", manifest)
            self.assertNotIn("one-use-public-token", manifest)

    def test_report_42_is_preferred_and_voting_room_is_not_commission_address(self) -> None:
        session = FakeSession(
            authentication()
            + [
                FakeResponse(
                    {
                        "content": [
                            {
                                "externalId": "uik-1",
                                "commissionName": "УИК №1",
                                "commissionNumber": 1,
                                "commissionType": 5,
                                "subjectRF": 71,
                            }
                        ],
                        "totalPages": 1,
                    }
                ),
                FakeResponse(
                    [
                        {
                            "systemExternalId": "uik-1",
                            "locality": "Parent locality",
                            "customAddress": "Parent address",
                            "level": 0,
                        }
                    ]
                ),
                FakeResponse(
                    {
                        "createdAt": "2026-09-07T00:00:00Z",
                        "body": {
                            "commissionOrgId": "uik-1",
                            "commissionName": "Участковая избирательная комиссия №1",
                            "commissionNumber": 1,
                            "commissionType": "5",
                            "commissionPostCode": "300000",
                            "commissionLocality": "Тула",
                            "commissionAddress": "ул. Комиссии, д. 1",
                            "commissionPhone": "11-11",
                            "votingRoom": "300001, Тула, ул. Голосования, д. 2, школа",
                            "votingRoomPhone": "22-22",
                        },
                    }
                ),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            result = crawl_cec_contacts(
                Path(directory),
                base_url="https://cec.test/api",
                commission_types=(5,),
                session=session,
                sleep=lambda _: None,
            )
        contact = result.contacts[0]
        self.assertEqual("300000, Тула, ул. Комиссии, д. 1", contact.commission_address)
        self.assertEqual("300001, Тула, ул. Голосования, д. 2, школа", contact.voting_address)
        self.assertEqual("cec-report-42", contact.source.source_type)

    def test_second_run_is_fully_resumed_from_verified_raw_cache(self) -> None:
        first_session = FakeSession(
            authentication()
            + [
                FakeResponse(
                    {
                        "content": [
                            {
                                "externalId": "uik-1",
                                "commissionName": "УИК №1",
                                "commissionNumber": 1,
                                "commissionType": 5,
                                "subjectRF": 71,
                                "address": "Комиссия",
                            }
                        ],
                        "totalPages": 1,
                    }
                ),
                FakeResponse([]),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            first = crawl_cec_contacts(
                output,
                base_url="https://cec.test/api",
                commission_types=(5,),
                include_report42=False,
                session=first_session,
                sleep=lambda _: None,
            )
            second_session = FakeSession([])
            second = crawl_cec_contacts(
                output,
                base_url="https://cec.test/api",
                commission_types=(5,),
                include_report42=False,
                session=second_session,
                sleep=lambda _: None,
            )
            self.assertEqual([], second_session.calls)
            self.assertEqual(first.contacts, second.contacts)
            self.assertEqual(first.contacts_path.read_bytes(), second.contacts_path.read_bytes())

    def test_nonretryable_http_response_is_resumed_from_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = RawResponseStore(Path(directory) / "raw")
            first_session = FakeSession(authentication() + [FakeResponse({"missing": True}, 404)])
            first = CecApi(
                store,
                CecConfig(base_url="https://cec.test/api"),
                session=first_session,
                sleep=lambda _: None,
            ).request_bytes("GET", "/reports/42", params={"commissionOrgId": "absent"})
            second_session = FakeSession([])
            second = CecApi(
                store,
                CecConfig(base_url="https://cec.test/api"),
                session=second_session,
                sleep=lambda _: None,
            ).request_bytes("GET", "/reports/42", params={"commissionOrgId": "absent"})
        self.assertEqual(404, first.record["status"])
        self.assertTrue(second.cache_hit)
        self.assertEqual([], second_session.calls)

    def test_subject_filter_is_only_a_fallback_for_missing_response_code(self) -> None:
        session = FakeSession(
            authentication()
            + [
                FakeResponse(
                    {
                        "content": [
                            {
                                "externalId": "missing-subject",
                                "commissionName": "УИК №1",
                                "commissionNumber": 1,
                                "commissionType": 5,
                            }
                        ],
                        "totalPages": 1,
                    }
                ),
                FakeResponse(
                    {
                        "content": [
                            {
                                "externalId": "explicit-subject",
                                "commissionName": "УИК №2",
                                "commissionNumber": 2,
                                "commissionType": 5,
                                "subjectRF": 50,
                            }
                        ],
                        "totalPages": 1,
                    }
                ),
                FakeResponse([]),
                FakeResponse([]),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            result = crawl_cec_contacts(
                Path(directory),
                base_url="https://cec.test/api",
                commission_types=(5,),
                subject_codes=("37", "38"),
                include_report42=False,
                session=session,
                sleep=lambda _: None,
            )
        contacts = {contact.external_id: contact for contact in result.contacts}
        self.assertEqual("37", contacts["missing-subject"].subject_code)
        self.assertEqual("50", contacts["explicit-subject"].subject_code)
        search_urls = [
            call["url"] for call in session.calls if "/commissionOrg/search?" in call["url"]
        ]
        self.assertIn("subjectRfCodes=37", search_urls[0])
        self.assertIn("subjectRfCodes=38", search_urls[1])

    def test_proxy_credentials_and_api_key_are_redacted_from_failure_manifest(self) -> None:
        proxy = "http://proxy-user:proxy-password@proxy.test:8080"
        session = FakeSession(
            authentication()
            + [requests.exceptions.ProxyError(f"cannot use {proxy} with secret-api-key")]
        )
        with tempfile.TemporaryDirectory() as directory:
            store = RawResponseStore(Path(directory) / "raw")
            api = CecApi(
                store,
                CecConfig(
                    base_url="https://cec.test/api",
                    proxy_url=proxy,
                    retries=0,
                ),
                session=session,
                sleep=lambda _: None,
            )
            with self.assertRaises(CecError) as raised:
                api.request_json("GET", "/commissionOrg/search", params={"page": 1})
            manifest = store.manifest_path.read_text(encoding="utf-8")

        for secret in (proxy, "proxy-user", "proxy-password", "secret-api-key"):
            self.assertNotIn(secret, str(raised.exception))
            self.assertNotIn(secret, manifest)

    def test_malformed_null_search_address_is_not_published(self) -> None:
        session = FakeSession(
            authentication()
            + [
                FakeResponse(
                    {
                        "content": [
                            {
                                "externalId": "uik-1",
                                "commissionName": "УИК №1",
                                "commissionNumber": 1,
                                "commissionType": 5,
                                "subjectRF": 37,
                                "address": "nullИвановская областьnull",
                            }
                        ],
                        "totalPages": 1,
                    }
                ),
                FakeResponse([]),
            ]
        )
        with tempfile.TemporaryDirectory() as directory:
            result = crawl_cec_contacts(
                Path(directory),
                base_url="https://cec.test/api",
                commission_types=(5,),
                include_report42=False,
                session=session,
                sleep=lambda _: None,
            )
        self.assertEqual("", result.contacts[0].commission_address)


if __name__ == "__main__":
    unittest.main()
