"""Bounded discovery and conservative ingestion of regional commission contacts.

The 2026 regional declaration catalog is reused as the official-site allowlist.  It
does *not* make declaration pages contact sources: this crawler starts from each
official base URL, adds bounded site-search requests, and follows only contact/UIK/
TIK-looking links on catalog-approved hosts.

Every successful response is preserved byte-for-byte in a content-addressed store.
Only CSV, JSON, simple HTML tables, and explicit labelled HTML contact blocks are
parsed.  PDF/Office/ambiguous artifacts remain in the manifest as unresolved input
for a future adapter or manual review.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import threading
import urllib.parse
from collections import Counter, deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

import requests

from .io import write_jsonl
from .models import CommissionContact, SourceEvidence, canonical_region_code
from .office_documents import parse_docx_precinct_rows, parse_xlsx_precinct_rows
from .regional_adapters import parse_html as parse_html_adapter
from .regional_adapters import seed_urls as adapter_seed_urls

DEFAULT_SEARCH_TERMS = (
    "территориальные избирательные комиссии адреса телефоны",
    "участковые избирательные комиссии адреса",
    "адреса помещений для голосования",
    "найти избирательный участок",
)
DEFAULT_FOLLOW_PATTERNS = (
    r"(?:избирательн\w*\s+комис|/special/ik\.php)",
    r"(?:территориальн\w*\s+избирательн\w*\s+комис|(?:^|\W)тик(?:\W|$))",
    r"(?:участков\w*\s+избирательн\w*\s+комис|(?:^|\W)уик(?:\W|$))",
    r"(?:избирательн\w*\s+участ|помещени\w*\s+для\s+голосован)",
    (
        r"(?:polling[-_/ ]?(?:place|station)|izbiratel\w*[-_/ ]+uchast|"
        r"(?:tik|uik)[-_/ ]+(?:address|contact)|uchastkov\w*[-_/ ]+komiss)"
    ),
)
DOCUMENT_SUFFIXES = frozenset(
    {".csv", ".json", ".pdf", ".xls", ".xlsx", ".doc", ".docx", ".rtf", ".ods", ".zip"}
)
HTML_SUFFIXES = frozenset({"", ".htm", ".html", ".shtml", ".php", ".asp", ".aspx", ".jsp"})
_UIK_RE = re.compile(
    r"(?:участков\w*\s+избирательн\w*\s+комис(?:сия|сии|сию)?|(?:^|\W)уик)"
    r"\s*(?:№|N|no\.?|номер)?\s*(\d+)",
    re.IGNORECASE,
)
_TIK_RE = re.compile(
    r"(?:территориальн\w*\s+избирательн\w*\s+комис(?:сия|сии|сию)?|(?:^|\W)тик(?:\W|$))",
    re.IGNORECASE,
)
_STORE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class RegionSource:
    code: str
    name: str
    base_url: str
    seed_urls: tuple[str, ...]
    allowed_hosts: tuple[str, ...]
    max_pages: int
    max_depth: int


@dataclass(frozen=True, slots=True)
class FetchResponse:
    url: str
    status: int
    body: bytes
    content_type: str = ""
    retrieved_at: str = ""


@dataclass(frozen=True, slots=True)
class ParseOutcome:
    contacts: tuple[CommissionContact, ...]
    parser: str
    status: str
    reason: str = ""


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResponse: ...


class RequestsFetcher:
    """Small thread-safe requests transport used by :func:`run_regional_crawl`."""

    def __init__(
        self,
        *,
        proxy_url: str | None = None,
        timeout: float = 30.0,
        concurrency: int = 6,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if not 1 <= concurrency <= 32:
            raise ValueError("concurrency must be between 1 and 32")
        self.timeout = timeout
        self.proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        self._limit = threading.BoundedSemaphore(concurrency)

    def fetch(self, url: str) -> FetchResponse:
        with self._limit:
            response = requests.get(
                url,
                timeout=self.timeout,
                proxies=self.proxies,
                headers={"User-Agent": "uik-address/0.1 (official-source archival crawler)"},
            )
        return FetchResponse(
            url=response.url,
            status=response.status_code,
            body=response.content,
            content_type=response.headers.get("content-type", ""),
            retrieved_at=datetime.now(UTC).isoformat(),
        )


@dataclass(frozen=True, slots=True)
class _Link:
    href: str
    text: str


class _HTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[_Link] = []
        self.tables: list[list[list[str]]] = []
        self.base_href: str | None = None
        self.title = ""
        self._text: list[str] = []
        self._anchor_href: str | None = None
        self._anchor_text: list[str] = []
        self._title_open = False
        self._title_text: list[str] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        values = dict(attrs)
        if tag in {"script", "style", "noscript"}:
            self._ignored += 1
            return
        if self._ignored:
            return
        if tag == "base" and values.get("href"):
            self.base_href = values["href"]
        elif tag == "a" and values.get("href"):
            self._anchor_href = values["href"]
            self._anchor_text = []
        elif tag == "title":
            self._title_open = True
            self._title_text = []
        elif tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        if tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}:
            self._text.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        self._text.append(data)
        if self._anchor_href is not None:
            self._anchor_text.append(data)
        if self._title_open:
            self._title_text.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in {"script", "style", "noscript"} and self._ignored:
            self._ignored -= 1
            return
        if self._ignored:
            return
        if tag == "a" and self._anchor_href is not None:
            self.links.append(_Link(self._anchor_href.strip(), _clean("".join(self._anchor_text))))
            self._anchor_href = None
            self._anchor_text = []
        elif tag == "title":
            self.title = _clean("".join(self._title_text))
            self._title_open = False
        elif tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None
        if tag in {"p", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}:
            self._text.append("\n")

    @property
    def text(self) -> str:
        lines = (_clean(line) for line in "".join(self._text).splitlines())
        return "\n".join(line for line in lines if line)


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _key(value: object) -> str:
    return re.sub(r"[^a-zа-я0-9]+", " ", _clean(value).casefold().replace("ё", "е")).strip()


def _host(url: str) -> str:
    return (urllib.parse.urlsplit(url).hostname or "").casefold().rstrip(".")


def canonical_url(value: str, base_url: str | None = None) -> str | None:
    raw = value.strip()
    if not raw or raw.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
        return None
    joined = urllib.parse.urljoin(base_url or "", raw)
    parts = urllib.parse.urlsplit(joined)
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        return None
    return urllib.parse.urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path or "/", parts.query, "")
    )


def _suffix(url: str) -> str:
    return Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).suffix.casefold()


def load_catalog(path: Path) -> list[RegionSource]:
    """Load and validate the 89-region official-site catalog."""

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schemaVersion") != 1:
        raise ValueError("catalog schemaVersion must be 1")
    raw_regions = value.get("regions")
    if not isinstance(raw_regions, list) or not raw_regions:
        raise ValueError("catalog regions must be a non-empty array")
    result: list[RegionSource] = []
    for raw in raw_regions:
        if not isinstance(raw, dict):
            raise TypeError("catalog region must be an object")
        code = canonical_region_code(raw.get("regionCode") or raw.get("code"))
        name = _clean(raw.get("regionName") or raw.get("name"))
        base = canonical_url(str(raw.get("baseUrl") or ""))
        seeds = [canonical_url(str(item)) for item in raw.get("seedUrls", [])]
        seeds = [item for item in seeds if item]
        if not base and seeds:
            first = urllib.parse.urlsplit(seeds[0])
            base = urllib.parse.urlunsplit((first.scheme, first.netloc, "/", "", ""))
        if not code or not name or not base:
            raise ValueError("each catalog region needs code, name, and baseUrl or seedUrls")
        hosts_raw = raw.get("allowedHosts")
        hosts = (
            {_host(base), *(_host(seed) for seed in seeds)}
            if hosts_raw is None
            else {_clean(host).casefold().rstrip(".") for host in hosts_raw}
        )
        if (
            "" in hosts
            or _host(base) not in hosts
            or any(_host(seed) not in hosts for seed in seeds)
        ):
            raise ValueError(f"region {code} seed/base hosts must be in allowedHosts")
        max_pages = int(raw.get("maxPages", 50))
        max_depth = int(raw.get("maxDepth", 2))
        if not 1 <= max_pages <= 2_000 or not 0 <= max_depth <= 10:
            raise ValueError(f"region {code} has unsafe crawl limits")
        result.append(
            RegionSource(
                code=code,
                name=name,
                base_url=base,
                seed_urls=tuple(dict.fromkeys([base, *seeds])),
                allowed_hosts=tuple(sorted(hosts)),
                max_pages=max_pages,
                max_depth=max_depth,
            )
        )
    codes = [region.code for region in result]
    if len(codes) != len(set(codes)):
        raise ValueError("catalog contains duplicate region codes")
    return sorted(
        result,
        key=lambda region: (int(region.code) if region.code.isdigit() else 10_000, region.code),
    )


_ALIASES = {
    "name": {
        "наименование",
        "наименование комиссии",
        "наименование избирательной комиссии",
        "комиссия",
        "тик",
        "уик",
        "commission",
        "commission name",
        "name",
    },
    "type": {"тип комиссии", "вид комиссии", "уровень комиссии", "commission type", "type"},
    "number": {
        "номер уик",
        "уик номер",
        "номер тик",
        "тик номер",
        "номер комиссии",
        "номер избирательного участка",
        "номер участка",
        "commission number",
    },
    "external_id": {"идентификатор", "ид комиссии", "uuid", "external id", "id"},
    "commission_address": {
        "адрес комиссии",
        "адрес тик",
        "адрес уик",
        "место нахождения комиссии",
        "место нахождения избирательной комиссии",
        "commission address",
    },
    "commission_phone": {
        "телефон комиссии",
        "телефон тик",
        "телефон уик",
        "номер телефона комиссии",
        "commission phone",
        "phone",
    },
    "voting_address": {
        "адрес помещения для голосования",
        "место голосования",
        "адрес места голосования",
        "адрес избирательного участка",
        "voting address",
        "polling place address",
    },
    "voting_phone": {
        "телефон помещения для голосования",
        "телефон места голосования",
        "телефон избирательного участка",
        "voting phone",
        "polling place phone",
    },
}


def _columns(headers: Sequence[object]) -> dict[str, int]:
    normalized = [_key(header).removeprefix("№ ") for header in headers]
    result: dict[str, int] = {}
    for field, aliases in _ALIASES.items():
        for index, header in enumerate(normalized):
            if header in aliases:
                result[field] = index
                break
    # The number sign is commonly the whole header, but is only safe when a
    # different header explicitly establishes UIK/TIK identity.
    if "number" not in result and any(header in {"№", "номер", "n"} for header in normalized):
        context = " ".join(normalized)
        if re.search(r"(?:уик|тик|избирательн\w* участ)", context, re.IGNORECASE):
            result["number"] = next(
                index for index, header in enumerate(normalized) if header in {"№", "номер", "n"}
            )
    return result


def _commission_type(name: str, explicit: str, headers: Sequence[object]) -> str:
    context = f"{name} {explicit} {' '.join(map(str, headers))}"
    if _UIK_RE.search(context) or re.search(r"(?:^|\W)уик(?:\W|$)", context, re.IGNORECASE):
        return "uik"
    if _TIK_RE.search(context):
        return "tik"
    return ""


def _int(value: object) -> int | None:
    match = re.search(r"\d+", _clean(value))
    return int(match.group()) if match else None


def _uik_number_from_identity(value: str) -> int | None:
    match = _UIK_RE.search(value)
    if not match:
        return None
    # Archive navigation often says "формирование УИК 2013 года".  The year
    # is not a commission number and must never join an actual UIK 2013.
    tail = value[match.end() :]
    if re.match(r"\s*г(?:од(?:а|у|ом|е)?|\.)\b", tail, re.IGNORECASE):
        return None
    return int(match.group(1))


def _publishable(value: object, *, phone: bool = False) -> str:
    """Reject search-result fragments and placeholders as contact values."""

    text = _clean(value)
    if not text or "..." in text or "…" in text:
        return ""
    if phone and sum(character.isdigit() for character in text) < 5:
        return ""
    return text


def _record_contact(
    record: Sequence[object],
    headers: Sequence[object],
    columns: Mapping[str, int],
    *,
    subject_code: str,
    source: SourceEvidence,
) -> CommissionContact | None:
    def get(field: str) -> str:
        index = columns.get(field)
        return _clean(record[index]) if index is not None and index < len(record) else ""

    name = _publishable(get("name"))
    kind = _commission_type(name, get("type"), headers)
    if not kind:
        return None
    identity_context = f"{name} {get('type')}"
    number = _int(get("number"))
    if number is None:
        number = _uik_number_from_identity(identity_context)
    if kind == "uik" and number is None:
        return None
    if not name:
        if kind == "uik" and number is not None:
            name = f"УИК №{number}"
        else:
            return None
    values = {
        "commission_address": _publishable(get("commission_address")),
        "commission_phone": _publishable(get("commission_phone"), phone=True),
        "voting_address": _publishable(get("voting_address")),
        "voting_phone": _publishable(get("voting_phone"), phone=True),
    }
    if not any(values.values()):
        return None
    return CommissionContact(
        subject_code=subject_code,
        commission_type=kind,
        commission_number=number,
        commission_name=name,
        external_id=get("external_id"),
        source=source,
        **values,
    )


def _records_contacts(
    rows: Sequence[Sequence[object]], *, subject_code: str, source: SourceEvidence
) -> list[CommissionContact]:
    if len(rows) < 2:
        return []
    headers = rows[0]
    columns = _columns(headers)
    if not columns.keys() & {
        "commission_address",
        "commission_phone",
        "voting_address",
        "voting_phone",
    }:
        return []
    return [
        contact
        for row in rows[1:]
        if (
            contact := _record_contact(
                row, headers, columns, subject_code=subject_code, source=source
            )
        )
    ]


def _decode(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1251"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            pass
    return payload.decode("utf-8", errors="replace")


def _csv_contacts(
    payload: bytes, *, subject_code: str, source: SourceEvidence
) -> list[CommissionContact]:
    text = _decode(payload)
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [[_clean(cell) for cell in row] for row in csv.reader(io.StringIO(text), dialect)]
    rows = [row for row in rows if any(row)]
    return _records_contacts(rows, subject_code=subject_code, source=source)


def _json_records(value: object) -> list[Mapping[str, object]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if not isinstance(value, Mapping):
        return []
    own_keys = {_key(key) for key in value}
    aliases = set().union(*_ALIASES.values())
    if own_keys & aliases:
        return [value]
    for key in ("data", "items", "results", "records", "commissions"):
        child = value.get(key)
        records = _json_records(child)
        if records:
            return records
    return []


def _json_contacts(
    payload: bytes, *, subject_code: str, source: SourceEvidence
) -> list[CommissionContact]:
    records = _json_records(json.loads(_decode(payload)))
    if not records:
        return []
    headers = sorted({str(key) for record in records for key in record})
    rows: list[list[object]] = [headers]
    rows.extend([record.get(header, "") for header in headers] for record in records)
    return _records_contacts(rows, subject_code=subject_code, source=source)


def _xlsx_contacts(
    payload: bytes, *, url: str, subject_code: str, source: SourceEvidence
) -> list[CommissionContact]:
    return [
        CommissionContact(
            subject_code=subject_code,
            commission_type="uik",
            commission_number=row.number,
            commission_name=f"УИК №{row.number}",
            external_id="",
            commission_address=row.commission_address,
            commission_phone=row.commission_phone,
            voting_address=row.voting_address,
            voting_phone=row.voting_phone,
            source=source,
        )
        for row in parse_xlsx_precinct_rows(payload, url=url)
    ]


def _docx_contacts(
    payload: bytes, *, url: str, subject_code: str, source: SourceEvidence
) -> list[CommissionContact]:
    return [
        CommissionContact(
            subject_code=subject_code,
            commission_type="uik",
            commission_number=row.number,
            commission_name=f"УИК №{row.number}",
            external_id="",
            commission_address=row.commission_address,
            commission_phone=row.commission_phone,
            voting_address=row.voting_address,
            voting_phone=row.voting_phone,
            source=source,
        )
        for row in parse_docx_precinct_rows(payload, url=url)
    ]


def _label_value(lines: Sequence[str], aliases: Sequence[str]) -> str:
    pattern = re.compile(
        rf"^(?:{'|'.join(map(re.escape, aliases))})\s*[:—-]\s*(.+)$", re.IGNORECASE
    )
    for line in lines:
        match = pattern.match(line)
        if match:
            return _clean(match.group(1))
    return ""


def _labelled_html_contacts(
    text: str, *, subject_code: str, source: SourceEvidence
) -> list[CommissionContact]:
    lines = [line for line in text.splitlines() if line]
    identities = [
        index for index, line in enumerate(lines) if _UIK_RE.search(line) or _TIK_RE.search(line)
    ]
    contacts: list[CommissionContact] = []
    for position, start in enumerate(identities):
        end = (
            identities[position + 1]
            if position + 1 < len(identities)
            else min(len(lines), start + 12)
        )
        block = lines[start:end]
        name = _publishable(lines[start])
        if not name or len(name) > 200:
            continue
        uik_number = _uik_number_from_identity(name)
        if _UIK_RE.search(name) and uik_number is None:
            continue
        kind = "uik" if uik_number is not None else "tik"
        number = uik_number
        commission_address = _publishable(
            _label_value(
                block[1:],
                ("Адрес комиссии", "Место нахождения комиссии", "Адрес ТИК", "Адрес УИК"),
            )
        )
        commission_phone = _publishable(
            _label_value(block[1:], ("Телефон комиссии", "Телефон", "Телефон ТИК", "Телефон УИК")),
            phone=True,
        )
        voting_address = _publishable(
            _label_value(
                block[1:],
                (
                    "Адрес помещения для голосования",
                    "Место голосования",
                    "Адрес избирательного участка",
                ),
            )
        )
        voting_phone = _publishable(
            _label_value(
                block[1:], ("Телефон помещения для голосования", "Телефон места голосования")
            ),
            phone=True,
        )
        if not any((commission_address, commission_phone, voting_address, voting_phone)):
            continue
        contacts.append(
            CommissionContact(
                subject_code=subject_code,
                commission_type=kind,
                commission_number=number,
                commission_name=name,
                external_id="",
                commission_address=commission_address,
                commission_phone=commission_phone,
                voting_address=voting_address,
                voting_phone=voting_phone,
                source=source,
            )
        )
    return contacts


def _deduplicate(contacts: Iterable[CommissionContact]) -> tuple[CommissionContact, ...]:
    unique: dict[tuple[object, ...], CommissionContact] = {}
    for contact in contacts:
        key = (
            contact.subject_code,
            contact.commission_type,
            contact.commission_number,
            _key(contact.commission_name),
            contact.external_id,
            contact.commission_address,
            contact.commission_phone,
            contact.voting_address,
            contact.voting_phone,
            contact.source.url,
            contact.source.sha256,
        )
        unique[key] = contact
    return tuple(sorted(unique.values(), key=_contact_sort_key))


def _contact_sort_key(contact: CommissionContact) -> tuple[object, ...]:
    code_key: object = (
        int(contact.subject_code) if contact.subject_code.isdigit() else contact.subject_code
    )
    return (
        code_key,
        contact.commission_type,
        contact.commission_number or -1,
        _key(contact.commission_name),
        contact.source.url,
    )


def parse_artifact(
    payload: bytes,
    *,
    url: str,
    subject_code: str,
    retrieved_at: str,
    status: int = 200,
    content_type: str = "",
) -> ParseOutcome:
    """Parse a preserved response without inferring values from ambiguous prose."""

    digest = hashlib.sha256(payload).hexdigest()
    suffix = _suffix(url)
    media = content_type.casefold()
    parser = "unsupported"
    source_type = "regional_unresolved"
    try:
        if suffix == ".csv" or "text/csv" in media:
            parser, source_type = "csv", "regional_csv"
        elif suffix == ".json" or "json" in media:
            parser, source_type = "json", "regional_json"
        elif suffix == ".xlsx" or "spreadsheetml" in media:
            parser, source_type = "xlsx_2026", "regional_xlsx_2026"
        elif suffix == ".docx" or "wordprocessingml" in media:
            parser, source_type = "docx_2026", "regional_docx_2026"
        elif (
            suffix in HTML_SUFFIXES
            or "html" in media
            or payload.lstrip().lower().startswith((b"<!doctype", b"<html"))
        ):
            parser, source_type = "html", "regional_html"
        else:
            return ParseOutcome((), parser, "unresolved", "unsupported document format")
        source = SourceEvidence(url, retrieved_at, digest, status, source_type)
        if parser == "csv":
            contacts = _csv_contacts(payload, subject_code=subject_code, source=source)
        elif parser == "json":
            contacts = _json_contacts(payload, subject_code=subject_code, source=source)
        elif parser == "xlsx_2026":
            contacts = _xlsx_contacts(payload, url=url, subject_code=subject_code, source=source)
        elif parser == "docx_2026":
            contacts = _docx_contacts(payload, url=url, subject_code=subject_code, source=source)
        else:
            html = _HTML()
            html.feed(_decode(payload))
            html.close()
            adapted = parse_html_adapter(
                subject_code=subject_code,
                url=url,
                title=html.title,
                text=html.text,
                source=source,
            )
            if adapted is not None:
                parser = f"adapter:{adapted.name}"
                contacts = list(adapted.contacts)
            else:
                contacts = []
                for table in html.tables:
                    contacts.extend(
                        _records_contacts(table, subject_code=subject_code, source=source)
                    )
                contacts.extend(
                    _labelled_html_contacts(html.text, subject_code=subject_code, source=source)
                )
    except (csv.Error, json.JSONDecodeError, UnicodeError, ValueError) as error:
        return ParseOutcome((), parser, "unresolved", f"parse error: {error}")
    result = _deduplicate(contacts)
    if not result:
        return ParseOutcome((), parser, "unresolved", "no high-confidence contact records")
    return ParseOutcome(result, parser, "parsed")


def _coerce_response(value: object, requested_url: str) -> FetchResponse:
    if isinstance(value, FetchResponse):
        return value
    if isinstance(value, Mapping):
        body = value.get("body", value.get("content", b""))
        if isinstance(body, str):
            body = body.encode("utf-8")
        return FetchResponse(
            url=str(value.get("url") or value.get("final_url") or requested_url),
            status=int(value.get("status") or value.get("status_code") or 0),
            body=bytes(body),
            content_type=str(value.get("content_type") or ""),
            retrieved_at=str(value.get("retrieved_at") or ""),
        )
    raise TypeError("fetcher must return FetchResponse or a response mapping")


def _fetch(fetcher: Fetcher | Callable[[str], object], url: str) -> FetchResponse:
    method = getattr(fetcher, "fetch", None)
    value = method(url) if callable(method) else fetcher(url)  # type: ignore[operator]
    return _coerce_response(value, url)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _store_payload(raw_root: Path, payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    relative = Path("raw") / "sha256" / digest[:2] / digest
    path = raw_root.parent / relative
    with _STORE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise OSError(f"corrupt content-addressed artifact: {path}")
        else:
            temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
            temporary.write_bytes(payload)
            temporary.replace(path)
    return digest, relative.as_posix()


def _cached(region_dir: Path, output_dir: Path) -> dict[str, FetchResponse]:
    path = region_dir / "manifest.json"
    if not path.is_file():
        return {}
    try:
        entries = json.loads(path.read_text(encoding="utf-8")).get("artifacts", [])
    except (json.JSONDecodeError, AttributeError):
        return {}
    result: dict[str, FetchResponse] = {}
    for entry in entries:
        raw_path = entry.get("rawPath")
        artifact = output_dir / raw_path if raw_path else None
        if not artifact or not artifact.is_file() or not 200 <= int(entry.get("status") or 0) < 400:
            continue
        body = artifact.read_bytes()
        if hashlib.sha256(body).hexdigest() != entry.get("sha256"):
            continue
        result[str(entry["requestedUrl"])] = FetchResponse(
            url=str(entry.get("url") or entry["requestedUrl"]),
            status=int(entry["status"]),
            body=body,
            content_type=str(entry.get("contentType") or ""),
            retrieved_at=str(entry.get("retrievedAt") or ""),
        )
    return result


def _looks_candidate(value: str, patterns: Sequence[re.Pattern[str]]) -> bool:
    return any(pattern.search(value) for pattern in patterns)


def crawl_region(
    region: RegionSource,
    fetcher: Fetcher | Callable[[str], object],
    output_dir: Path,
    *,
    search_terms: Sequence[str] = DEFAULT_SEARCH_TERMS,
    follow_patterns: Sequence[str] = DEFAULT_FOLLOW_PATTERNS,
    max_pages: int | None = None,
    max_depth: int | None = None,
    max_documents: int = 100,
    concurrency: int = 4,
    refresh: bool = False,
    cache_only: bool = False,
) -> dict[str, Any]:
    """Crawl one region, returning its deterministic manifest and summary."""

    page_limit = region.max_pages if max_pages is None else max_pages
    depth_limit = region.max_depth if max_depth is None else max_depth
    if not 1 <= page_limit <= 2_000 or not 0 <= depth_limit <= 10:
        raise ValueError("unsafe crawl limit")
    if not 0 <= max_documents <= 2_000 or not 1 <= concurrency <= 32:
        raise ValueError("unsafe document/concurrency limit")
    compiled = [re.compile(pattern, re.IGNORECASE) for pattern in follow_patterns]
    allowed = set(region.allowed_hosts)
    region_dir = output_dir / "regions" / region.code
    cache = {} if refresh else _cached(region_dir, output_dir)
    search_urls = [
        canonical_url(f"/search/?q={urllib.parse.quote_plus(term)}", region.base_url)
        for term in search_terms
        if _clean(term)
    ]
    initial = sorted(
        {
            *region.seed_urls,
            *adapter_seed_urls(region.code, region.base_url),
            *(url for url in search_urls if url),
        }
    )
    pending: deque[tuple[str, int, str]] = deque((url, 0, "") for url in initial)
    queued = set(initial)
    visited: set[str] = set()
    documents: dict[str, tuple[int, str]] = {}
    artifacts: list[dict[str, Any]] = []
    contacts: list[CommissionContact] = []

    def retrieve(url: str) -> tuple[str, FetchResponse | None, str]:
        if url in cache:
            return url, cache[url], ""
        if cache_only:
            return url, None, "not present in verified cache (cache-only)"
        try:
            response = _fetch(fetcher, url)
            if not response.retrieved_at:
                response = FetchResponse(
                    response.url,
                    response.status,
                    response.body,
                    response.content_type,
                    datetime.now(UTC).isoformat(),
                )
            return url, response, ""
        except Exception as error:  # noqa: BLE001 - failure is manifest data
            return url, None, f"{type(error).__name__}: {error}"

    def record_response(
        requested: str,
        response: FetchResponse | None,
        *,
        depth: int,
        parent: str,
        candidate: bool,
        error: str,
    ) -> ParseOutcome | None:
        if response is None:
            artifacts.append(
                {
                    "regionCode": region.code,
                    "requestedUrl": requested,
                    "url": requested,
                    "depth": depth,
                    "parentUrl": parent,
                    "status": 0,
                    "error": error,
                    "candidate": candidate,
                    "parseStatus": "fetch_failed",
                    "sha256": "",
                    "rawPath": "",
                }
            )
            return None
        final = canonical_url(response.url) or requested
        if _host(final) not in allowed:
            artifacts.append(
                {
                    "regionCode": region.code,
                    "requestedUrl": requested,
                    "url": final,
                    "depth": depth,
                    "parentUrl": parent,
                    "status": response.status,
                    "error": "redirected outside allowed hosts",
                    "candidate": candidate,
                    "parseStatus": "rejected_redirect",
                    "sha256": "",
                    "rawPath": "",
                    "retrievedAt": response.retrieved_at,
                }
            )
            return None
        digest, raw_path = _store_payload(output_dir / "raw", response.body)
        outcome = (
            parse_artifact(
                response.body,
                url=final,
                subject_code=region.code,
                retrieved_at=response.retrieved_at,
                status=response.status,
                content_type=response.content_type,
            )
            if candidate and 200 <= response.status < 400
            else None
        )
        artifacts.append(
            {
                "regionCode": region.code,
                "requestedUrl": requested,
                "url": final,
                "depth": depth,
                "parentUrl": parent,
                "status": response.status,
                "contentType": response.content_type,
                "retrievedAt": response.retrieved_at,
                "sha256": digest,
                "byteLength": len(response.body),
                "rawPath": raw_path,
                "candidate": candidate,
                "parser": outcome.parser if outcome else "",
                "parseStatus": outcome.status
                if outcome
                else ("not_candidate" if 200 <= response.status < 400 else "http_error"),
                "parseReason": outcome.reason if outcome else "",
                "contactCount": len(outcome.contacts) if outcome else 0,
            }
        )
        if outcome:
            contacts.extend(outcome.contacts)
        return outcome

    while pending and len(visited) < page_limit:
        batch: list[tuple[str, int, str]] = []
        while pending and len(batch) < min(concurrency, page_limit - len(visited)):
            item = pending.popleft()
            if item[0] not in visited:
                visited.add(item[0])
                batch.append(item)
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            fetched = list(executor.map(lambda item: retrieve(item[0]), batch))
        for (requested, depth, parent), (_, response, error) in zip(batch, fetched, strict=True):
            if response is None:
                record_response(
                    requested, None, depth=depth, parent=parent, candidate=True, error=error
                )
                continue
            final = canonical_url(response.url) or requested
            if _host(final) not in allowed:
                record_response(
                    requested, response, depth=depth, parent=parent, candidate=True, error=""
                )
                continue
            htmlish = (
                _suffix(final) in HTML_SUFFIXES
                or "html" in response.content_type.casefold()
                or response.body.lstrip().lower().startswith((b"<!doctype", b"<html"))
            )
            parser: _HTML | None = None
            body_signal = ""
            if htmlish and 200 <= response.status < 400:
                parser = _HTML()
                parser.feed(_decode(response.body))
                parser.close()
                body_signal = f"{parser.title} {parser.text[:10000]} {final}"
            candidate = _looks_candidate(body_signal or final, compiled)
            record_response(
                requested, response, depth=depth, parent=parent, candidate=candidate, error=""
            )
            if not parser or depth >= depth_limit:
                continue
            base = canonical_url(parser.base_href or final, final) or final
            for link in sorted(parser.links, key=lambda item: (item.href, item.text)):
                target = canonical_url(link.href, base)
                if not target or _host(target) not in allowed:
                    continue
                signal = f"{link.text} {target}"
                if not _looks_candidate(signal, compiled):
                    continue
                suffix = _suffix(target)
                if suffix in DOCUMENT_SUFFIXES:
                    documents.setdefault(target, (depth + 1, final))
                elif suffix in HTML_SUFFIXES and target not in queued:
                    queued.add(target)
                    pending.append((target, depth + 1, final))

    truncated_pages = bool(pending)
    document_items = [(url, *documents[url]) for url in sorted(documents)[:max_documents]]
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        fetched_documents = list(executor.map(lambda item: retrieve(item[0]), document_items))
    for (requested, depth, parent), (_, response, error) in zip(
        document_items, fetched_documents, strict=True
    ):
        record_response(
            requested, response, depth=depth, parent=parent, candidate=True, error=error
        )

    final_contacts = _deduplicate(contacts)
    artifact_rows = sorted(artifacts, key=lambda item: (item["requestedUrl"], item.get("url", "")))
    manifest = {
        "schemaVersion": 1,
        "region": {"code": region.code, "name": region.name, "baseUrl": region.base_url},
        "limits": {"maxPages": page_limit, "maxDepth": depth_limit, "maxDocuments": max_documents},
        "truncated": truncated_pages or len(documents) > max_documents,
        "artifacts": artifact_rows,
    }
    parse_counts = Counter(item["parseStatus"] for item in artifact_rows)
    summary = {
        "regionCode": region.code,
        "regionName": region.name,
        "pageRequests": len(visited),
        "documentRequests": len(document_items),
        "artifacts": len(artifact_rows),
        "truncated": manifest["truncated"],
        "fetchFailures": parse_counts["fetch_failed"]
        + parse_counts["http_error"]
        + parse_counts["rejected_redirect"],
        "parsedArtifacts": parse_counts["parsed"],
        "unresolvedArtifacts": parse_counts["unresolved"],
        "contacts": len(final_contacts),
        "tikContacts": sum(contact.commission_type == "tik" for contact in final_contacts),
        "uikContacts": sum(contact.commission_type == "uik" for contact in final_contacts),
    }
    _write_json(region_dir / "manifest.json", manifest)
    write_jsonl(region_dir / "contacts.jsonl", (contact.to_dict() for contact in final_contacts))
    _write_json(region_dir / "summary.json", summary)
    return {"manifest": manifest, "contacts": final_contacts, "summary": summary}


def crawl_catalog(
    regions: Sequence[RegionSource],
    fetcher: Fetcher | Callable[[str], object],
    output_dir: Path,
    *,
    max_pages_per_region: int | None = None,
    concurrency: int = 6,
    refresh: bool = False,
    cache_only: bool = False,
    search_terms: Sequence[str] = DEFAULT_SEARCH_TERMS,
    follow_patterns: Sequence[str] = DEFAULT_FOLLOW_PATTERNS,
) -> dict[str, Any]:
    """Crawl all regions with globally bounded region-level concurrency."""

    if not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")

    def run(region: RegionSource) -> tuple[str, dict[str, Any] | None, str]:
        try:
            return (
                region.code,
                crawl_region(
                    region,
                    fetcher,
                    output_dir,
                    search_terms=search_terms,
                    follow_patterns=follow_patterns,
                    max_pages=max_pages_per_region,
                    concurrency=1,
                    refresh=refresh,
                    cache_only=cache_only,
                ),
                "",
            )
        except Exception as error:  # noqa: BLE001 - other regions must continue
            return region.code, None, f"{type(error).__name__}: {error}"

    ordered = sorted(
        regions,
        key=lambda region: (int(region.code) if region.code.isdigit() else 10_000, region.code),
    )
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        results = list(executor.map(run, ordered))
    contacts: list[CommissionContact] = []
    region_summaries: list[dict[str, Any]] = []
    manifests: list[dict[str, Any]] = []
    for region, (code, result, error) in zip(ordered, results, strict=True):
        if result is None:
            region_summaries.append(
                {
                    "regionCode": code,
                    "regionName": region.name,
                    "error": error,
                    "contacts": 0,
                    "tikContacts": 0,
                    "uikContacts": 0,
                }
            )
            continue
        contacts.extend(result["contacts"])
        region_summaries.append(result["summary"])
        manifests.extend(result["manifest"]["artifacts"])
    final_contacts = _deduplicate(contacts)
    contact_counts = Counter(contact.commission_type for contact in final_contacts)
    summary = {
        "schemaVersion": 1,
        "regions": len(ordered),
        "regionsCompleted": sum("error" not in item for item in region_summaries),
        "regionsWithContacts": sum(item.get("contacts", 0) > 0 for item in region_summaries),
        "contacts": {
            "total": len(final_contacts),
            "tik": contact_counts["tik"],
            "uik": contact_counts["uik"],
            "withCommissionAddress": sum(bool(item.commission_address) for item in final_contacts),
            "withCommissionPhone": sum(bool(item.commission_phone) for item in final_contacts),
            "withVotingAddress": sum(bool(item.voting_address) for item in final_contacts),
            "withVotingPhone": sum(bool(item.voting_phone) for item in final_contacts),
        },
        "artifacts": {
            "total": len(manifests),
            "parsed": sum(item.get("parseStatus") == "parsed" for item in manifests),
            "unresolved": sum(item.get("parseStatus") == "unresolved" for item in manifests),
            "failures": sum(
                item.get("parseStatus") in {"fetch_failed", "http_error", "rejected_redirect"}
                for item in manifests
            ),
        },
        "regionCoverage": region_summaries,
    }
    write_jsonl(output_dir / "contacts.jsonl", (contact.to_dict() for contact in final_contacts))
    _write_json(
        output_dir / "manifest.json",
        {
            "schemaVersion": 1,
            "artifacts": sorted(
                manifests, key=lambda item: (item["requestedUrl"], item.get("url", ""))
            ),
        },
    )
    _write_json(output_dir / "summary.json", summary)
    return summary


def reparse_cached_regions(
    regions: Sequence[RegionSource],
    output_dir: Path,
    *,
    region_codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Reparse preserved successful artifacts without making network requests."""

    requested = {canonical_region_code(code) for code in region_codes} if region_codes else None
    known = {region.code for region in regions}
    if requested and (missing := sorted(requested - known)):
        raise ValueError(f"unknown region codes: {', '.join(missing)}")

    selected = [region for region in regions if requested is None or region.code in requested]
    totals: Counter[str] = Counter()
    for region in selected:
        region_dir = output_dir / "regions" / region.code
        manifest_path = region_dir / "manifest.json"
        if not manifest_path.is_file():
            totals["regions_without_cache"] += 1
            continue
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts = manifest.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise TypeError(f"{manifest_path}: artifacts must be an array")
        contacts: list[CommissionContact] = []
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                continue
            status = int(artifact.get("status") or 0)
            if not artifact.get("candidate") or not 200 <= status < 400:
                continue
            totals["candidate_artifacts"] += 1
            raw_path = str(artifact.get("rawPath") or "")
            cached_path = output_dir / raw_path if raw_path else None
            if not cached_path or not cached_path.is_file():
                artifact.update(
                    {
                        "parser": "",
                        "parseStatus": "cache_error",
                        "parseReason": "cached body is missing",
                        "contactCount": 0,
                    }
                )
                totals["cache_errors"] += 1
                continue
            payload = cached_path.read_bytes()
            digest = hashlib.sha256(payload).hexdigest()
            if digest != str(artifact.get("sha256") or ""):
                artifact.update(
                    {
                        "parser": "",
                        "parseStatus": "cache_error",
                        "parseReason": "cached body hash mismatch",
                        "contactCount": 0,
                    }
                )
                totals["cache_errors"] += 1
                continue
            previous_status = str(artifact.get("parseStatus") or "")
            outcome = parse_artifact(
                payload,
                url=str(artifact.get("url") or artifact.get("requestedUrl") or ""),
                subject_code=region.code,
                retrieved_at=str(artifact.get("retrievedAt") or ""),
                status=status,
                content_type=str(artifact.get("contentType") or ""),
            )
            artifact.update(
                {
                    "parser": outcome.parser,
                    "parseStatus": outcome.status,
                    "parseReason": outcome.reason,
                    "contactCount": len(outcome.contacts),
                }
            )
            contacts.extend(outcome.contacts)
            totals["reparsed_artifacts"] += 1
            totals["parsed_artifacts"] += outcome.status == "parsed"
            totals["newly_parsed_artifacts"] += (
                previous_status != "parsed" and outcome.status == "parsed"
            )

        final_contacts = _deduplicate(contacts)
        manifest["artifacts"] = sorted(
            artifacts, key=lambda item: (item.get("requestedUrl", ""), item.get("url", ""))
        )
        _write_json(manifest_path, manifest)
        write_jsonl(
            region_dir / "contacts.jsonl", (contact.to_dict() for contact in final_contacts)
        )
        old_summary_path = region_dir / "summary.json"
        old_summary = (
            json.loads(old_summary_path.read_text(encoding="utf-8"))
            if old_summary_path.is_file()
            else {}
        )
        parse_counts = Counter(str(item.get("parseStatus") or "") for item in manifest["artifacts"])
        summary = {
            **old_summary,
            "regionCode": region.code,
            "regionName": region.name,
            "artifacts": len(manifest["artifacts"]),
            "parsedArtifacts": parse_counts["parsed"],
            "unresolvedArtifacts": parse_counts["unresolved"],
            "fetchFailures": parse_counts["fetch_failed"]
            + parse_counts["http_error"]
            + parse_counts["rejected_redirect"]
            + parse_counts["cache_error"],
            "contacts": len(final_contacts),
            "tikContacts": sum(contact.commission_type == "tik" for contact in final_contacts),
            "uikContacts": sum(contact.commission_type == "uik" for contact in final_contacts),
        }
        _write_json(old_summary_path, summary)
        totals["regions_reparsed"] += 1
        totals["contacts"] += len(final_contacts)

    summary = aggregate_cached_regions(regions, output_dir)
    summary["reparse"] = {
        key: totals[key]
        for key in (
            "regions_reparsed",
            "regions_without_cache",
            "candidate_artifacts",
            "reparsed_artifacts",
            "parsed_artifacts",
            "newly_parsed_artifacts",
            "cache_errors",
            "contacts",
        )
    }
    _write_json(output_dir / "summary.json", summary)
    return summary


