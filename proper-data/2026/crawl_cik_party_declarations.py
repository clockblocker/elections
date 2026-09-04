from __future__ import annotations

import argparse
import hashlib
import sys
import unicodedata
import urllib.parse
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SHARED_CRAWLER_DIR = Path(__file__).resolve().parents[1] / "crawler"
sys.path.insert(0, str(SHARED_CRAWLER_DIR))

from common import (
    RequestsOpener,
    atomic_write,
    configured_proxy_url,
    json_write,
    sha256_bytes,
)
from transport import (
    FetchConfig,
    Fetcher,
    GlobalRateLimiter,
    ResponseStore,
)

PAGE_URL = (
    "http://www.cikrf.ru/analog/ediny-den-golosovaniya-2026/"
    "kategorii-viborov/vybory-deputatov-gosudarstvennoy-dumy-federalnogo-"
    "sobraniya-rossiyskoy-federatsii-devyatogo-sozyva/federalnye-spiski-"
    "kandidatov/svedeniya-o-dokhodakh-i-imushchestve-kandidatov-perechen-"
    "politicheskikh-partiy-vydvinuvshikh-zaregis/"
)
DEFAULT_RAW_DIR = Path("data/raw/cik-current-2026-party-declarations")
DEFAULT_OUTPUT_DIR = Path("proper-data/2026/declarations/party-lists")
MINIMUM_EXPECTED_DOCUMENTS = 28

CATEGORY_INCOME_PROPERTY = "income_property"
CATEGORY_FOREIGN_PROPERTY = "foreign_property"
CATEGORY_LARGE_TRANSACTIONS = "large_transactions"
CATEGORIES = (
    CATEGORY_INCOME_PROPERTY,
    CATEGORY_FOREIGN_PROPERTY,
    CATEGORY_LARGE_TRANSACTIONS,
)


def _space(value: str) -> str:
    return " ".join(value.split())


def _encoded_url(base_url: str, href: str) -> str:
    """Resolve an official link while retaining a URL-safe request key."""
    absolute = urllib.parse.urljoin(base_url, href)
    parts = urllib.parse.urlsplit(absolute)
    path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/:@")
    query = urllib.parse.quote(urllib.parse.unquote(parts.query), safe="=&/:@")
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, path, query, parts.fragment)
    )


def classify_document(link_text: str, href: str = "") -> str:
    value = _space(f"{link_text} {urllib.parse.unquote(href)}").casefold()
    if "за пределами" in value or "за рубеж" in value:
        return CATEGORY_FOREIGN_PROPERTY
    if "сведения о расходах" in value or "крупн" in value and "сделк" in value:
        return CATEGORY_LARGE_TRANSACTIONS
    if "источник" in value and "доход" in value:
        return CATEGORY_INCOME_PROPERTY
    raise ValueError(f"unrecognized declaration category: {link_text!r}")


