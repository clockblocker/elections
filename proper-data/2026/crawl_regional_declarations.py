"""Discover and download 2026 candidate disclosures from regional commissions.

The optional catalog is JSON with this deliberately small schema::

    {
      "schemaVersion": 1,
      "regions": [{
        "code": "77",
        "name": "Moscow",
        "seedUrls": ["https://official.example/elections/2026/"],
        "allowedHosts": ["official.example"],
        "maxPages": 50,
        "maxDepth": 2,
        "trustedSeedContext": false,
        "followPatterns": ["optional case-insensitive regular expression"]
      }]
    }

``allowedHosts`` defaults to the seed hosts. Discovery never follows an HTML page
outside those hosts and is bounded by both depth and page count. The built-in
catalog covers Moscow and St Petersburg, which are useful smoke tests as well as
examples of a district-document and individual-candidate-document layout.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import urllib.parse
import zipfile
from collections import Counter, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

SHARED_CRAWLER_DIR = Path(__file__).resolve().parents[1] / "crawler"
sys.path.insert(0, str(SHARED_CRAWLER_DIR))

from common import atomic_write, decode_text, json_write, sha256_bytes
from shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
from transport import FetchConfig, Fetcher, ResponseStore

DEFAULT_RAW = Path("data/raw/cik-current-2026-regional-declarations")
DEFAULT_OUTPUT = Path("proper-data/2026/declarations/regions")
DOCUMENT_SUFFIXES = frozenset(
    {".pdf", ".xls", ".xlsx", ".doc", ".docx", ".rtf", ".zip"}
)
HTML_SUFFIXES = frozenset(
    {"", ".htm", ".html", ".shtml", ".php", ".asp", ".aspx", ".jsp"}
)
INACCURACY_RE = re.compile(
    r"(?:недостовер|выявлен\w*\s+факт|несоответств)", re.IGNORECASE
)
DECLARATION_RE = re.compile(
    r"(?:сведени\w*\s+о\s+доход|доход\w*\s+и\s+(?:об\s+)?имуществ|"
    r"имуществ\w*\s+(?:зарегистрированн\w*\s+)?кандидат|(?:doxod|dohod|dokhod)|"
    r"dokh(?:od\w*)?[-_/]+(?:i[-_/]+)?imush)",
    re.IGNORECASE,
)
PERSONAL_EXPENSE_RE = re.compile(
    r"расход\w*\s+кандидат\w*.*(?:супруг|несовершеннолетн|по\s+каждой\s+сделк)",
    re.IGNORECASE,
)
ELECTION_FINANCE_RE = re.compile(
    r"(?:избирательн\w*\s+фонд|финансирован\w*\s+выбор|финансов\w*\s+отч[её]т|"
    r"postupleni\w*[-_/]+i[-_/]+raskhodovani\w*[-_/]+sredstv|"
    r"izbiratel\w*[-_/]+fond|finansov\w*[-_/]+otchet)",
    re.IGNORECASE,
)
DISTRICT_RE = re.compile(
    r"(?:одномандат|окружн\w*\s+избирательн|избирательн\w*\s+округ|okrug)",
    re.IGNORECASE,
)
YEAR_2026_RE = re.compile(
    r"(?:2026|20092026)",
    re.IGNORECASE,
)
DUMA_RE = re.compile(
    r"(?:госдум|государственн\w*\s+дум|gosdum|gosdumu|"
    r"gosudarstvenn\w*[-_/]+dum\w*|"
    r"gd[-_/]?(?:9|2026)\b)",
    re.IGNORECASE,
)
ANY_YEAR_RE = re.compile(r"(?<!\d)20\d{2}")
COMPACT_DATE_RE = re.compile(
    r"(?<!\d)(?:0[1-9]|[12]\d|3[01])(?:0[1-9]|1[0-2])(20\d{2})(?!\d)"
)
CANDIDATE_RE = re.compile(r"(?:кандидат|kandidat)", re.IGNORECASE)
STAFF_PATH_RE = re.compile(
    r"(?:protivodejstvi[ey].*korrup|protivodeystviya-korruptsii|/antic(?:/|$))",
    re.IGNORECASE,
)
OTHER_ELECTION_RE = re.compile(
    r"(?:выбор\w*\s+глав|губернатор|региональн\w*\s+парламент|"
    r"законодательн\w*\s+(?:собрани|дум)|муниципальн\w*\s+выбор|"
    r"местн\w*\s+выбор|vybory[-_/]+glav|vybory[-_/]+parlament|"
    r"gubernator|zakonodat|municip|mestn\w*[-_/]+vybor)",
    re.IGNORECASE,
)
ELECTION_FINANCE_PATH_RE = re.compile(
    r"(?:postupleni\w*[-_/]+i[-_/]+raskhodovani\w*[-_/]+sredstv|"
    r"izbiratel\w*[-_/]+fond|finansov\w*[-_/]+otchet)",
    re.IGNORECASE,
)

BUILTIN_CATALOG: dict[str, Any] = {
    "schemaVersion": 1,
    "regions": [
        {
            "code": "77",
            "name": "Москва",
            "seedUrls": ["https://www.mosgorizbirkom.ru/vybory/vibori-v-gosdumu/"],
            "allowedHosts": ["www.mosgorizbirkom.ru"],
            "maxPages": 40,
            "maxDepth": 2,
            "trustedSeedContext": True,
        },
        {
            "code": "78",
            "name": "Санкт-Петербург",
            "seedUrls": [
                "http://www.st-petersburg.izbirkom.ru/edg20092026/doxod_gd9.php"
            ],
            "allowedHosts": ["www.st-petersburg.izbirkom.ru"],
            "maxPages": 20,
            "maxDepth": 2,
            "trustedSeedContext": True,
        },
    ],
}


@dataclass(frozen=True)
class Link:
    text: str
    href: str
    heading: str
    context: str = ""


class LinkParser(HTMLParser):
    """Extract links plus enough local structure to classify terse file labels."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[Link] = []
        self.base_href: str | None = None
        self.title = ""
        self.text = ""
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._heading_level: str | None = None
        self._heading_text: list[str] = []
        self._last_heading = ""
        self._title_open = False
        self._title_text: list[str] = []
        self._all_text: list[str] = []
        self._contexts: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = dict(attrs)
        if tag == "base" and values.get("href"):
            self.base_href = values["href"]
        elif tag == "a" and values.get("href"):
            self._anchor_href = values["href"]
            self._anchor_text = []
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading_level = tag
            self._heading_text = []
        elif tag == "title":
            self._title_open = True
            self._title_text = []
        if tag in {"tr", "li"}:
            self._contexts.append({"tag": tag, "text": [], "links": []})

    def handle_data(self, data: str) -> None:
        self._all_text.append(data)
        for context in self._contexts:
            context["text"].append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._heading_level is not None:
            self._heading_text.append(data)
        if self._title_open:
            self._title_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "a" and self._anchor_href is not None:
            self.links.append(
                Link(
                    text=_clean_text("".join(self._anchor_text)),
                    href=self._anchor_href.strip(),
                    heading=self._last_heading,
                )
            )
            if self._contexts:
                self._contexts[-1]["links"].append(len(self.links) - 1)
            self._anchor_href = None
            self._anchor_text = []
        elif tag == self._heading_level:
            self._last_heading = _clean_text("".join(self._heading_text))
            self._heading_level = None
            self._heading_text = []
        elif tag == "title":
            self.title = _clean_text("".join(self._title_text))
            self._title_open = False
            self._title_text = []
        if self._contexts and tag == self._contexts[-1]["tag"]:
            context = self._contexts.pop()
            text = _clean_text(" ".join(context["text"]))
            for index in context["links"]:
                link = self.links[index]
                self.links[index] = Link(
                    text=link.text,
                    href=link.href,
                    heading=link.heading,
                    context=text,
                )

    def close(self) -> None:
        super().close()
        self.text = _clean_text(" ".join(self._all_text))


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def category_for(value: str) -> str | None:
    """Classify Russian disclosure labels, with inaccuracy taking precedence."""
    if INACCURACY_RE.search(value):
        return "inaccuracy"
    if DECLARATION_RE.search(value) or PERSONAL_EXPENSE_RE.search(value):
        return "declaration"
    return None


