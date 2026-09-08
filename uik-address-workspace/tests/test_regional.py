from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

from openpyxl import Workbook

from uik_address.office_documents import _parse_labelled_pdf_text, _parse_table
from uik_address.regional import (
    FetchResponse,
    RegionSource,
    aggregate_cached_regions,
    canonical_url,
    crawl_catalog,
    crawl_region,
    load_catalog,
    parse_artifact,
    reparse_cached_regions,
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

    def test_loads_curated_supplemental_catalog(self) -> None:
        catalog = Path(__file__).resolve().parents[1] / "official-precinct-sources.json"
        regions = load_catalog(catalog)
        self.assertEqual(
            ["3", "16", "24", "38", "50", "52", "64", "66", "74", "78"],
            [region.code for region in regions],
        )
        petersburg = next(region for region in regions if region.code == "78")
        self.assertEqual(
            {"tik27.spbik.spb.ru", "www.gov.spb.ru"},
            set(petersburg.allowed_hosts),
        )
        self.assertGreaterEqual(len(petersburg.seed_urls), 10)

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
    @staticmethod
    def xlsx_bytes(*, dated: bool = True) -> bytes:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append([None, "Перечень избирательных участков"])
        sheet.append([None, "20 сентября 2026 года" if dated else "Список участков"])
        sheet.append([None, "№ п/п", "Сведения об избирательном участке"])
        sheet.append([None, None, "№", "Адрес", "Телефон"])
        sheet.append([None, "1", 2, 3, 4])
        sheet.append([None, 1, 17, "г. Биробиджан, ул. Ленина, 1", "8 42622 12-34-56"])
        sheet.append([None, 2, 18, "г. Биробиджан, ул. Шолом-Алейхема, 2", "8 42622 65-43-21"])
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        return output.getvalue()

    @staticmethod
    def docx_bytes(*, dated: bool = True, remote: bool = False) -> bytes:
        date = "по состоянию на 16 июня 2026 года" if dated else "архивный список"
        if remote:
            date += " для групп избирателей, где отсутствуют помещения для голосования"
        xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
        <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
          <w:body><w:p><w:r><w:t>{date}</w:t></w:r></w:p><w:tbl>
            <w:tr>
              <w:tc><w:p><w:r><w:t>Номер избирательного участка</w:t></w:r></w:p></w:tc>
              <w:tc><w:p><w:r><w:t>Место нахождения участковой избирательной комиссии</w:t></w:r></w:p></w:tc>
              <w:tc><w:p><w:r><w:t>Адрес помещения для голосования</w:t></w:r></w:p></w:tc>
            </w:tr>
            <w:tr>
              <w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc>
              <w:tc><w:p><w:r><w:t>Администрация, ул. Советская, 1</w:t></w:r></w:p></w:tc>
              <w:tc><w:p><w:r><w:t>Школа, ул. Рабочая, 3</w:t></w:r></w:p></w:tc>
            </w:tr>
          </w:tbl></w:body>
        </w:document>"""
        output = BytesIO()
        with ZipFile(output, "w") as archive:
            archive.writestr("word/document.xml", xml)
        return output.getvalue()

    def test_parses_current_xlsx_precinct_list_with_multiline_headers(self) -> None:
        payload = self.xlsx_bytes()
        outcome = parse_artifact(
            payload,
            url="https://official.test/20-09-2026/precincts.xlsx",
            subject_code="79",
            retrieved_at=NOW,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.assertEqual("parsed", outcome.status)
        self.assertEqual("xlsx_2026", outcome.parser)
        self.assertEqual([17, 18], [contact.commission_number for contact in outcome.contacts])
        self.assertEqual("г. Биробиджан, ул. Ленина, 1", outcome.contacts[0].voting_address)
        self.assertEqual("8 42622 12-34-56", outcome.contacts[0].voting_phone)
        self.assertEqual("", outcome.contacts[0].commission_address)
        self.assertEqual("regional_xlsx_2026", outcome.contacts[0].source.source_type)
        self.assertEqual(hashlib.sha256(payload).hexdigest(), outcome.contacts[0].source.sha256)

    def test_rejects_xlsx_without_2026_freshness_evidence(self) -> None:
        outcome = parse_artifact(
            self.xlsx_bytes(dated=False),
            url="https://official.test/archive/precincts.xlsx",
            subject_code="79",
            retrieved_at=NOW,
        )
        self.assertEqual("unresolved", outcome.status)
        self.assertEqual((), outcome.contacts)

    def test_parses_current_docx_and_keeps_commission_location_separate(self) -> None:
        payload = self.docx_bytes()
        outcome = parse_artifact(
            payload,
            url="https://official.test/files/current-list.docx",
            subject_code="12",
            retrieved_at=NOW,
            content_type=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
        )
        self.assertEqual("parsed", outcome.status)
        self.assertEqual("docx_2026", outcome.parser)
        self.assertEqual(1, len(outcome.contacts))
        contact = outcome.contacts[0]
        self.assertEqual(42, contact.commission_number)
        self.assertEqual("Школа, ул. Рабочая, 3", contact.voting_address)
        self.assertEqual("Администрация, ул. Советская, 1", contact.commission_address)
        self.assertEqual("regional_docx_2026", contact.source.source_type)

    def test_rejects_remote_mobile_voting_locations_as_uik_addresses(self) -> None:
        outcome = parse_artifact(
            self.docx_bytes(remote=True),
            url="https://official.test/2026/remote-voting.docx",
            subject_code="50",
            retrieved_at=NOW,
        )
        self.assertEqual("unresolved", outcome.status)
        self.assertEqual((), outcome.contacts)

    def test_xlsx_copies_explicit_ditto_location_but_keeps_both_fields(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["Перечень избирательных участков на 20 сентября 2026 года"])
        sheet.append(
            [
                "№ п/п",
                "№ УИК",
                "Избирательная комиссия",
                None,
                'Помещение для голосования (-"-, если совпадает с избирательной комиссией)',
            ]
        )
        sheet.append([None, None, "Адрес", "Телефон", "Адрес", "Телефон"])
        sheet.append(
            [
                1,
                7,
                "Хабаровский край, г. Тест, ул. Школьная, 1",
                "8 (4212) 11-22-33",
                '-"-',
                '-"-',
            ]
        )
        output = BytesIO()
        workbook.save(output)
        workbook.close()
        outcome = parse_artifact(
            output.getvalue(),
            url="https://official.test/2026/uik.xlsx",
            subject_code="27",
            retrieved_at=NOW,
        )
        self.assertEqual("parsed", outcome.status)
        contact = outcome.contacts[0]
        self.assertEqual(contact.commission_address, contact.voting_address)
        self.assertEqual(contact.commission_phone, contact.voting_phone)

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

    def test_leningrad_adapter_uses_verified_directory_number(self) -> None:
        payload = """
        <html><head><title>Территориальная избирательная комиссия
        Бокситогорского муниципального района</title></head><body>
        <b>Адрес комиссии:</b>
        <span>187650, Ленинградская область, г. Бокситогорск,
        ул. Социалистическая, д. 9</span>
        <b>Телефон:</b> 8 (81366) 21840
        <footer>Телефон ИСЦ ЦИК России: 8-800-200-00-20</footer>
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url=(
                "http://leningrad-reg.izbirkom.ru/izbiratelnye-komissii/"
                "territorialnye-izbiratelnye-komissii-leningradskoy-oblasti/"
                "tik01-boksitogorskogo-munitsipalnogo-rayona/o-komissii/index.php"
            ),
            subject_code="47",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:leningrad_tik_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(1, contact.commission_number)
        self.assertEqual(
            "187650, Ленинградская область, г. Бокситогорск, ул. Социалистическая, д. 9",
            contact.commission_address,
        )
        self.assertEqual("8 (81366) 21840", contact.commission_phone)
        self.assertEqual("regional_adapter_leningrad_tik", contact.source.source_type)

    def test_leningrad_adapter_seeds_are_verified_and_bounded(self) -> None:
        urls = seed_urls("47", "http://leningrad-reg.izbirkom.ru/")
        self.assertEqual(18, len(urls))
        self.assertTrue(
            urls[0].endswith("/tik01-boksitogorskogo-munitsipalnogo-rayona/o-komissii/index.php")
        )
        self.assertTrue(
            urls[-1].endswith("/tik21-tosnenskogo-munitsipalnogo-rayona/o-komissii/index.php")
        )

    def test_nizhny_novgorod_adapter_uses_current_contact_subpage(self) -> None:
        payload = """
        <html><head><title>Работа с обращениями</title></head><body>
        Адрес комиссии: 607130, Нижегородская область, рп Ардатов, ул. Ленина, 28
        Телефон: 8-83179-5-04-23
        <footer>Адрес 603082, г. Нижний Новгород, Кремль, корп. 14</footer>
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url=("http://nnov.izbirkom.ru/izbiratelnye-komissii/tik-01/rabota-s-obrashcheniyami/"),
            subject_code="52",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:nizhny_novgorod_tik_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(1, contact.commission_number)
        self.assertEqual(
            "607130, Нижегородская область, рп Ардатов, ул. Ленина, 28",
            contact.commission_address,
        )
        self.assertEqual("8-83179-5-04-23", contact.commission_phone)
        self.assertEqual("regional_adapter_nizhny_novgorod_tik", contact.source.source_type)

    def test_sverdlovsk_adapter_uses_verified_editorial_route_map(self) -> None:
        payload = """
        <html><head><title>Алапаевская городская территориальная
        избирательная комиссия</title></head><body>
        Адрес: 624605, Свердловская область, г. Алапаевск, ул. Ленина, д.18.
        Телефон: (34346) 21679.
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="http://sverdlovsk.izbirkom.ru/stranitsy-tik/01/",
            subject_code="66",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:sverdlovsk_tik_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(61, contact.commission_number)
        self.assertEqual(
            "624605, Свердловская область, г. Алапаевск, ул. Ленина, д.18",
            contact.commission_address,
        )
        self.assertEqual("(34346) 21679", contact.commission_phone)
        self.assertEqual("regional_adapter_sverdlovsk_tik", contact.source.source_type)

    def test_chelyabinsk_adapter_bounds_address_before_schedule(self) -> None:
        payload = """
        <html><head><title>Работа с обращениями</title></head><body>
        Обращения граждан принимаются по адресу:
        Челябинская область, Агаповский округ, село Агаповка,
        улица Дорожная, дом 32А, кабинет 30
        Время работы: Пн – чт: с 8.30 до 17.30
        Телефон: 8-(351-40)-2-02-95
        </body></html>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="http://chelyabinsk.izbirkom.ru/site-tik/01/rabota-s-obrashcheniyami/",
            subject_code="74",
            retrieved_at=NOW,
        )
        self.assertEqual("adapter:chelyabinsk_tik_directory", outcome.parser)
        contact = outcome.contacts[0]
        self.assertEqual(1, contact.commission_number)
        self.assertEqual(
            "Челябинская область, Агаповский округ, село Агаповка, "
            "улица Дорожная, дом 32А, кабинет 30",
            contact.commission_address,
        )
        self.assertEqual("8-(351-40)-2-02-95", contact.commission_phone)
        self.assertEqual("regional_adapter_chelyabinsk_tik", contact.source.source_type)

    def test_ural_volga_adapter_seeds_are_verified_and_bounded(self) -> None:
        nizhny = seed_urls("52", "http://nnov.izbirkom.ru/")
        self.assertEqual(61, len(nizhny))
        self.assertTrue(nizhny[0].endswith("/tik-01/rabota-s-obrashcheniyami/"))
        self.assertTrue(nizhny[-1].endswith("/tik-61/rabota-s-obrashcheniyami/"))

        sverdlovsk = seed_urls("66", "http://sverdlovsk.izbirkom.ru/")
        self.assertEqual(81, len(sverdlovsk))
        self.assertTrue(sverdlovsk[0].endswith("/stranitsy-tik/01/"))
        self.assertTrue(sverdlovsk[-1].endswith("/stranitsy-tik/83/"))

        chelyabinsk = seed_urls("74", "http://chelyabinsk.izbirkom.ru/")
        self.assertEqual(51, len(chelyabinsk))
        self.assertTrue(chelyabinsk[0].endswith("/site-tik/01/rabota-s-obrashcheniyami/"))
        self.assertTrue(chelyabinsk[-1].endswith("/site-tik/51/rabota-s-obrashcheniyami/"))

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

    def test_parses_combined_voting_and_commission_html_table(self) -> None:
        payload = """
        <h1>Выборы депутатов Государственной Думы 20 сентября 2026 года</h1>
        <table><tr><th>№ УИК</th>
        <th>Помещение для голосования, место нахождения избирательной комиссии</th>
        <th>Номер телефона участковой избирательной комиссии</th></tr>
        <tr><td>1567</td><td>Гимназия № 1, ул. Наймушина, 9</td>
        <td>7-46-19</td></tr></table>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="https://official.test/2026/precincts.html",
            subject_code="38",
            retrieved_at=NOW,
        )
        self.assertEqual("parsed", outcome.status)
        self.assertEqual(1567, outcome.contacts[0].commission_number)
        self.assertEqual("УИК №1567", outcome.contacts[0].commission_name)
        self.assertEqual("Гимназия № 1, ул. Наймушина, 9", outcome.contacts[0].voting_address)
        self.assertEqual("7-46-19", outcome.contacts[0].commission_phone)
        self.assertEqual("regional_html_2026", outcome.contacts[0].source.source_type)

    def test_parses_current_labelled_municipal_html_blocks(self) -> None:
        payload = """
        <h1>Постановление от 25.03.2026 № 926</h1>
        <h2>Избирательный участок № 3496</h2>
        <p>Состав участка: дома по улице Первомайской №№ 1–19.</p>
        <p>Место нахождения участковой комиссии и помещения для голосования:
        МБОУ СОШ № 25; пгт Фряново, ул. Первомайская, стр. 12.</p>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="https://official.test/acts/25032026-926/",
            subject_code="50",
            retrieved_at=NOW,
        )
        self.assertEqual("parsed", outcome.status)
        self.assertEqual(3496, outcome.contacts[0].commission_number)
        self.assertEqual(
            "МБОУ СОШ № 25; пгт Фряново, ул. Первомайская, стр. 12",
            outcome.contacts[0].voting_address,
        )
        self.assertEqual("regional_html_2026", outcome.contacts[0].source.source_type)

    def test_marks_current_html_amendment_as_superseding_evidence(self) -> None:
        payload = """
        <h1>Постановление от 10.08.2026 о внесении изменений</h1>
        <h2>Избирательный участок № 2414</h2>
        <p>Место нахождения участковой комиссии и помещения для голосования:
        Школа № 1; г. Тест, ул. Новая, 2.</p>
        """.encode()
        outcome = parse_artifact(
            payload,
            url="https://official.test/acts/amendment-2026/",
            subject_code="74",
            retrieved_at=NOW,
        )
        self.assertEqual("regional_html_2026_amendment", outcome.contacts[0].source.source_type)

    def test_ambiguous_csv_and_invalid_pdf_remain_unresolved(self) -> None:
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
        self.assertIn("parse error", pdf_outcome.reason)

    def test_parses_only_explicit_current_combined_pdf_locations(self) -> None:
        text = """
        Постановление администрации от 01.08.2026
        Избирательный участок, участок референдума № 1301
        Место нахождения участковой избирательной комиссии и помещения для
        голосования: Дом культуры, Амурская область, г. Райчихинск, ул. Победы, д. 11
        № телефона: 8 (41647) 2-00-50
        В границах: улица Победы, дома 1, 2, 3.
        Избирательный участок, участок референдума № 1302
        Место нахождения участковой избирательной комиссии и помещения для
        голосования: Школа, Амурская область, г. Райчихинск, ул. Пионерская, д. 31
        № телефона: 8 (41647) 2-30-56
        В границах: улица Пионерская, дома 1, 2, 3.
        """
        rows = _parse_labelled_pdf_text(text, url="https://official.test/list.pdf")
        self.assertEqual([1301, 1302], [row.number for row in rows])
        self.assertEqual(rows[0].commission_address, rows[0].voting_address)
        self.assertEqual("8 (41647) 2-00-50", rows[0].voting_phone)

        undated = text.replace("01.08.2026", "архивный список")
        self.assertEqual((), _parse_labelled_pdf_text(undated, url="https://official.test/a.pdf"))

    def test_parses_direct_shared_pdf_address_with_inline_phone(self) -> None:
        text = """
        Перечень на 20 сентября 2026 года
        Избирательный участок № 2180
        Аптекарский пер., д. 3, 4, 6
        Адрес помещения участковой избирательной комиссии и помещения для голосования:
        Миллионная ул., д. 14, школа № 204, тел. 762-00-41
        Избирательный участок № 2181
        Невский пр., д. 2, 4, 6
        Адрес помещения участковой избирательной комиссии и помещения для голосования:
        Невский пр., д. 14, школа № 210, тел. 417-54-36
        """
        rows = _parse_labelled_pdf_text(text, url="https://official.test/2026/list.pdf")
        self.assertEqual([2180, 2181], [row.number for row in rows])
        self.assertEqual("Миллионная ул., д. 14, школа № 204", rows[0].voting_address)
        self.assertEqual("762-00-41", rows[0].voting_phone)

    def test_labelled_location_drops_an_unlabelled_precinct_boundary_tail(self) -> None:
        text = """
        Постановление администрации от 06.04.2026
        Избирательный участок № 2140
        Место нахождения участковой избирательной комиссии и помещения для голосования –
        Дворец культуры имени И.В. Окунева (проспект Вагоностроителей, 1)
        проспект Вагоностроителей – № 3; улицы: Ильича – № 1, 2, 3.
        Избирательный участок № 2141
        Место нахождения участковой избирательной комиссии и помещения для голос ования –
        Школа № 35 (улица Патона, 7)
        улицы: Бажова – № 3, 5; Ильича – № 14, 15.
        Избирательный участок № 2142
        Место нахождения участковой избирательной комиссии и помещения для голосования –
        Пансионат «Тагильский» (улица Красногвардейская, 57а)
        Государственное учреждение «Тагильский пансионат».
        """
        rows = _parse_labelled_pdf_text(text, url="https://official.test/2026/list.docx")
        self.assertEqual([2140, 2141, 2142], [row.number for row in rows])
        self.assertEqual(
            "Дворец культуры имени И.В. Окунева (проспект Вагоностроителей, 1)",
            rows[0].voting_address,
        )
        self.assertEqual("Школа № 35 (улица Патона, 7)", rows[1].voting_address)
        self.assertEqual(
            "Пансионат «Тагильский» (улица Красногвардейская, 57а)",
            rows[2].voting_address,
        )

    def test_table_inline_location_excludes_precinct_boundaries(self) -> None:
        rows = [
            ["№ п/п", "№ изб. участка", "Границы избирательного участка"],
            [
                "",
                "293",
                (
                    "Тихоокеанская ул., дома № 1, 3; Михайловская дорога, дом 6. "
                    "Адрес помещения участковой избирательной комиссии и для "
                    "голосования: Тихоокеанская ул., дом 10, корпус 2, школа № 475, "
                    "тел. 339-95-50 (доб. 1010)"
                ),
            ],
        ]
        parsed = _parse_table(rows, url="https://official.test/2026/list.docx")
        self.assertEqual([293], [row.number for row in parsed])
        self.assertEqual(
            "Тихоокеанская ул., дом 10, корпус 2, школа № 475",
            parsed[0].voting_address,
        )
        self.assertEqual("339-95-50 (доб. 1010)", parsed[0].voting_phone)

    def test_pdf_table_rejoins_hyphenated_number_header_and_keeps_voting_column(self) -> None:
        rows = [
            ["ЕДИНЫЙ ДЕНЬ ГОЛОСОВАНИЯ 20 СЕНТЯБРЯ 2026 ГОДА"],
            [
                "№\nучастковой\nизбира-\nтельной\nкомиссии",
                "Наименование улицы",
                "Номер(а) дома",
                (
                    "Адреса помещений для работы участковой избирательной комиссии "
                    "(наименование объекта), телефон"
                ),
                "Адреса помещений для голосования (наименование объекта)",
            ],
            [
                "1279",
                "Заозёрная ул.",
                "3; 3, корп. 2; 4; 6",
                "Московский пр., д. 80 (Институт детства), 252-73-14",
                "Московский пр., д. 80 (Институт детства)",
            ],
        ]
        parsed = _parse_table(rows, url="https://official.test/2026/list.pdf")
        self.assertEqual([1279], [row.number for row in parsed])
        self.assertEqual(
            "Московский пр., д. 80 (Институт детства)",
            parsed[0].voting_address,
        )

    def test_table_accepts_number_sign_in_uik_cells(self) -> None:
        rows = [
            [
                "№ УИК",
                "Адрес помещений для работы участковой избирательной комиссии",
                "Адрес помещения для голосования",
            ],
            ["№ 104", "Кадетская линия, дом 3", "Кадетская линия, дом 3"],
        ]
        parsed = _parse_table(rows, url="https://official.test/2026/list.xls")
        self.assertEqual([104], [row.number for row in parsed])
        self.assertEqual("Кадетская линия, дом 3", parsed[0].voting_address)

    def test_table_separates_unlabelled_trailing_phone_from_voting_address(self) -> None:
        rows = [
            ["№ УИК", "Адрес помещения для голосования"],
            ["1365", "Фёдора Котанова ул., д. 3 (Детский сад № 80), 679-73-09"],
        ]
        parsed = _parse_table(rows, url="https://official.test/2026/list.pdf")
        self.assertEqual("Фёдора Котанова ул., д. 3 (Детский сад № 80)", parsed[0].voting_address)
        self.assertEqual("679-73-09", parsed[0].voting_phone)

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

    def test_curated_crawl_can_exclude_broad_regional_adapter_seeds(self) -> None:
        source = RegionSource(
            "52",
            "Test Region",
            "https://official.test/",
            ("https://official.test/current-2026.html",),
            ("official.test",),
            1,
            0,
        )
        fetcher = FakeFetcher(
            {
                "https://official.test/current-2026.html": response(
                    "https://official.test/current-2026.html", "<h1>Current list</h1>"
                )
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            crawl_region(
                source,
                fetcher,
                Path(directory),
                search_terms=(),
                include_adapter_seeds=False,
            )
        self.assertEqual(["https://official.test/current-2026.html"], fetcher.calls)

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

    def test_reparse_cached_regions_unlocks_preserved_xlsx_without_network(self) -> None:
        payload = ParserTests.xlsx_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            raw_path = Path("raw/sha256") / digest[:2] / digest
            (output / raw_path).parent.mkdir(parents=True)
            (output / raw_path).write_bytes(payload)
            region_dir = output / "regions/1"
            region_dir.mkdir(parents=True)
            (region_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "region": {"code": "1", "name": "Test Region"},
                        "artifacts": [
                            {
                                "regionCode": "1",
                                "requestedUrl": "https://official.test/2026/precincts.xlsx",
                                "url": "https://official.test/2026/precincts.xlsx",
                                "status": 200,
                                "contentType": (
                                    "application/vnd.openxmlformats-officedocument."
                                    "spreadsheetml.sheet"
                                ),
                                "retrievedAt": NOW,
                                "sha256": digest,
                                "rawPath": raw_path.as_posix(),
                                "candidate": True,
                                "parser": "unsupported",
                                "parseStatus": "unresolved",
                                "parseReason": "unsupported document format",
                                "contactCount": 0,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            summary = reparse_cached_regions([self.region()], output)
            contacts = (region_dir / "contacts.jsonl").read_text(encoding="utf-8").splitlines()
            artifact = json.loads((region_dir / "manifest.json").read_text())["artifacts"][0]
        self.assertEqual(2, len(contacts))
        self.assertEqual("parsed", artifact["parseStatus"])
        self.assertEqual("xlsx_2026", artifact["parser"])
        self.assertEqual(1, summary["reparse"]["newly_parsed_artifacts"])
        self.assertEqual(2, summary["contacts"]["uik"])


if __name__ == "__main__":
    unittest.main()
