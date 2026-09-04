from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

CURRENT_2026 = Path(__file__).parents[2] / "2026"
sys.path.insert(0, str(CURRENT_2026))

from crawl_cik_party_declarations import (
    CATEGORY_FOREIGN_PROPERTY,
    CATEGORY_INCOME_PROPERTY,
    CATEGORY_LARGE_TRANSACTIONS,
    ResponseStore,
    classify_document,
    crawl,
    parse_declaration_page,
)

PAGE = "http://official.test/declarations/"
HTML = """<!doctype html><meta charset="utf-8">
<h2>Политическая партия «ТЕСТ»</h2>
<a href="income.pdf">Сведения о размере и об источниках доходов кандидатов</a>
<a href="foreign.pdf">Сведения об имуществе за пределами Российской Федерации</a>
<a href="large.pdf">Сведения о расходах по каждой крупной сделке</a>
<!-- <h2>Скрытая партия</h2><a href="hidden.pdf">Сведения о доходах</a> -->
""".encode()


class FakeFetcher:
    def __init__(self, store: ResponseStore, responses: dict[str, bytes]) -> None:
        self.store = store
        self.responses = responses

    def fetch(self, url: str, *, refresh: bool = False) -> dict:
        del refresh
        return self.store.save(
            url,
            self.responses[url],
            {
                "final_url": url,
                "retrieved_at": "2026-09-04T00:00:00+00:00",
                "status": 200,
                "content_type": "application/pdf"
                if url.endswith("pdf")
                else "text/html",
                "provenance": "live-official",
            },
        )


class PartyDeclarationParserTests(unittest.TestCase):
    def test_categories_are_distinct(self) -> None:
        self.assertEqual(
            classify_document("Сведения об источниках доходов"),
            CATEGORY_INCOME_PROPERTY,
        )
        self.assertEqual(
            classify_document("Имущество за рубежом"), CATEGORY_FOREIGN_PROPERTY
        )
        self.assertEqual(
            classify_document("Крупные сделки"), CATEGORY_LARGE_TRANSACTIONS
        )

    def test_visible_links_are_grouped_under_the_nearest_heading(self) -> None:
        documents = parse_declaration_page(HTML, PAGE)
        self.assertEqual(len(documents), 3)
        self.assertEqual(
            {item["party"] for item in documents}, {"Политическая партия «ТЕСТ»"}
        )
        self.assertNotIn("hidden.pdf", " ".join(item["url"] for item in documents))


class PartyDeclarationCrawlTests(unittest.TestCase):
    def test_crawl_writes_verified_portable_corpus(self) -> None:
        responses = {
            PAGE: HTML,
            f"{PAGE}income.pdf": b"%PDF-1.7\nincome",
            f"{PAGE}foreign.pdf": b"%PDF-1.7\nforeign",
            f"{PAGE}large.pdf": b"%PDF-1.7\nlarge",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ResponseStore(root / "raw")
            index = crawl(
                page_url=PAGE,
                output_dir=root / "output",
                minimum_documents=3,
                store=store,
                fetcher=FakeFetcher(store, responses),
            )

            self.assertTrue(index["completeness"]["complete"])
            self.assertEqual(index["completeness"]["validPdf"], 3)
            self.assertTrue((root / "output" / "source.html").is_file())
            for document in index["documents"]:
                path = root / "output" / document["path"]
                self.assertTrue(path.read_bytes().startswith(b"%PDF-"))
            on_disk = json.loads(
                (root / "output" / "index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                on_disk["completeness"]["categoryCounts"],
                {
                    CATEGORY_INCOME_PROPERTY: 1,
                    CATEGORY_FOREIGN_PROPERTY: 1,
                    CATEGORY_LARGE_TRANSACTIONS: 1,
                },
            )

    def test_non_pdf_response_is_reported_and_fails_the_run(self) -> None:
        responses = {
            PAGE: HTML,
            f"{PAGE}income.pdf": b"not a PDF",
            f"{PAGE}foreign.pdf": b"%PDF-1.7\nforeign",
            f"{PAGE}large.pdf": b"%PDF-1.7\nlarge",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ResponseStore(root / "raw")
            with self.assertRaisesRegex(RuntimeError, "downloaded 2/3"):
                crawl(
                    page_url=PAGE,
                    output_dir=root / "output",
                    minimum_documents=3,
                    store=store,
                    fetcher=FakeFetcher(store, responses),
                )
            index = json.loads(
                (root / "output" / "index.json").read_text(encoding="utf-8")
            )
            self.assertEqual(index["completeness"]["failed"], 1)
            self.assertIn("PDF magic", index["failures"][0]["error"])


if __name__ == "__main__":
    unittest.main()