def category_signals(value: str) -> set[str]:
    result: set[str] = set()
    if DECLARATION_RE.search(value) or PERSONAL_EXPENSE_RE.search(value):
        result.add("declaration")
    if INACCURACY_RE.search(value):
        result.add("inaccuracy")
    return result


def canonical_url(url: str, base_url: str | None = None) -> str | None:
    raw = url.strip()
    if re.match(r"^https?:/{3,}", raw, re.IGNORECASE):
        return None
    if re.match(r"^[\w.-]+\.(?:ru|рф|com|org|net)/", raw, re.IGNORECASE):
        raw = f"http://{raw}"
    joined = urllib.parse.urljoin(base_url or "", raw)
    parts = urllib.parse.urlsplit(joined)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return None
    return urllib.parse.urlunsplit(
        (
            parts.scheme.casefold(),
            parts.netloc.casefold(),
            parts.path or "/",
            parts.query,
            "",
        )
    )


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").casefold().rstrip(".")


def _host_alias_key(url: str) -> str:
    """Treat www and bare variants of the same official URL as one resource."""
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").casefold()
    host = host.removeprefix("www.")
    if parts.port:
        host = f"{host}:{parts.port}"
    return urllib.parse.urlunsplit(
        (parts.scheme.casefold(), host, parts.path, parts.query, "")
    )