def aggregate_cached_regions(regions: Sequence[RegionSource], output_dir: Path) -> dict[str, Any]:
    """Rebuild global outputs from every available per-region result."""

    contacts: list[CommissionContact] = []
    summaries: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    for region in regions:
        region_dir = output_dir / "regions" / region.code
        contacts_path = region_dir / "contacts.jsonl"
        summary_path = region_dir / "summary.json"
        manifest_path = region_dir / "manifest.json"
        if contacts_path.is_file():
            for line in contacts_path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    contacts.append(CommissionContact.from_dict(json.loads(line)))
        if summary_path.is_file():
            summaries.append(json.loads(summary_path.read_text(encoding="utf-8")))
        if manifest_path.is_file():
            artifacts.extend(
                json.loads(manifest_path.read_text(encoding="utf-8")).get("artifacts", [])
            )

    final_contacts = _deduplicate(contacts)
    contact_counts = Counter(contact.commission_type for contact in final_contacts)
    summary = {
        "schemaVersion": 1,
        "regions": len(regions),
        "regionsCompleted": len(summaries),
        "regionsWithContacts": sum(item.get("contacts", 0) > 0 for item in summaries),
        "contacts": {
            "total": len(final_contacts),
            "tik": contact_counts["tik"],
            "uik": contact_counts["uik"],
            "withCommissionAddress": sum(bool(item.commission_address) for item in final_contacts),
            "withCommissionPhone": sum(bool(item.commission_phone) for item in final_contacts),
            "withVotingAddress": sum(bool(item.voting_address) for item in final_contacts),
            "withVotingPhone": sum(bool(item.voting_phone) for item in final_contacts),
        },
        "artifacts": {
            "total": len(artifacts),
            "parsed": sum(item.get("parseStatus") == "parsed" for item in artifacts),
            "unresolved": sum(item.get("parseStatus") == "unresolved" for item in artifacts),
            "failures": sum(
                item.get("parseStatus") in {"fetch_failed", "http_error", "rejected_redirect"}
                for item in artifacts
            ),
        },
        "regionCoverage": sorted(
            summaries,
            key=lambda item: (
                int(item["regionCode"]) if str(item.get("regionCode", "")).isdigit() else 10_000,
                str(item.get("regionCode", "")),
            ),
        ),
    }
    write_jsonl(output_dir / "contacts.jsonl", (item.to_dict() for item in final_contacts))
    _write_json(
        output_dir / "manifest.json",
        {
            "schemaVersion": 1,
            "artifacts": sorted(
                artifacts, key=lambda item: (item["requestedUrl"], item.get("url", ""))
            ),
        },
    )
    _write_json(output_dir / "summary.json", summary)
    return summary


def run_regional_crawl(
    catalog_path: Path,
    output_dir: Path,
    *,
    proxy_url: str | None = None,
    max_pages_per_region: int = 100,
    concurrency: int = 6,
    timeout: float = 30.0,
    refresh: bool = False,
    cache_only: bool = False,
    region_codes: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Convenience entrypoint used by the CLI/assembler orchestration."""

    regions = load_catalog(catalog_path)
    selected = regions
    if region_codes:
        requested = {canonical_region_code(code) for code in region_codes}
        known = {region.code for region in regions}
        if missing := sorted(requested - known):
            raise ValueError(f"unknown region codes: {', '.join(missing)}")
        selected = [region for region in regions if region.code in requested]
    fetcher = RequestsFetcher(
        proxy_url=proxy_url,
        timeout=timeout,
        concurrency=concurrency,
    )
    crawl_catalog(
        selected,
        fetcher,
        output_dir,
        max_pages_per_region=max_pages_per_region,
        concurrency=concurrency,
        refresh=refresh,
        cache_only=cache_only,
    )
    return aggregate_cached_regions(regions, output_dir)
