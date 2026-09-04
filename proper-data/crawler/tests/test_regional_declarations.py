from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

CURRENT_2026 = Path(__file__).parents[2] / "2026"
CRAWLER = Path(__file__).parents[1]
sys.path[:0] = [str(CURRENT_2026), str(CRAWLER)]

from crawl_regional_declarations import (
    LinkParser,
    _explicit_other_election,
    _explicitly_historical,
    canonical_url,
    category_for,
    crawl_region,
    is_election_finance_document,
    load_catalog,
    validate_document,
)
from transport import ResponseStore


class FakeFetcher:
    def __init__(
        self,
        store: ResponseStore,
        responses: dict[str, bytes | tuple[bytes, str]],
    ) -> None:
        self.store = store
        self.responses = responses

    def fetch(self, url: str, *, refresh: bool = False) -> dict:
        del refresh
        response = self.responses.get(url)
        if response is None:
            return self.store.save_failure(
                url,
                {
                    "retrieved_at": "2026-09-04T00:00:00+00:00",
                    "status": 404,
                    "error": "fixture not found",
                },
            )
        payload, final_url = (
            response if isinstance(response, tuple) else (response, url)
        )
        content_type = (
            "application/pdf" if payload.startswith(b"%PDF-") else "text/html"
        )
        return self.store.save(
            url,
            payload,
            {
                "final_url": final_url,
                "retrieved_at": "2026-09-04T00:00:00+00:00",
                "status": 200,
                "content_type": content_type,
                "provenance": "live-official",
            },
        )


def region(max_pages: int = 10) -> dict:
    return {
        "code": "77",
        "name": "Fixture",
        "seedUrls": ["https://official.test/elections/2026/"],
        "allowedHosts": ["official.test"],
        "maxPages": max_pages,
        "maxDepth": 2,
        "trustedSeedContext": True,
        "followPatterns": [],
    }


class RegionalClassificationTests(unittest.TestCase):
    def test_russian_labels_are_classified_with_inaccuracy_precedence(self):
        self.assertEqual(
            category_for("Сведения о доходах и об имуществе кандидатов"),
            "declaration",
        )
        self.assertEqual(
            category_for("Сведения о выявленных фактах недостоверности"),
            "inaccuracy",
        )
        self.assertEqual(
            category_for("Сведения о расходах кандидата, его супруги по каждой сделке"),
            "declaration",
        )
        self.assertTrue(
            is_election_finance_document(
                "Сведения о поступлении средств в избирательные фонды кандидатов "
                "и расходовании этих средств"
            )
        )
        self.assertFalse(
            is_election_finance_document(
                "Сведения о расходах кандидата, его супруги по каждой сделке"
            )
        )
        self.assertEqual(
            category_for("Недостоверность в сведениях о доходах и имуществе кандидата"),
            "inaccuracy",
        )
        self.assertEqual(category_for("/sved-o-dokh-i-imush/"), "declaration")

    def test_parser_gives_candidate_link_its_section_heading(self):
        parser = LinkParser()
        parser.feed(
            '<h2>Сведения о доходах и имуществе</h2><a href="one.pdf">Иванов</a>'
        )
        parser.close()
        self.assertEqual(parser.links[0].text, "Иванов")
        self.assertIn("доходах", parser.links[0].heading)

    def test_parser_gives_terse_link_its_complete_row_context(self):
        parser = LinkParser()
        parser.feed(
            "<table><tr><td>Сведения о выявленных фактах недостоверности</td>"
            '<td><a href="facts.docx">Выписка из протокола заседания</a></td>'
            "</tr></table>"
        )
        parser.close()
        self.assertEqual(category_for(parser.links[0].context), "inaccuracy")

    def test_embedded_historical_dates_and_other_elections_are_rejected(self):
        self.assertTrue(_explicitly_historical("/docs/202111151030.pdf"))
        self.assertFalse(_explicitly_historical("/docs/202607101030.pdf"))
        self.assertFalse(_explicitly_historical("/edg20092026/gd_doxod211.php"))
        self.assertFalse(
            _explicitly_historical("/gd-2026/candidate%20district%2049.pdf")
        )
        self.assertTrue(_explicit_other_election("/vybory-glavy-respubliki/income.pdf"))
        self.assertFalse(
            _explicit_other_election("/gosduma/vybory-glavy-komissii/income.pdf")
        )

    def test_urls_are_joined_and_fragments_removed(self):
        self.assertEqual(
            canonical_url("../docs/one.pdf#page=2", "HTTPS://OFFICIAL.TEST/a/b/"),
            "https://official.test/a/docs/one.pdf",
        )
        self.assertIsNone(canonical_url("javascript:alert(1)", "https://a.test/"))
        self.assertIsNone(
            canonical_url("http:////izbirkom.ru/election/1", "https://a.test/")
        )
        self.assertEqual(
            canonical_url(" max.ru/id123 /", "https://a.test/"),
            "http://max.ru/id123 /",
        )