def _suffix(url: str) -> str:
    path = urllib.parse.unquote(urllib.parse.urlsplit(url).path)
    return Path(path).suffix.casefold()


def is_document_url(url: str) -> bool:
    return _suffix(url) in DOCUMENT_SUFFIXES


def is_election_finance_document(value: str) -> bool:
    """Exclude campaign-fund reports, which are not personal disclosures."""
    return bool(ELECTION_FINANCE_RE.search(value))


def _should_follow(
    link: Link,
    url: str,
    *,
    patterns: list[re.Pattern[str]],
) -> bool:
    # A heading can legitimately classify a terse document link, but must not be
    # inherited by every navigation link that follows it in the page template.
    value = f"{link.text} {link.context} {url}"
    if category_for(value) or DISTRICT_RE.search(url) or CANDIDATE_RE.search(url):
        return True
    if DISTRICT_RE.search(link.text) and YEAR_2026_RE.search(value):
        return True
    pattern_matched = any(pattern.search(value) for pattern in patterns)
    # Catalog patterns help with local spelling and route conventions, but a
    # broad year token must not turn an entire /2026/ site tree into scope.
    return bool(
        pattern_matched and (DUMA_RE.search(value) or CANDIDATE_RE.search(value))
    )


def _current_duma_context(value: str) -> bool:
    return bool(YEAR_2026_RE.search(value) and DUMA_RE.search(value))


def _explicitly_historical(value: str) -> bool:
    # Decode URL escapes first: otherwise a space encoded as ``%20`` followed
    # by district 49 looks like the fictitious year ``2049``.
    decoded = urllib.parse.unquote(value)
    compact_years = COMPACT_DATE_RE.findall(decoded)
    without_compact_dates = COMPACT_DATE_RE.sub("", decoded)
    years = [*compact_years, *ANY_YEAR_RE.findall(without_compact_dates)]
    return any(year != "2026" for year in years)


def _explicit_other_election(value: str) -> bool:
    return bool(OTHER_ELECTION_RE.search(value) and not DUMA_RE.search(value))


def _staff_path_allowed(url: str, label: str) -> bool:
    if not STAFF_PATH_RE.search(urllib.parse.urlsplit(url).path):
        return True
    value = f"{label} {url}"
    return bool(_current_duma_context(value) and CANDIDATE_RE.search(value))


def _source(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": record.get("final_url", record["requested_url"]),
        "requestKey": record["requested_url"],
        "retrievedAt": record.get("retrieved_at"),
        "status": record.get("status"),
        "sha256": record.get("sha256"),
        "provenance": record.get("provenance", "live-official"),
    }


def _body(store: ResponseStore, record: dict[str, Any]) -> bytes:
    body_path = record.get("body_path")
    if not body_path:
        return b""
    return (store.root / body_path).read_bytes()


