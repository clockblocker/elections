from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from uik_address.regional import (
    FetchResponse,
    RegionSource,
    aggregate_cached_regions,
    canonical_url,
    crawl_catalog,
    crawl_region,
    load_catalog,
    parse_artifact,
)
from uik_address.regional_adapters import seed_urls

NOW = "2026-09-08T12:00:00+00:00"


class FakeFetcher:
    def __init__(self, responses: dict[str, FetchResponse]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchResponse:
        self.calls.append(url)
        if url not in self.responses:
            raise AssertionError(f"unexpected network request: {url}")
        return self.responses[url]


def response(url: str, body: str | bytes, content_type: str = "text/html") -> FetchResponse:
    payload = body.encode("utf-8") if isinstance(body, str) else body
    return FetchResponse(url, 200, payload, content_type, NOW)


class CatalogTests(unittest.TestCase):
    def test_loads_real_89_region_catalog(self) -> None:
        catalog = (
            Path(__file__).resolve().parents[2]
            / "proper-data/2026/regional-declaration-sources.json"
        )
        regions = load_catalog(catalog)
        self.assertEqual(89, len(regions))
        self.assertEqual(89, len({region.code for region in regions}))
        self.assertTrue(all(region.base_url for region in regions))
        self.assertTrue(all(region.allowed_hosts for region in regions))

    def test_rejects_seed_outside_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "regions": [
                            {
                                "code": "1",
                                "name": "One",
                                "baseUrl": "https://official.test/",
                                "seedUrls": ["https://evil.test/page"],
                                "allowedHosts": ["official.test"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "allowedHosts"):
                load_catalog(path)

    def test_canonical_url_rejects_non_http(self) -> None:
        self.assertIsNone(canonical_url("javascript:alert(1)", "https://official.test/"))
        self.assertEqual(
            "https://official.test/a", canonical_url("/a#frag", "https://official.test/")
        )


class ParserTests(unittest.TestCase):
    def test_kemerovo_adapter_maps_directory_ordinal_to_backbone_number(self) -> None:
        payload = """
        <html><head><title>Территориальная избирательная комиссия
        Анжеро-Судженского городского округа</title></head><body>
        <strong>Адрес комиссии: </strong><span>652470, г. Анжеро-Судженск, ул. Ленина, 6</span>
        <strong>Телефон: </strong>8-(38453)-6-48-88
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="http://kemerovo.izbirkom.ru/site-tik/tik001/",
            subject_code="42",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:kemerovo_tik_directory", outcome.parser)
        self.assertEqual(1, outcome.contacts[0].commission_number)
        self.assertEqual("8-(38453)-6-48-88", outcome.contacts[0].commission_phone)
        self.assertEqual("regional_adapter_kemerovo_tik", outcome.contacts[0].source.source_type)

        remapped = parse_artifact(
            payload,
            url="http://kemerovo.izbirkom.ru/site-tik/tik043/",
            subject_code="42",
            retrieved_at=NOW,
        )
        self.assertEqual(19, remapped.contacts[0].commission_number)

    def test_kemerovo_adapter_seeds_are_bounded(self) -> None:
        urls = seed_urls("42", "http://kemerovo.izbirkom.ru/")
        self.assertEqual(47, len(urls))
        self.assertTrue(urls[0].endswith("/tik001/"))
        self.assertTrue(urls[-1].endswith("/tik047/"))
        self.assertEqual((), seed_urls("27", "http://khabarovsk.izbirkom.ru/"))

    def test_vologda_adapter_uses_verified_directory_number(self) -> None:
        payload = """
        <html><head><title>Территориальная избирательная комиссия Белозерского
        муниципального округа</title></head><body>
        Территориальная избирательная комиссия Белозерского муниципального округа
        находится по адресу: 161200, Вологодская область, г. Белозерск,
        просп. Советский, д. 63, Телефон: 8 (81756) 23338
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url=(
                "http://vologod.izbirkom.ru/izbiratelnye-komissii/"
                "territorialnye-izbiratelnye-komissii/T03.php"
            ),
            subject_code="35",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:vologda_tik_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(3, contact.commission_number)
        self.assertEqual(
            "161200, Вологодская область, г. Белозерск, просп. Советский, д. 63",
            contact.commission_address,
        )
        self.assertEqual("8 (81756) 23338", contact.commission_phone)
        self.assertEqual("regional_adapter_vologda_tik", contact.source.source_type)

    def test_yamal_adapter_uses_explicit_verified_route_map(self) -> None:
        payload = """
        <html><body>
        Территориальная избирательная комиссия г.Муравленко
        Адрес: 629603 г. Муравленко, ул. Ленина, 80
        тел (34938)2-82-49, 2-84-49
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="http://yamal-nenetsk.izbirkom.ru/about/tik/03tik/adress/index.php",
            subject_code="89",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:yamal_tik_address_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(4, contact.commission_number)
        self.assertEqual("629603 г. Муравленко, ул. Ленина, 80", contact.commission_address)
        self.assertEqual("(34938)2-82-49, 2-84-49", contact.commission_phone)
        self.assertEqual("regional_adapter_yamal_tik", contact.source.source_type)

    def test_vologda_and_yamal_adapter_seeds_are_bounded(self) -> None:
        vologda = seed_urls("35", "http://vologod.izbirkom.ru/")
        self.assertEqual(24, len(vologda))
        self.assertTrue(vologda[0].endswith("/T01.php"))
        self.assertTrue(vologda[-1].endswith("/T24.php"))
        yamal = seed_urls("89", "http://yamal-nenetsk.izbirkom.ru/")
        self.assertEqual(6, len(yamal))
        self.assertTrue(yamal[0].endswith("/01tik/adress/index.php"))
        self.assertTrue(yamal[-1].endswith("/10tik/adress/index.php"))

    def test_belgorod_adapter_uses_verified_municipality_slug(self) -> None:
        payload = """
        <html><body>
        Территориальные избирательные комиссии
        Губкинский городской округ
        Фактический адрес: 309189, Белгородская область, г. Губкин, ул. Мира, 20
        Телефон: +7 (47241) 7-54-97
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="http://belgorod.izbirkom.ru/tik/gubkin/",
            subject_code="31",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:belgorod_tik_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(9, contact.commission_number)
        self.assertEqual(
            "309189, Белгородская область, г. Губкин, ул. Мира, 20",
            contact.commission_address,
        )
        self.assertEqual("+7 (47241) 7-54-97", contact.commission_phone)
        self.assertEqual("regional_adapter_belgorod_tik", contact.source.source_type)

    def test_ugra_adapter_uses_verified_directory_map(self) -> None:
        payload = """
        <html><head><title>Приём обращений в ТИК Октябрьского района</title></head>
        <body>
        Адрес комиссии: 628100, пгт Октябрьское, ул. Ленина, дом 40, помещение 125.
        Телефон: 8 (34678) 2-13-89
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url=(
                "http://hmao.izbirkom.ru/izbiratelnie-komissii/tik/tikpage/"
                "tik10/priem-og/index.php?sphrase_id=5942"
            ),
            subject_code="86",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:ugra_tik_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(12, contact.commission_number)
        self.assertEqual(
            "628100, пгт Октябрьское, ул. Ленина, дом 40, помещение 125", contact.commission_address
        )
        self.assertEqual("8 (34678) 2-13-89", contact.commission_phone)
        self.assertEqual("regional_adapter_ugra_tik", contact.source.source_type)

    def test_belgorod_and_ugra_adapter_seeds_are_bounded(self) -> None:
        belgorod = seed_urls("31", "http://belgorod.izbirkom.ru/")
        self.assertEqual(22, len(belgorod))
        self.assertTrue(belgorod[0].endswith("/tik/alekseevka/"))
        self.assertTrue(belgorod[-1].endswith("/tik/stroitel/"))
        ugra = seed_urls("86", "http://hmao.izbirkom.ru/")
        self.assertEqual(20, len(ugra))
        self.assertTrue(ugra[0].endswith("/tik01/priem-og/index.php"))
        self.assertTrue(ugra[-1].endswith("/tik21/priem-og/index.php"))

    def test_penza_adapter_uses_verified_directory_map_and_excludes_footer(self) -> None:
        payload = """
        <html><head><title>Новости ТИК Башмаковского района</title></head><body>
        ТЕРРИТОРИАЛЬНАЯ ИЗБИРАТЕЛЬНАЯ КОМИССИЯ БАШМАКОВСКОГО РАЙОНА
        Адрес: 442060, Пензенская область, р.п. Башмаково, ул. Советская, 17
        Телефон: (841-43) 4-13-06
        Адрес: 440000, г. Пенза, ул. Володарского, 49
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="http://penza.izbirkom.ru/tik_page/tik_01/index.php",
            subject_code="58",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:penza_tik_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(7, contact.commission_number)
        self.assertEqual(
            "442060, Пензенская область, р.п. Башмаково, ул. Советская, 17",
            contact.commission_address,
        )
        self.assertEqual("(841-43) 4-13-06", contact.commission_phone)
        self.assertEqual("regional_adapter_penza_tik", contact.source.source_type)

    def test_penza_adapter_seeds_are_verified_and_bounded(self) -> None:
        penza = seed_urls("58", "http://penza.izbirkom.ru/")
        self.assertEqual(33, len(penza))
        self.assertTrue(penza[0].endswith("/tik_01/index.php"))
        self.assertTrue(penza[-1].endswith("/tik_35/index.php"))

    def test_parses_explicit_csv_uik_rows(self) -> None:
        payload = (
            "Номер УИК;Адрес комиссии;Телефон комиссии;Адрес помещения для голосования\n"
            "42;ул. Рабочая, 1;+7 (999) 111-22-33;Школа, ул. Рабочая, 3\n"
        ).encode()
        outcome = parse_artifact(
            payload,
            url="https://official.test/uik.csv",
            subject_code="54",
            retrieved_at=NOW,
            content_type="text/csv",
        )
        self.assertEqual("parsed", outcome.status)
        self.assertEqual(1, len(outcome.contacts))
        contact = outcome.contacts[0]
        self.assertEqual("uik", contact.commission_type)
        self.assertEqual(42, contact.commission_number)
        self.assertEqual("УИК №42", contact.commission_name)
        self.assertEqual("Школа, ул. Рабочая, 3", contact.voting_address)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), contact.source.sha256)
        self.assertEqual("regional_csv", contact.source.source_type)

    def test_parses_json_tik_record(self) -> None:
        payload = json.dumps(
            {
                "items": [
                    {
                        "Наименование комиссии": "Территориальная избирательная комиссия Центрального района",
                        "Адрес комиссии": "г. Тест, ул. Мира, 1",
                        "Телефон комиссии": "8 999 000-00-00",
                        "UUID": "abc",
                    }
                ]
            },
            ensure_ascii=False,
        ).encode()
        outcome = parse_artifact(
            payload,
            url="https://official.test/tik.json",
            subject_code="1",
            retrieved_at=NOW,
            content_type="application/json",
        )
        self.assertEqual("parsed", outcome.status)
        self.assertEqual("tik", outcome.contacts[0].commission_type)
        self.assertEqual("abc", outcome.contacts[0].external_id)

    def test_parses_simple_labelled_html_and_table(self) -> None:
        payload = """
        <html><body>
          <section><h2>Территориальная избирательная комиссия Ленинского района</h2>
          <p>Адрес комиссии: г. Тест, ул. Ленина, 2</p><p>Телефон: +7 999 222-33-44</p></section>
          <table><tr><th>Номер УИК</th><th>Адрес помещения для голосования</th></tr>
          <tr><td>7</td><td>Дом культуры, ул. Южная, 5</td></tr></table>
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="https://official.test/contacts/",
            subject_code="1",
            retrieved_at=NOW,
        )
        self.assertEqual("parsed", outcome.status)
        self.assertEqual({"tik", "uik"}, {item.commission_type for item in outcome.contacts})

    def test_ambiguous_csv_and_pdf_remain_unresolved(self) -> None:
        csv_outcome = parse_artifact(
            b"number,address,phone\n7,Somewhere,123456\n",
            url="https://official.test/data.csv",
            subject_code="1",
            retrieved_at=NOW,
        )
        pdf_outcome = parse_artifact(
            b"%PDF-1.7 not parsed",
            url="https://official.test/list.pdf",
            subject_code="1",
            retrieved_at=NOW,
            content_type="application/pdf",
        )
        self.assertEqual("unresolved", csv_outcome.status)
        self.assertEqual("unresolved", pdf_outcome.status)
        self.assertIn("unsupported", pdf_outcome.reason)

    def test_search_snippets_with_ellipses_are_not_published(self) -> None:
        payload = """
        <html><body>
          <div>Территориальная избирательная комиссия Центрального района</div>
          <div>Адрес комиссии: г. Тест ... ул. Мира, 1</div>
          <div>Телефон: (999) 123 ... 45</div>
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="https://official.test/search/?q=тик",
            subject_code="1",
            retrieved_at=NOW,
        )
        self.assertEqual("unresolved", outcome.status)
        self.assertEqual((), outcome.contacts)

    def test_uik_formation_year_is_not_a_commission_number(self) -> None:
        payload = """
        <html><body>
          <h2>Формирование УИК 2013 года</h2>
          <p>Телефон: +7 999 111-22-33</p>
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="https://official.test/archive/",
            subject_code="62",
            retrieved_at=NOW,
        )
        self.assertEqual("unresolved", outcome.status)
        self.assertEqual((), outcome.contacts)


class CrawlTests(unittest.TestCase):
    def region(self, *, max_pages: int = 10, max_depth: int = 2) -> RegionSource:
        return RegionSource(
            "1",
            "Test Region",
            "https://official.test/",
            ("https://official.test/",),
            ("official.test",),
            max_pages,
            max_depth,
        )

    def test_bounded_same_host_crawl_preserves_and_parses(self) -> None:
        search_terms: tuple[str, ...] = ()
        home = """
        <html><head><title>Избирательная комиссия</title></head><body>
          <a href="/contacts/">Территориальные избирательные комиссии: контакты</a>
          <a href="https://evil.test/uik.csv">УИК адреса</a>
          <a href="/noise/">Новости</a>
        </body></html>
        """
        contacts = """
        <html><body><h1>Контакты ТИК</h1>
          <a href="/files/uik.csv">Адреса участковых избирательных комиссий и помещений для голосования</a>
          <a href="/files/phones.pdf">Телефоны ТИК</a>
        </body></html>
        """
        csv_body = "Номер УИК;Адрес помещения для голосования\n15;Школа №1\n"
        fetcher = FakeFetcher(
            {
                "https://official.test/": response("https://official.test/", home),
                "https://official.test/contacts/": response(
                    "https://official.test/contacts/", contacts
                ),
                "https://official.test/files/uik.csv": response(
                    "https://official.test/files/uik.csv", csv_body, "text/csv"
                ),
                "https://official.test/files/phones.pdf": response(
                    "https://official.test/files/phones.pdf", b"%PDF-1.7", "application/pdf"
                ),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            result = crawl_region(
                self.region(),
                fetcher,
                output,
                search_terms=search_terms,
                concurrency=2,
            )
            self.assertNotIn("https://evil.test/uik.csv", fetcher.calls)
            self.assertNotIn("https://official.test/noise/", fetcher.calls)
            self.assertEqual(1, len(result["contacts"]))
            manifest = json.loads((output / "regions/1/manifest.json").read_text())
            unresolved = [
                item for item in manifest["artifacts"] if item["parseStatus"] == "unresolved"
            ]
            self.assertIn(
                "https://official.test/files/phones.pdf", [item["url"] for item in unresolved]
            )
            for item in manifest["artifacts"]:
                if item.get("rawPath"):
                    raw = output / item["rawPath"]
                    self.assertTrue(raw.is_file())
                    self.assertEqual(item["sha256"], hashlib.sha256(raw.read_bytes()).hexdigest())

    def test_depth_and_page_limits_are_explicit(self) -> None:
        first = '<a href="/tik/one">Контакты территориальной избирательной комиссии</a>'
        second = '<a href="/tik/two">Контакты территориальной избирательной комиссии</a>'
        fetcher = FakeFetcher(
            {
                "https://official.test/": response("https://official.test/", first),
                "https://official.test/tik/one": response("https://official.test/tik/one", second),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            result = crawl_region(
                self.region(max_pages=2, max_depth=2),
                fetcher,
                Path(directory),
                search_terms=(),
                concurrency=1,
            )
        self.assertTrue(result["manifest"]["truncated"])
        self.assertEqual(2, result["summary"]["pageRequests"])
        self.assertNotIn("https://official.test/tik/two", fetcher.calls)

    def test_catalog_outputs_are_sorted_and_reuse_cache(self) -> None:
        csv_body = "Номер УИК;Адрес помещения для голосования\n9;Школа\n"
        page = '<a href="/uik.csv">Адреса участковых избирательных комиссий</a>'
        fetcher = FakeFetcher(
            {
                "https://official.test/": response("https://official.test/", page),
                "https://official.test/uik.csv": response(
                    "https://official.test/uik.csv", csv_body, "text/csv"
                ),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            summary = crawl_catalog(
                [self.region()],
                fetcher,
                output,
                max_pages_per_region=5,
                concurrency=1,
                refresh=False,
                search_terms=(),
            )
            first_calls = list(fetcher.calls)
            second = FakeFetcher({})
            crawl_catalog(
                [self.region()],
                second,
                output,
                max_pages_per_region=5,
                concurrency=1,
                refresh=False,
                search_terms=(),
            )
            lines = (output / "contacts.jsonl").read_text().splitlines()
            manifest = json.loads((output / "manifest.json").read_text())
        self.assertEqual(1, summary["contacts"]["total"])
        self.assertEqual(["https://official.test/", "https://official.test/uik.csv"], first_calls)
        self.assertEqual([], second.calls)
        self.assertEqual(1, len(lines))
        self.assertEqual(
            sorted(item["requestedUrl"] for item in manifest["artifacts"]),
            [item["requestedUrl"] for item in manifest["artifacts"]],
        )

    def test_cached_aggregation_preserves_regions_outside_target(self) -> None:
        second = RegionSource(
            "2",
            "Second Region",
            "https://second.test/",
            ("https://second.test/",),
            ("second.test",),
            1,
            0,
        )
        bodies = {
            "https://official.test/": response(
                "https://official.test/",
                "<h1>ТИК Центральная</h1><p>Телефон: 1234567</p>",
            ),
            "https://second.test/": response(
                "https://second.test/",
                "<h1>ТИК Северная</h1><p>Телефон: 7654321</p>",
            ),
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            crawl_catalog(
                [self.region(max_pages=1, max_depth=0)],
                FakeFetcher(bodies),
                output,
                concurrency=1,
                search_terms=(),
            )
            crawl_catalog([second], FakeFetcher(bodies), output, concurrency=1, search_terms=())
            summary = aggregate_cached_regions(
                [self.region(max_pages=1, max_depth=0), second], output
            )
            contacts = (output / "contacts.jsonl").read_text().splitlines()
        self.assertEqual(2, summary["contacts"]["total"])
        self.assertEqual(2, len(contacts))


if __name__ == "__main__":
    unittest.main()