class RegionalDocumentTests(unittest.TestCase):
    def test_pdf_signature_wins_over_misleading_url_suffix(self):
        self.assertEqual(
            validate_document(b"%PDF-1.7\nfixture", "https://official.test/file.xls"),
            (".pdf", "application/pdf"),
        )

    def test_xlsx_container_is_identified(self):
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w") as archive:
            archive.writestr("[Content_Types].xml", "types")
            archive.writestr("xl/workbook.xml", "workbook")
        extension, media_type = validate_document(
            payload.getvalue(), "https://official.test/file.zip"
        )
        self.assertEqual(extension, ".xlsx")
        self.assertIn("spreadsheet", media_type)

    def test_html_error_page_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "markup"):
            validate_document(
                b"<!DOCTYPE html><title>denied</title>",
                "https://official.test/file.pdf",
            )


class RegionalCatalogTests(unittest.TestCase):
    def test_catalog_defaults_allowed_hosts_to_seed_hosts(self):
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
                                "seedUrls": ["https://Official.Test/election/"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_catalog(path)
        self.assertEqual(loaded[0]["allowedHosts"], ["official.test"])

    def test_seed_cannot_escape_explicit_host_allowlist(self):
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
                                "seedUrls": ["https://wrong.test/"],
                                "allowedHosts": ["official.test"],
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "seed hosts"):
                load_catalog(path)