def validate_document(payload: bytes, source_url: str) -> tuple[str, str]:
    """Return a trustworthy extension and media type, rejecting HTML/error pages."""
    if not payload:
        raise ValueError("empty response")
    stripped = payload.lstrip()
    lowered = stripped[:512].lower()
    if lowered.startswith((b"<!doctype html", b"<html", b"<?xml")):
        raise ValueError("response is markup, not a disclosure document")
    if stripped.startswith(b"%PDF-"):
        return ".pdf", "application/pdf"
    if payload.startswith(b"{\\rtf"):
        return ".rtf", "application/rtf"
    if payload.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        extension = _suffix(source_url)
        if extension not in {".xls", ".doc"}:
            extension = ".ole"
        media_type = (
            "application/vnd.ms-excel"
            if extension == ".xls"
            else "application/msword"
            if extension == ".doc"
            else "application/x-ole-storage"
        )
        return extension, media_type
    if payload.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(payload)) as archive:
                names = {name.casefold() for name in archive.namelist()}
        except zipfile.BadZipFile as error:
            raise ValueError("invalid ZIP/Office document") from error
        if "[content_types].xml" in names and any(
            name.startswith("xl/") for name in names
        ):
            return (
                ".xlsx",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        if "[content_types].xml" in names and any(
            name.startswith("word/") for name in names
        ):
            return (
                ".docx",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        return ".zip", "application/zip"
    raise ValueError("unrecognized document signature")


def _file_stem(title: str, category: str, digest: str) -> str:
    readable = re.sub(r"[^\w.-]+", "_", title, flags=re.UNICODE).strip("._")
    readable = readable[:100].rstrip("._") or category
    return f"{readable}-{digest[:12]}"


def _region_config(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise TypeError("each catalog region must be an object")
    code = str(raw.get("code", "")).strip()
    name = str(raw.get("name", "")).strip()
    seeds_raw = raw.get("seedUrls")
    if not code or not name or not isinstance(seeds_raw, list) or not seeds_raw:
        raise ValueError("each region needs code, name, and non-empty seedUrls")
    seeds: list[str] = []
    for raw_url in seeds_raw:
        url = canonical_url(str(raw_url))
        if not url:
            raise ValueError(f"region {code} has an invalid seed URL")
        seeds.append(url)
    hosts_raw = raw.get("allowedHosts")
    hosts = (
        {_host(url) for url in seeds}
        if hosts_raw is None
        else {str(host).casefold().rstrip(".") for host in hosts_raw}
    )
    if not hosts or "" in hosts or any(_host(seed) not in hosts for seed in seeds):
        raise ValueError(f"region {code} seed hosts must be in allowedHosts")
    max_pages = int(raw.get("maxPages", 50))
    max_depth = int(raw.get("maxDepth", 2))
    if not 1 <= max_pages <= 2_000 or not 0 <= max_depth <= 10:
        raise ValueError(f"region {code} has unsafe crawl limits")
    patterns = raw.get("followPatterns", [])
    if not isinstance(patterns, list) or not all(
        isinstance(item, str) for item in patterns
    ):
        raise ValueError(f"region {code} followPatterns must be strings")
    for pattern in patterns:
        re.compile(pattern, re.IGNORECASE)
    trusted_seed_context = raw.get("trustedSeedContext", False)
    if not isinstance(trusted_seed_context, bool):
        raise TypeError(f"region {code} trustedSeedContext must be a boolean")
    return {
        "code": code,
        "name": name,
        "seedUrls": list(dict.fromkeys(seeds)),
        "allowedHosts": sorted(hosts),
        "maxPages": max_pages,
        "maxDepth": max_depth,
        "trustedSeedContext": trusted_seed_context,
        "followPatterns": patterns,
    }


def load_catalog(path: Path | None) -> list[dict[str, Any]]:
    value = (
        BUILTIN_CATALOG
        if path is None
        else json.loads(path.read_text(encoding="utf-8"))
    )
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("catalog schemaVersion must be 1")
    regions = value.get("regions")
    if not isinstance(regions, list) or not regions:
        raise ValueError("catalog regions must be a non-empty array")
    result = [_region_config(region) for region in regions]
    codes = [region["code"] for region in result]
    if len(codes) != len(set(codes)):
        raise ValueError("catalog contains duplicate region codes")
    return result


def _publish_index(
    destination: Path,
    config: dict[str, Any],
    *,
    max_pages: int,
    max_depth: int,
    pages: list[dict[str, Any]],
    downloaded: list[dict[str, Any]],
    page_failures: list[dict[str, Any]],
    document_failures: list[dict[str, Any]],
    truncated: bool,
    crawl_in_progress: bool,
) -> dict[str, Any]:
    counts = Counter(document["category"] for document in downloaded)
    signal_counts = Counter(
        category for page in pages for category in page["categorySignals"]
    )
    empty_counts = Counter(
        category for page in pages for category in page["explicitEmptyCategories"]
    )
    complete = (
        not crawl_in_progress
        and not page_failures
        and not document_failures
        and not truncated
    )
    result = {
        "schemaVersion": 1,
        "retrievedAt": datetime.now(timezone.utc).isoformat(),
        "region": {
            "code": config["code"],
            "name": config["name"],
            "seedUrls": config["seedUrls"],
            "allowedHosts": config["allowedHosts"],
        },
        "limits": {"maxPages": max_pages, "maxDepth": max_depth},
        "crawlInProgress": crawl_in_progress,
        "complete": complete,
        "truncated": truncated,
        "pageCount": len(pages),
        "documentCount": len(downloaded),
        "documentCounts": dict(sorted(counts.items())),
        "categoryCoverage": {
            category: {
                "signalPageCount": signal_counts[category],
                "explicitEmptyPageCount": empty_counts[category],
                "documentCount": counts[category],
            }
            for category in ("declaration", "inaccuracy")
        },
        "failures": {
            "pages": page_failures,
            "documents": document_failures,
            "runner": [],
        },
        "pages": pages,
        "documents": sorted(
            downloaded, key=lambda item: (item["category"], item["officialUrl"])
        ),
    }
    json_write(destination / "index.json", result)
    return result


def crawl_region(
    config: dict[str, Any],
    fetcher: Fetcher,
    output_root: Path,
    *,
    refresh: bool = False,
    max_pages_override: int | None = None,
) -> dict[str, Any]:
    """Crawl one configured region and publish its index plus validated files."""
    code = config["code"]
    destination = output_root / code
    files_dir = destination / "files"
    allowed_hosts = set(config["allowedHosts"])
    max_pages = max_pages_override or config["maxPages"]
    max_depth = config["maxDepth"]
    patterns = [
        re.compile(pattern, re.IGNORECASE) for pattern in config["followPatterns"]
    ]
    pending: deque[tuple[str, int, bool]] = deque(
        (url, 0, config["trustedSeedContext"]) for url in config["seedUrls"]
    )
    queued = set(config["seedUrls"])
    visited: set[str] = set()
    pages: list[dict[str, Any]] = []
    page_failures: list[dict[str, Any]] = []
    documents: dict[str, dict[str, Any]] = {}
    downloaded: list[dict[str, Any]] = []
    document_failures: list[dict[str, Any]] = []
    checkpoint = {
        "destination": destination,
        "config": config,
        "max_pages": max_pages,
        "max_depth": max_depth,
        "pages": pages,
        "downloaded": downloaded,
        "page_failures": page_failures,
        "document_failures": document_failures,
        "truncated": False,
    }
    _publish_index(**checkpoint, crawl_in_progress=True)

    while pending and len(visited) < max_pages:
        url, depth, inherited_context = pending.popleft()
        if url in visited:
            continue
        visited.add(url)
        record = fetcher.fetch(url, refresh=refresh)
        status = int(record.get("status", 0))
        if not 200 <= status < 400 or not record.get("body_path"):
            page_failures.append(
                {
                    "url": url,
                    "depth": depth,
                    "status": status or None,
                    "error": record.get("error", f"HTTP {status}"),
                }
            )
            _publish_index(**checkpoint, crawl_in_progress=True)
            continue
        final_url = str(record.get("final_url", url))
        if _host(final_url) not in allowed_hosts:
            page_failures.append(
                {
                    "url": url,
                    "depth": depth,
                    "status": status,
                    "error": f"redirected outside allowed hosts to {_host(final_url)}",
                }
            )
            _publish_index(**checkpoint, crawl_in_progress=True)
            continue
        payload = _body(fetcher.store, record)
        content_type = str(record.get("content_type", "")).casefold()
        if "html" not in content_type and not payload.lstrip().lower().startswith(
            (b"<!doctype", b"<html")
        ):
            page_failures.append(
                {"url": url, "depth": depth, "error": "seed/page is not HTML"}
            )
            _publish_index(**checkpoint, crawl_in_progress=True)
            continue
        html, encoding = decode_text(payload)
        parser = LinkParser()
        parser.feed(html)
        parser.close()
        # Relative links belong to the response URL after redirects (not the
        # originally requested spelling, which often omits a trailing slash).
        base_url = canonical_url(parser.base_href or final_url, final_url) or final_url
        page_context = inherited_context or _current_duma_context(
            f"{parser.title} {url}"
        )
        page_category = category_for(f"{parser.title} {url}")
        signals = category_signals(f"{parser.text} {url}")
        linked_categories: Counter[str] = Counter()
        page_document_urls: set[str] = set()

        for link in parser.links:
            target = canonical_url(link.href, base_url)
            if not target or _host(target) not in allowed_hosts:
                continue
            link_value = f"{link.text} {link.href} {target}"
            if _explicitly_historical(link_value):
                continue
            document_target = is_document_url(target)
            if not document_target and _explicit_other_election(link_value):
                continue
            target_context = page_context or _current_duma_context(link_value)
            if not target_context or not _staff_path_allowed(target, link.text):
                continue
            direct_category = category_for(f"{link.text} {link.href}")
            context_category = category_for(link.context)
            local_category = category_for(link.heading)
            category = (
                direct_category or context_category or local_category or page_category
            )
            if document_target:
                if category != "inaccuracy" and (
                    is_election_finance_document(
                        f"{link.text} {link.context} {link.heading}"
                    )
                    or is_election_finance_document(f"{parser.title} {url}")
                ):
                    continue
                if category is None:
                    continue
                scope_warnings = []
                if ELECTION_FINANCE_PATH_RE.search(urllib.parse.urlsplit(target).path):
                    scope_warnings.append(
                        "target URL is under an election-finance path; retained because its source context identifies a personal disclosure"
                    )
                if OTHER_ELECTION_RE.search(urllib.parse.urlsplit(target).path):
                    scope_warnings.append(
                        "target URL is under another-election path; retained because its source context is the 2026 State Duma election"
                    )
                linked_categories[category] += 1
                page_document_urls.add(target)
                document_key = _host_alias_key(target)
                existing = documents.get(document_key)
                source_page = {
                    "url": url,
                    "title": parser.title,
                    "depth": depth,
                    "sha256": record.get("sha256"),
                }
                if existing:
                    if (
                        existing["category"] != "inaccuracy"
                        and category == "inaccuracy"
                    ):
                        existing["category"] = category
                    if source_page not in existing["sourcePages"]:
                        existing["sourcePages"].append(source_page)
                    existing["scopeWarnings"] = sorted(
                        set(existing["scopeWarnings"] + scope_warnings)
                    )
                else:
                    documents[document_key] = {
                        "category": category,
                        "title": link.text
                        or link.heading
                        or Path(urllib.parse.urlsplit(target).path).name,
                        "officialUrl": target,
                        "sourcePages": [source_page],
                        "scopeWarnings": scope_warnings,
                    }
                continue
            if _suffix(target) not in HTML_SUFFIXES:
                continue
            if (
                depth < max_depth
                and target not in queued
                and (
                    _current_duma_context(link_value)
                    or _should_follow(link, target, patterns=patterns)
                )
            ):
                queued.add(target)
                pending.append((target, depth + 1, target_context))

        pages.append(
            {
                "url": url,
                "title": parser.title,
                "depth": depth,
                "electionContextConfirmed": page_context,
                "encoding": encoding,
                "categorySignals": sorted(signals),
                "linkedDocumentCounts": dict(sorted(linked_categories.items())),
                "explicitEmptyCategories": sorted(
                    category for category in signals if linked_categories[category] == 0
                ),
                "discoveredDocumentUrls": sorted(page_document_urls),
                "source": _source(record),
            }
        )
        _publish_index(**checkpoint, crawl_in_progress=True)

    truncated = bool(pending)
    checkpoint["truncated"] = truncated
    _publish_index(**checkpoint, crawl_in_progress=True)
    for _, document in sorted(documents.items()):
        url = document["officialUrl"]
        record = fetcher.fetch(url, refresh=refresh)
        status = int(record.get("status", 0))
        if not 200 <= status < 400 or not record.get("body_path"):
            document_failures.append(
                {
                    "url": url,
                    "category": document["category"],
                    "status": status or None,
                    "error": record.get("error", f"HTTP {status}"),
                }
            )
            _publish_index(**checkpoint, crawl_in_progress=True)
            continue
        final_url = str(record.get("final_url", url))
        if _host(final_url) not in allowed_hosts:
            document_failures.append(
                {
                    "url": url,
                    "category": document["category"],
                    "status": status,
                    "error": f"redirected outside allowed hosts to {_host(final_url)}",
                }
            )
            _publish_index(**checkpoint, crawl_in_progress=True)
            continue
        payload = _body(fetcher.store, record)
        try:
            extension, media_type = validate_document(payload, url)
        except ValueError as error:
            document_failures.append(
                {
                    "url": url,
                    "category": document["category"],
                    "status": status,
                    "error": str(error),
                    "source": _source(record),
                }
            )
            _publish_index(**checkpoint, crawl_in_progress=True)
            continue
        digest = sha256_bytes(payload)
        filename = (
            _file_stem(document["title"], document["category"], digest) + extension
        )
        path = files_dir / filename
        if not path.exists() or sha256_bytes(path.read_bytes()) != digest:
            atomic_write(path, payload)
        downloaded.append(
            {
                **document,
                "documentId": digest,
                "originalFilename": urllib.parse.unquote(
                    Path(urllib.parse.urlsplit(url).path).name
                ),
                "filename": filename,
                "path": str((Path("files") / filename).as_posix()),
                "mediaType": media_type,
                "byteLength": len(payload),
                "sha256": digest,
                "source": _source(record),
            }
        )
        _publish_index(**checkpoint, crawl_in_progress=True)

    return _publish_index(**checkpoint, crawl_in_progress=False)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Crawl regional 2026 candidate disclosures through the configured proxy.",
        epilog="Catalog schema is documented in the module docstring; omit --catalog to crawl Moscow and St Petersburg.",
    )
    result.add_argument("--catalog", type=Path)
    result.add_argument(
        "--region", action="append", help="region code to crawl; repeatable"
    )
    result.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    result.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    result.add_argument("--refresh", action="store_true")
    result.add_argument("--max-pages", type=int, help="temporary per-region safety cap")
    result.add_argument("--rate", type=float, default=3.0)
    result.add_argument("--jitter", type=float, default=0.1)
    result.add_argument("--timeout", type=float, default=30.0)
    result.add_argument("--retries", type=int, default=4)
    result.add_argument("--backoff-initial", type=float, default=0.5)
    result.add_argument("--backoff-max", type=float, default=30.0)
    result.add_argument(
        "--coordination-dir", type=Path, default=DEFAULT_COORDINATION_DIR
    )
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.max_pages is not None and not 1 <= args.max_pages <= 2_000:
        raise SystemExit("--max-pages must be between 1 and 2000")
    regions = load_catalog(args.catalog)
    requested = set(args.region or [])
    if requested:
        known = {region["code"] for region in regions}
        unknown = sorted(requested - known)
        if unknown:
            raise SystemExit(f"unknown region code(s): {', '.join(unknown)}")
        regions = [region for region in regions if region["code"] in requested]
    store = ResponseStore(args.raw_dir)
    limiter = SharedRateLimiter(
        args.rate,
        coordination_dir=args.coordination_dir,
        jitter=args.jitter,
    )
    fetcher = Fetcher(
        store,
        limiter,
        FetchConfig(
            timeout=args.timeout,
            retries=args.retries,
            backoff_initial=args.backoff_initial,
            backoff_max=args.backoff_max,
        ),
    )
    results: list[dict[str, Any]] = []
    try:
        for region in regions:
            print(f"regional disclosures {region['code']} {region['name']}", flush=True)
            try:
                result = crawl_region(
                    region,
                    fetcher,
                    args.output,
                    refresh=args.refresh,
                    max_pages_override=args.max_pages,
                )
            except Exception as error:  # noqa: BLE001 - continue other regions
                index_path = args.output / region["code"] / "index.json"
                if index_path.is_file():
                    result = json.loads(index_path.read_text(encoding="utf-8"))
                else:
                    result = {
                        "schemaVersion": 1,
                        "region": {"code": region["code"], "name": region["name"]},
                        "pageCount": 0,
                        "documentCount": 0,
                        "documentCounts": {},
                        "failures": {"pages": [], "documents": [], "runner": []},
                        "pages": [],
                        "documents": [],
                    }
                result["crawlInProgress"] = False
                result["complete"] = False
                result.setdefault("failures", {}).setdefault("runner", []).append(
                    {"error": f"{type(error).__name__}: {error}"}
                )
                json_write(index_path, result)
            results.append(result)
            print(
                f"  pages={result['pageCount']}; documents={result['documentCount']}; "
                f"declarations={result['documentCounts'].get('declaration', 0)}; "
                f"inaccuracies={result['documentCounts'].get('inaccuracy', 0)}; "
                f"complete={result['complete']}",
                flush=True,
            )
    finally:
        store.flush()
    return 0 if all(result["complete"] for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