class DeclarationPageParser(HTMLParser):
    """Extract visible PDF links and the nearest preceding party heading."""

    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source_url = source_url
        self.current_heading = ""
        self.documents: list[dict[str, str]] = []
        self._heading_parts: list[str] | None = None
        self._link: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        if lowered == "h2":
            self._heading_parts = []
            return
        if lowered != "a":
            return
        href = dict(attrs).get("href") or ""
        decoded_path = urllib.parse.unquote(urllib.parse.urlsplit(href).path)
        if not decoded_path.casefold().endswith(".pdf"):
            return
        self._link = {"href": href, "text_parts": []}

    def handle_data(self, data: str) -> None:
        if self._heading_parts is not None:
            self._heading_parts.append(data)
        if self._link is not None:
            self._link["text_parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "h2" and self._heading_parts is not None:
            self.current_heading = _space("".join(self._heading_parts))
            self._heading_parts = None
            return
        if lowered != "a" or self._link is None:
            return
        href = str(self._link["href"])
        text = _space("".join(self._link["text_parts"]))
        self._link = None
        try:
            category = classify_document(text, href)
        except ValueError:
            return
        if not self.current_heading:
            raise ValueError(f"declaration link has no party heading: {href}")
        self.documents.append(
            {
                "party": self.current_heading,
                "category": category,
                "linkText": text,
                "url": _encoded_url(self.source_url, href),
            }
        )


def parse_declaration_page(payload: bytes, source_url: str) -> list[dict[str, str]]:
    text = payload.decode("utf-8", errors="replace")
    parser = DeclarationPageParser(source_url)
    parser.feed(text)
    parser.close()
    unique: dict[str, dict[str, str]] = {}
    for document in parser.documents:
        unique.setdefault(document["url"], document)
    return list(unique.values())


def _party_id(heading: str) -> str:
    normalized = unicodedata.normalize("NFC", _space(heading).casefold())
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"party-{digest[:12]}"


def _original_filename(url: str) -> str:
    name = unicodedata.normalize(
        "NFC", urllib.parse.unquote(Path(urllib.parse.urlsplit(url).path).name)
    )
    name = "".join("_" if ord(char) < 32 or char in "/\\" else char for char in name)
    name = name.strip().strip(".")
    if not name.casefold().endswith(".pdf"):
        name += ".pdf"
    return name or "declaration.pdf"


def _body(store: ResponseStore, record: dict[str, Any]) -> bytes:
    return (store.root / str(record["body_path"])).read_bytes()


def _source(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "requestedUrl": record.get("requested_url"),
        "finalUrl": record.get("final_url", record.get("requested_url")),
        "retrievedAt": record.get("retrieved_at"),
        "status": record.get("status"),
        "contentType": record.get("content_type"),
        "sha256": record.get("sha256"),
        "byteLength": record.get("byte_length"),
        "provenance": record.get("provenance", "live-official"),
    }


def _require_success(record: dict[str, Any], label: str) -> None:
    status = int(record.get("status", 0))
    if not 200 <= status < 300 or not record.get("body_path"):
        error = record.get("error") or f"HTTP {status}"
        raise RuntimeError(f"could not fetch {label}: {error}")


def crawl(
    *,
    page_url: str = PAGE_URL,
    raw_dir: Path = DEFAULT_RAW_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    refresh: bool = False,
    minimum_documents: int = MINIMUM_EXPECTED_DOCUMENTS,
    rate: float = 2.0,
    timeout: float = 60.0,
    retries: int = 5,
    store: ResponseStore | None = None,
    fetcher: Any | None = None,
) -> dict[str, Any]:
    store = store or ResponseStore(raw_dir)
    if fetcher is None:
        proxy_url = configured_proxy_url()
        if not proxy_url:
            raise RuntimeError("PROPER_DATA_PROXY_URL is required for this crawl")
        fetcher = Fetcher(
            store,
            GlobalRateLimiter(rate),
            FetchConfig(timeout=timeout, retries=retries),
            client=RequestsOpener(proxy_url),
        )

    page_record = fetcher.fetch(page_url, refresh=refresh)
    _require_success(page_record, "CEC declaration page")
    page_payload = _body(store, page_record)
    discovered = parse_declaration_page(page_payload, page_url)
    if len(discovered) < minimum_documents:
        raise RuntimeError(
            f"CEC page exposed {len(discovered)} declarations; "
            f"expected at least {minimum_documents}"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write(output_dir / "source.html", page_payload)

    documents: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for item in discovered:
        try:
            record = fetcher.fetch(item["url"], refresh=refresh)
            _require_success(record, item["url"])
            payload = _body(store, record)
            digest = sha256_bytes(payload)
            if digest != record.get("sha256"):
                raise RuntimeError("raw-store SHA-256 mismatch")
            if not payload.startswith(b"%PDF-"):
                raise RuntimeError("response does not have PDF magic")
            party_id = _party_id(item["party"])
            relative_path = (
                Path("files") / party_id / item["category"] / f"{digest}.pdf"
            )
            atomic_write(output_dir / relative_path, payload)
            documents.append(
                {
                    "partyId": party_id,
                    "party": item["party"],
                    "category": item["category"],
                    "title": item["linkText"],
                    "originalFilename": _original_filename(item["url"]),
                    "url": item["url"],
                    "path": relative_path.as_posix(),
                    "sha256": digest,
                    "byteLength": len(payload),
                    "pdfMagicValid": True,
                    "source": _source(record),
                }
            )
        except Exception as error:  # noqa: BLE001 - keep an auditable partial index
            failures.append({**item, "error": f"{type(error).__name__}: {error}"})

    documents.sort(key=lambda value: (value["party"], value["category"], value["url"]))
    parties: list[dict[str, Any]] = []
    for heading in sorted({item["party"] for item in discovered}):
        grouped = [item for item in documents if item["party"] == heading]
        parties.append(
            {
                "partyId": _party_id(heading),
                "heading": heading,
                "documentCount": len(grouped),
                "categories": dict(
                    sorted(Counter(item["category"] for item in grouped).items())
                ),
            }
        )

    category_counts = Counter(item["category"] for item in documents)
    index = {
        "schemaVersion": 1,
        "dataset": "cik-2026-duma-federal-party-list-declarations",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "sourcePage": {
            **_source(page_record),
            "path": "source.html",
        },
        "categories": {
            CATEGORY_INCOME_PROPERTY: "income and property",
            CATEGORY_FOREIGN_PROPERTY: "foreign property and obligations",
            CATEGORY_LARGE_TRANSACTIONS: "large transactions and funding sources",
        },
        "parties": parties,
        "documents": documents,
        "failures": failures,
        "completeness": {
            "minimumExpected": minimum_documents,
            "discovered": len(discovered),
            "downloaded": len(documents),
            "validPdf": sum(item["pdfMagicValid"] for item in documents),
            "failed": len(failures),
            "complete": len(documents) == len(discovered) and not failures,
            "categoryCounts": {
                category: category_counts.get(category, 0) for category in CATEGORIES
            },
        },
    }
    json_write(output_dir / "index.json", index)
    store.flush()
    if failures:
        raise RuntimeError(
            f"downloaded {len(documents)}/{len(discovered)} declarations; "
            f"see {output_dir / 'index.json'}"
        )
    return index


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Crawl the CEC's 2026 Duma federal party-list declarations."
    )
    parser.add_argument("--page-url", default=PAGE_URL)
    parser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--minimum-documents", type=int, default=MINIMUM_EXPECTED_DOCUMENTS
    )
    parser.add_argument("--rate", type=float, default=2.0)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--refresh", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    index = crawl(
        page_url=args.page_url,
        raw_dir=args.raw_dir,
        output_dir=args.output_dir,
        refresh=args.refresh,
        minimum_documents=args.minimum_documents,
        rate=args.rate,
        timeout=args.timeout,
        retries=args.retries,
    )
    completeness = index["completeness"]
    print(
        f"Downloaded {completeness['downloaded']}/{completeness['discovered']} "
        f"valid party-list declaration PDFs to {args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