class RegionalCrawlTests(unittest.TestCase):
    def test_discovers_both_layouts_and_records_an_explicit_empty_category(self):
        seed = "https://official.test/elections/2026/"
        district_one = "https://official.test/elections/2026/odnomandatny-okrug-1/"
        district_two = "https://official.test/elections/2026/facts-2/"
        declaration = (
            "https://official.test/gd-2026/"
            "postuplenie-i-raskhodovanie-sredstv/declaration.pdf"
        )
        inaccuracy = "https://official.test/docs/facts.pdf"
        election_fund = "https://official.test/docs/election-fund.pdf"
        responses = {
            seed: (
                "<title>2026 election</title>"
                '<a href="odnomandatny-okrug-1/">2026 electoral district 1</a>'
                '<a href="facts-2/">Сведения о выявленных фактах недостоверности</a>'
                '<a href="/news/2026/">Новости выборов 2026</a>'
                '<a href="district-3.jpg">Госдума 2026, одномандатный округ 3</a>'
                '<a href="https://outside.test/district/">outside</a>'
            ).encode(),
            district_one: (
                "<title>Избирательный округ 1</title>"
                "<h2>Сведения о доходах и об имуществе кандидатов</h2>"
                '<a href="/gd-2026/postuplenie-i-raskhodovanie-sredstv/declaration.pdf">'
                "Иванов И.И.</a>"
                '<a href="/docs/election-fund.pdf">'
                "Сведения о поступлении средств в избирательные фонды кандидатов</a>"
                "<h2>Сведения о выявленных фактах недостоверности</h2>"
                "<p>Фактов не выявлено</p>"
            ).encode(),
            district_two: (
                "<title>Сведения о выявленных фактах недостоверности</title>"
                '<a href="/docs/facts.pdf">Петров П.П.</a>'
            ).encode(),
            declaration: b"%PDF-1.4\ndeclaration\n%%EOF",
            inaccuracy: b"%PDF-1.4\nfacts\n%%EOF",
            election_fund: b"%PDF-1.4\nfund\n%%EOF",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ResponseStore(root / "raw")
            config = {**region(), "followPatterns": ["2026|кандидат"]}
            result = crawl_region(
                config, FakeFetcher(store, responses), root / "output"
            )
            index = json.loads(
                (root / "output" / "77" / "index.json").read_text(encoding="utf-8")
            )
            files = list((root / "output" / "77" / "files").glob("*"))

        self.assertTrue(result["complete"], result)
        self.assertEqual(result["pageCount"], 3)
        self.assertEqual(result["documentCounts"], {"declaration": 1, "inaccuracy": 1})
        self.assertEqual(len(files), 2)
        self.assertEqual(index["documentCount"], 2)
        self.assertNotIn(
            election_fund, {document["officialUrl"] for document in index["documents"]}
        )
        retained = next(
            document
            for document in index["documents"]
            if document["officialUrl"] == declaration
        )
        self.assertTrue(retained["scopeWarnings"])
        first_district = next(
            page for page in index["pages"] if page["url"] == district_one
        )
        self.assertIn("inaccuracy", first_district["explicitEmptyCategories"])
        self.assertNotIn(
            "https://outside.test/district/",
            {page["url"] for page in index["pages"]},
        )

    def test_page_bound_sets_truncated_and_incomplete(self):
        seed = "https://official.test/elections/2026/"
        responses = {
            seed: b'<title>2026 election</title><a href="odnomandatny-okrug/">2026 district</a>'
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = crawl_region(
                region(max_pages=1),
                FakeFetcher(ResponseStore(root / "raw"), responses),
                root / "output",
            )
        self.assertTrue(result["truncated"])
        self.assertFalse(result["complete"])

    def test_gd_2026_candidate_tree_reaches_numeric_district_disclosure(self):
        seed = "https://official.test/gd-2026/"
        candidates = "https://official.test/gd-2026/candidates/"
        district = "https://official.test/gd-2026/informatsiya-o-kandidatakh/49/"
        income = f"{district}svedeniya-o-dokhodakh-i-imushchestve/"
        document = f"{income}candidate.pdf"
        responses = {
            seed: '<a href="candidates/">Информация о кандидатах</a>'.encode(),
            candidates: (b'<a href="/gd-2026/informatsiya-o-kandidatakh/49/">49</a>'),
            district: (
                '<a href="svedeniya-o-dokhodakh-i-imushchestve/">'
                "Сведения о доходах и имуществе</a>"
            ).encode(),
            income: (
                "<title>Сведения о доходах и имуществе кандидатов</title>"
                '<a href="candidate.pdf">Кандидат</a>'
            ).encode(),
            document: b"%PDF-1.4\ncurrent\n%%EOF",
        }
        config = {
            **region(),
            "seedUrls": [seed],
            "maxDepth": 3,
            "followPatterns": ["кандидат"],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = crawl_region(
                config,
                FakeFetcher(ResponseStore(root / "raw"), responses),
                root / "output",
            )
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["pageCount"], 4)
        self.assertEqual(result["documents"][0]["officialUrl"], document)

    def test_follows_dokhod_route_when_anchor_text_is_only_svedeniya(self):
        seed = "https://official.test/elections/2026/"
        district = "https://official.test/elections/2026/odnomandatny-okrug-1/"
        income = f"{district}dokhod-imushchestv/"
        document = f"{income}candidate.pdf"
        responses = {
            seed: (
                '<a href="odnomandatny-okrug-1/">'
                "Госдума 2026, одномандатный округ 1</a>"
            ).encode(),
            district: (
                '<a href="dokhod-imushchestv/">Сведения</a> '
                "о доходах и имуществе кандидатов"
            ).encode(),
            income: (
                "<title>Сведения о доходах и имуществе кандидатов</title>"
                '<a href="candidate.pdf">Иванов И.И.</a>'
            ).encode(),
            document: b"%PDF-1.4\ncurrent\n%%EOF",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = crawl_region(
                region(),
                FakeFetcher(ResponseStore(root / "raw"), responses),
                root / "output",
            )
        self.assertTrue(result["complete"])
        self.assertEqual(result["pageCount"], 3)
        self.assertEqual(result["documentCount"], 1)
        self.assertEqual(result["documents"][0]["officialUrl"], document)

    def test_follows_transliterated_candidate_navigation_route(self):
        seed = (
            "https://official.test/elections/2026/"
            "vybory-deputatov-gosudarstvennoy-dumy/"
        )
        information = f"{seed}informatsionnoe-obespechenie-vyborov/"
        candidates = f"{information}svedeniya-o-zaregistrirovannykh-kandidatakh/"
        district = f"{candidates}district-81/"
        document = f"{district}income.pdf"
        responses = {
            seed: (
                '<a href="informatsionnoe-obespechenie-vyborov/">'
                "Информационное обеспечение</a>"
            ).encode(),
            information: (
                '<a href="svedeniya-o-zaregistrirovannykh-kandidatakh/">Сведения</a>'
            ).encode(),
            candidates: ('<a href="district-81/">Одномандатный округ 81</a>').encode(),
            district: (
                "<title>Сведения о доходах и имуществе кандидатов</title>"
                '<a href="income.pdf">Кандидат</a>'
            ).encode(),
            document: b"%PDF-1.4\ncurrent\n%%EOF",
        }
        config = {**region(), "seedUrls": [seed], "maxDepth": 3}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = crawl_region(
                config,
                FakeFetcher(ResponseStore(root / "raw"), responses),
                root / "output",
            )
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["pageCount"], 4)
        self.assertEqual(result["documentCount"], 1)
        self.assertEqual(result["documents"][0]["officialUrl"], document)

    def test_relative_links_use_redirected_trailing_slash_as_their_base(self):
        seed = "https://official.test/elections/2026/gd9/"
        requested_district = f"{seed}district-81"
        final_district = f"{requested_district}/"
        income = f"{final_district}dokhod/"
        document = f"{income}candidate.pdf"
        responses = {
            seed: (
                '<a href="district-81">Госдума 2026, одномандатный округ 81</a>'
            ).encode(),
            requested_district: (
                '<a href="dokhod/">Сведения о доходах</a>'.encode(),
                final_district,
            ),
            income: (
                "<title>Сведения о доходах кандидатов</title>"
                '<a href="candidate.pdf">Кандидат</a>'
            ).encode(),
            document: b"%PDF-1.4\ncurrent\n%%EOF",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = crawl_region(
                {**region(), "seedUrls": [seed]},
                FakeFetcher(ResponseStore(root / "raw"), responses),
                root / "output",
            )
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["pageCount"], 3)
        self.assertEqual(result["documents"][0]["officialUrl"], document)

    def test_direct_inaccuracy_link_survives_an_election_finance_page(self):
        seed = "https://official.test/elections/2026/gd9/"
        finance = f"{seed}finansirovanie-vyborov.php"
        facts = f"{seed}facts.pdf"
        fund = f"{seed}fund.pdf"
        responses = {
            seed: (
                '<a href="finansirovanie-vyborov.php">'
                "Госдума 2026: финансирование выборов</a>"
            ).encode(),
            finance: (
                "<title>Финансирование выборов</title>"
                '<a href="fund.pdf">Сведения о средствах '
                "в избирательном фонде</a>"
                '<a href="facts.pdf">Сведения о выявленных '
                "фактах недостоверности, представленных кандидатами</a>"
            ).encode(),
            facts: b"%PDF-1.4\nfacts\n%%EOF",
            fund: b"%PDF-1.4\nfund\n%%EOF",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = crawl_region(
                {**region(), "seedUrls": [seed]},
                FakeFetcher(ResponseStore(root / "raw"), responses),
                root / "output",
            )
        self.assertTrue(result["complete"], result)
        self.assertEqual(result["documentCounts"], {"inaccuracy": 1})
        self.assertEqual(result["documents"][0]["officialUrl"], facts)

    def test_untrusted_search_seed_rejects_historical_and_staff_income_hits(self):
        seed = "https://official.test/search/"
        current_page = "https://official.test/elections/2026/gd9/"
        current_pdf = (
            "https://official.test/"
            "vybory-deputatov-zakonodatelnogo-sobraniya/current.pdf"
        )
        responses = {
            seed: (
                "<title>Search</title>"
                '<a href="/archive/2016/income.pdf">'
                "Сведения о доходах кандидатов в Государственную Думу 2016</a>"
                '<a href="/protivodeystvie-korrupcii/staff.pdf">'
                "Сведения о доходах сотрудников за 2026 год</a>"
                '<a href="/elections/2026/gd9/">Госдума 2026</a>'
            ).encode(),
            current_page: (
                "<title>Государственная Дума 2026</title>"
                '<a href="/vybory-deputatov-zakonodatelnogo-sobraniya/current.pdf">'
                "Сведения о доходах кандидатов</a>"
                '<a href="/antic/staff.pdf">Сведения о доходах работников</a>'
                '<a href="/vybory-glavy-respubliki/">'
                "Сведения о доходах кандидатов</a>"
            ).encode(),
            current_pdf: b"%PDF-1.4\ncurrent\n%%EOF",
        }
        config = {
            **region(),
            "seedUrls": [seed],
            "trustedSeedContext": False,
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = crawl_region(
                config,
                FakeFetcher(ResponseStore(root / "raw"), responses),
                root / "output",
            )
        self.assertEqual(result["pageCount"], 2)
        self.assertEqual(result["documentCount"], 1)
        self.assertEqual(result["documents"][0]["officialUrl"], current_pdf)
        self.assertTrue(result["documents"][0]["scopeWarnings"])


if __name__ == "__main__":
    unittest.main()
