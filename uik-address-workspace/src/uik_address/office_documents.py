"""Conservative parsers for official 2026 precinct-list Office documents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import pdfplumber
import xlrd
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException
from pypdf import PdfReader
from pypdf.errors import PyPdfError

MAX_WORKBOOK_BYTES = 25_000_000
MAX_WORKSHEETS = 100
MAX_ROWS_PER_SHEET = 200_000
MAX_COLUMNS = 100
HEADER_SCAN_ROWS = 30
MAX_DOCUMENT_XML_BYTES = 100_000_000
MAX_PDF_BYTES = 25_000_000
MAX_PDF_PAGES = 500
MAX_PDF_TEXT_CHARACTERS = 50_000_000
MAX_ADDRESS_CHARACTERS = 500

_PRECINCT_CONTEXT_RE = re.compile(
    r"(?:переч(?:ень|ня)\s+(?:участков(?:ых)?\s+избирательных\s+комиссий|"
    r"избирательных\s+участков)|сведения\s+об\s+избирательном\s+участке|"
    r"номер\s+избирательного\s+участка|участков\w*\s+избирательн\w*\s+комисси\w*|"
    r"(?:^|\W)уик(?:\W|$))",
    re.IGNORECASE,
)
_REMOTE_VOTING_CONTEXT_RE = re.compile(
    r"(?:групп\w*\s+избирател|отсутствуют\s+помещения\s+для\s+голосования|"
    r"транспортн\w*\s+сообщени\w*\s+с\s+котор\w*\s+затруднен)",
    re.IGNORECASE,
)
_CYRILLIC_OR_LATIN_RE = re.compile(r"[a-zа-яё]", re.IGNORECASE)
_PDF_RECORD_RE = re.compile(
    r"избир\s*ательн\w*\s+участ\w*"
    r"(?:\s*,?\s*участ\w*\s+референдум\w*)?\s*№?\s*(\d{1,5})",
    re.IGNORECASE | re.MULTILINE,
)
_PDF_COMBINED_LOCATION_RE = re.compile(
    r"(?:"
    r"(?:место\s+нахождени[ея]|адрес)\s+(?:помещени\w*\s+)?участков\w*\s+"
    r"(?:избирательн\w*\s+)?комисси\w*\s+и\s+(?:помещени\w*\s+)?для\s+голосовани\w*"
    r"|помещени\w*\s+для\s+голосовани\w*"
    r"|для\s+голосовани\w*"
    r")\s*[:;–—-]\s*(.*?)"
    r"(?=\s*№\s*телефон|\s*(?:в\s+границах|границ\w*\s+(?:избирательн\w*\s+)?"
    r"участк\w*)\s*[:;]|\n\s*(?:территориальн\w*\s+"
    r"избирательн\w*\s+комисси\w*|(?:внутригородск\w*\s+)?муниципальн\w*\s+"
    r"образовани\w*)|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_PDF_PHONE_RE = re.compile(
    r"№\s*телефон\w*\s*[:;]\s*(.*?)"
    r"(?=\s*в\s+границах\s*[:;]|\Z)",
    re.IGNORECASE | re.DOTALL,
)


@dataclass(frozen=True, slots=True)
class PrecinctDocumentRow:
    number: int
    voting_address: str = ""
    voting_phone: str = ""
    commission_address: str = ""
    commission_phone: str = ""


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _key(value: object) -> str:
    text = _clean(value).casefold().replace("ё", "е")
    # PDF table extractors preserve print line-break hyphenation. Rejoin only a
    # hyphen followed by whitespace so ordinary compounds remain distinct.
    text = re.sub(r"(?<=[a-zа-я])[-‐‑]\s+(?=[a-zа-я])", "", text)
    return re.sub(r"[^a-zа-я0-9№]+", " ", text).strip()


def _is_2026_source(url: str, rows: list[list[str]], context_extra: str = "") -> bool:
    context = f"{url} {context_extra} " + " ".join(
        cell for row in rows[:HEADER_SCAN_ROWS] for cell in row
    )
    return bool(re.search(r"(?<!\d)2026(?!\d)|20\s*09\s*2026", context, re.IGNORECASE))


def _number_header(value: str) -> bool:
    key = _key(value)
    return key in {"№", "номер", "№ уик", "номер уик"} or bool(
        re.search(
            r"(?:номер|№)\s+(?:(?:изб(?:ирательного)?\s+)?участка|участковой\s+"
            r"(?:избирательной\s+)?комиссии|уик)\b",
            key,
        )
    )


def _address_header(value: str) -> bool:
    if len(value) > 250 or _PDF_COMBINED_LOCATION_RE.search(value):
        return False
    key = _key(value)
    return key == "адрес" or bool(
        re.search(
            r"(?:адрес|место нахождения).*(?:помещен|голосован|избирательн|участк)|"
            r"адрес наименование помещения",
            key,
        )
    )


def _phone_header(value: str) -> bool:
    key = _key(value)
    return key in {"телефон", "телефоны"} or key.startswith(("телефон ", "телефон, "))


def _column_role(rows: list[list[str]], row_index: int, column_index: int) -> str:
    context = _key(
        " ".join(
            row[column_index]
            for row in rows[: row_index + 1]
            if column_index < len(row) and row[column_index]
        )
    )
    if re.search(
        r"(?:помещен.*голосован|участок для голосования|место проведения голосования)",
        context,
    ):
        return "voting"
    if re.search(r"(?:избирательн.*комисс|адрес уик|место нахождения участковой)", context):
        return "commission"
    return "generic"


def _number(value: str) -> int | None:
    match = re.fullmatch(r"\s*(?:уик\s*)?(?:№|N|No\.?)?\s*(\d+)(?:\.0+)?\s*", value, re.IGNORECASE)
    if match is None:
        return None
    number = int(match.group(1))
    return number if 0 < number < 100_000 else None


def _address(value: str) -> str:
    text = _clean(value)
    if (
        len(text) < 8
        or len(text) > MAX_ADDRESS_CHARACTERS
        or not _CYRILLIC_OR_LATIN_RE.search(text)
        or "..." in text
        or "…" in text
    ):
        return ""
    return text


def _phone(value: str) -> str:
    text = _clean(value)
    return text if sum(character.isdigit() for character in text) >= 5 else ""


def _address_and_phone(value: str) -> tuple[str, str]:
    text = _clean(value).rstrip(" .;")
    marker = re.search(r"\s*,?\s*тел(?:ефон)?\.?\b", text, re.IGNORECASE)
    if marker is None:
        suffix = re.search(
            r"^(.*?)[,;]\s*((?:\+?\d[\d()\s+\-–—]{3,})\d)$",
            text,
        )
        if suffix is not None and sum(character.isdigit() for character in suffix.group(2)) >= 5:
            return _address(suffix.group(1)), _phone(suffix.group(2))
        return _address(text), ""
    phone_candidates = re.findall(
        r"[+()\d][\d()\s+\-–—]{4,}\d(?:\s*\(?(?:доб\.?\s*)?\d+\)?)?",
        text[marker.start() :],
        re.IGNORECASE,
    )
    phone = _phone(phone_candidates[-1]) if phone_candidates else ""
    return _address(text[: marker.start()]), phone


def _trim_unlabelled_precinct_boundaries(value: str) -> str:
    """Drop an address-list tail when a combined-location block has no boundary label."""

    match = re.search(
        r"\)(?=\s+(?:проспект(?:ы)?|улиц(?:а|ы)?|переул(?:ок|ки)|проезд(?:ы)?|"
        r"шоссе|микрорайон(?:ы)?|дом(?:а|ы)?|пос[её]лок|деревн[яи]|садовод\w*|"
        r"государственн\w*|муниципальн\w*)\b)",
        value,
        re.IGNORECASE | re.DOTALL,
    )
    return value[: match.end()] if match is not None else value


def _ditto(value: str) -> bool:
    return bool(re.fullmatch(r"[\s\-–—'\"«»]+", value))


def _sheet_rows(worksheet: Any) -> list[list[str]]:
    rows: list[list[str]] = []
    for row_number, raw in enumerate(
        worksheet.iter_rows(values_only=True, max_col=MAX_COLUMNS), start=1
    ):
        if row_number > MAX_ROWS_PER_SHEET:
            raise ValueError(f"worksheet {worksheet.title!r} exceeds the row limit")
        row = [_clean(cell) for cell in raw]
        while row and not row[-1]:
            row.pop()
        rows.append(row)
    return rows


def _parse_table(
    rows: list[list[str]], *, url: str, context_extra: str = ""
) -> list[PrecinctDocumentRow]:
    if not rows or not _is_2026_source(url, rows, context_extra):
        return []
    context = (
        context_extra + " " + " ".join(cell for row in rows[:HEADER_SCAN_ROWS] for cell in row)
    )
    if not _PRECINCT_CONTEXT_RE.search(context):
        return []
    if _REMOTE_VOTING_CONTEXT_RE.search(context):
        return []

    number_columns: list[tuple[int, int]] = []
    address_columns: list[tuple[int, int, str]] = []
    phone_columns: list[tuple[int, int, str]] = []
    for row_index, row in enumerate(rows[:HEADER_SCAN_ROWS]):
        for column_index, cell in enumerate(row):
            if _number_header(cell):
                number_columns.append((row_index, column_index))
            if _address_header(cell):
                address_columns.append(
                    (row_index, column_index, _column_role(rows, row_index, column_index))
                )
            if _phone_header(cell):
                phone_columns.append(
                    (row_index, column_index, _column_role(rows, row_index, column_index))
                )

    # Some official lists keep boundaries and the labelled combined UIK/voting
    # location in one cell. Extract that explicit label without treating the
    # preceding boundary prose as an address column.
    inline_records: dict[int, PrecinctDocumentRow] = {}
    for row in rows:
        row_text = " ".join(cell for cell in row if cell)
        record_match = _PDF_RECORD_RE.search(row_text)
        number = int(record_match.group(1)) if record_match is not None else None
        if number is None and number_columns:
            _, column = max(number_columns, key=lambda item: (item[1], item[0]))
            number = _number(row[column]) if column < len(row) else None
        location_match = _PDF_COMBINED_LOCATION_RE.search(row_text)
        if number is None or location_match is None:
            continue
        address, phone = _address_and_phone(location_match.group(1))
        if address:
            inline_records[number] = PrecinctDocumentRow(
                number=number,
                voting_address=address,
                voting_phone=phone,
                commission_address=address,
                commission_phone=phone,
            )
    if not number_columns or not address_columns:
        return list(inline_records.values())

    # Prefer the most explicit/right-most UIK number column. This avoids using
    # a serial-number column when a sheet has both "№ п/п" and "№ УИК".
    number_row, number_column = max(
        number_columns,
        key=lambda item: (
            "уик" in _key(rows[item[0]][item[1]])
            or "избирательного участка" in _key(rows[item[0]][item[1]]),
            item[1],
            item[0],
        ),
    )

    def select_column(role: str, values: list[tuple[int, int, str]]) -> tuple[int, int] | None:
        matches = [(row, column) for row, column, actual in values if actual == role]
        return max(matches, key=lambda item: (item[0], item[1])) if matches else None

    commission_address = select_column("commission", address_columns)
    voting_address = select_column("voting", address_columns)
    generic_address = select_column("generic", address_columns)
    if voting_address is None and commission_address is None:
        voting_address = generic_address
    commission_phone = select_column("commission", phone_columns)
    voting_phone = select_column("voting", phone_columns)
    generic_phone = select_column("generic", phone_columns)
    if voting_phone is None and commission_phone is None and voting_address is not None:
        voting_phone = generic_phone
    header_rows = [number_row]
    header_rows.extend(row for row, _, _ in address_columns)
    header_rows.extend(row for row, _, _ in phone_columns)
    data_start = max(header_rows) + 1

    def value(row: list[str], column: tuple[int, int] | None) -> str:
        return row[column[1]] if column is not None and column[1] < len(row) else ""

    result: dict[int, PrecinctDocumentRow] = dict(inline_records)
    for row in rows[data_start:]:
        raw_number = row[number_column] if number_column < len(row) else ""
        number = _number(raw_number)
        commission_address_value, embedded_commission_phone = _address_and_phone(
            value(row, commission_address)
        )
        commission_phone_value = _phone(value(row, commission_phone)) or embedded_commission_phone
        raw_voting_address = value(row, voting_address)
        raw_voting_phone = value(row, voting_phone)
        voting_address_value, embedded_voting_phone = _address_and_phone(raw_voting_address)
        voting_phone_value = _phone(raw_voting_phone) or embedded_voting_phone
        if (
            not voting_address_value
            and commission_address_value
            and "если совпадает" in context.casefold()
            and _ditto(raw_voting_address)
        ):
            voting_address_value = commission_address_value
        if (
            not voting_phone_value
            and commission_phone_value
            and "если совпадает" in context.casefold()
            and _ditto(raw_voting_phone)
        ):
            voting_phone_value = commission_phone_value
        if number is None or not any(
            (
                voting_address_value,
                voting_phone_value,
                commission_address_value,
                commission_phone_value,
            )
        ):
            continue
        result[number] = PrecinctDocumentRow(
            number=number,
            voting_address=voting_address_value,
            voting_phone=voting_phone_value,
            commission_address=commission_address_value,
            commission_phone=commission_phone_value,
        )
    return list(result.values())


def parse_xlsx_precinct_rows(payload: bytes, *, url: str) -> tuple[PrecinctDocumentRow, ...]:
    """Extract explicitly numbered polling places from a 2026 XLSX workbook.

    A workbook is accepted only when its URL or leading sheet text says 2026,
    its header context explicitly describes precincts/UIKs, and it has distinct
    number and address columns. Unsupported layouts return no rows.
    """

    if not payload:
        return ()
    if len(payload) > MAX_WORKBOOK_BYTES:
        raise ValueError("workbook exceeds the byte limit")
    try:
        workbook = load_workbook(BytesIO(payload), read_only=True, data_only=True)
    except (BadZipFile, InvalidFileException, KeyError, OSError, ValueError) as error:
        raise ValueError(f"invalid XLSX workbook: {error}") from error
    try:
        if len(workbook.worksheets) > MAX_WORKSHEETS:
            raise ValueError("workbook exceeds the worksheet limit")
        records: dict[int, PrecinctDocumentRow] = {}
        conflicts: set[int] = set()
        for worksheet in workbook.worksheets:
            for record in _parse_table(_sheet_rows(worksheet), url=url):
                previous = records.get(record.number)
                if previous is not None and previous != record:
                    conflicts.add(record.number)
                else:
                    records[record.number] = record
        for number in conflicts:
            records.pop(number, None)
        return tuple(records[number] for number in sorted(records))
    finally:
        workbook.close()


def parse_xls_precinct_rows(payload: bytes, *, url: str) -> tuple[PrecinctDocumentRow, ...]:
    """Extract explicitly numbered polling places from a current BIFF XLS file."""

    if not payload:
        return ()
    if len(payload) > MAX_WORKBOOK_BYTES:
        raise ValueError("workbook exceeds the byte limit")
    try:
        workbook = xlrd.open_workbook(file_contents=payload, on_demand=True)
    except (OSError, ValueError, xlrd.XLRDError) as error:
        raise ValueError(f"invalid XLS workbook: {error}") from error
    try:
        if workbook.nsheets > MAX_WORKSHEETS:
            raise ValueError("workbook exceeds the worksheet limit")
        records: dict[int, PrecinctDocumentRow] = {}
        conflicts: set[int] = set()
        for worksheet in workbook.sheets():
            if worksheet.nrows > MAX_ROWS_PER_SHEET:
                raise ValueError(f"worksheet {worksheet.name!r} exceeds the row limit")
            rows = [
                [
                    _clean(worksheet.cell_value(row, column))
                    for column in range(min(worksheet.ncols, MAX_COLUMNS))
                ]
                for row in range(worksheet.nrows)
            ]
            for record in _parse_table(rows, url=url):
                previous = records.get(record.number)
                if previous is not None and previous != record:
                    conflicts.add(record.number)
                else:
                    records[record.number] = record
        for number in conflicts:
            records.pop(number, None)
        return tuple(records[number] for number in sorted(records))
    finally:
        workbook.release_resources()


def _docx_cell_text(cell: ElementTree.Element, namespace: dict[str, str]) -> str:
    paragraphs: list[str] = []
    for paragraph in cell.findall(".//w:p", namespace):
        text = "".join(node.text or "" for node in paragraph.findall(".//w:t", namespace))
        if cleaned := _clean(text):
            paragraphs.append(cleaned)
    return _clean(" ".join(paragraphs))


def parse_docx_precinct_rows(payload: bytes, *, url: str) -> tuple[PrecinctDocumentRow, ...]:
    """Extract explicitly numbered polling places from a current 2026 DOCX."""

    if not payload:
        return ()
    if len(payload) > MAX_WORKBOOK_BYTES:
        raise ValueError("document exceeds the byte limit")
    namespace = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    try:
        with ZipFile(BytesIO(payload)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > MAX_DOCUMENT_XML_BYTES:
                raise ValueError("document XML exceeds the byte limit")
            root = ElementTree.fromstring(archive.read(info))
    except (BadZipFile, KeyError, ElementTree.ParseError, OSError) as error:
        raise ValueError(f"invalid DOCX document: {error}") from error

    document_context = _clean(
        " ".join(node.text or "" for node in root.findall(".//w:t", namespace))
    )
    records = {
        record.number: record for record in _parse_labelled_pdf_text(document_context, url=url)
    }
    table_records: dict[int, PrecinctDocumentRow] = {}
    table_conflicts: set[int] = set()
    for table_number, table in enumerate(root.findall(".//w:tbl", namespace), start=1):
        if table_number > MAX_WORKSHEETS:
            raise ValueError("document exceeds the table limit")
        rows: list[list[str]] = []
        for row_number, row in enumerate(table.findall("./w:tr", namespace), start=1):
            if row_number > MAX_ROWS_PER_SHEET:
                raise ValueError("document table exceeds the row limit")
            cells = [
                _docx_cell_text(cell, namespace)
                for cell in row.findall("./w:tc", namespace)[:MAX_COLUMNS]
            ]
            rows.append(cells)
        for record in _parse_table(rows, url=url, context_extra=document_context):
            previous = table_records.get(record.number)
            if previous is not None and previous != record:
                table_conflicts.add(record.number)
            else:
                table_records[record.number] = record
    for number in table_conflicts:
        records.pop(number, None)
        table_records.pop(number, None)
    # Structured cells take precedence over noisier concatenated DOCX text.
    records.update(table_records)
    return tuple(records[number] for number in sorted(records))


def _parse_labelled_pdf_text(text: str, *, url: str) -> tuple[PrecinctDocumentRow, ...]:
    """Parse only records that explicitly share a UIK and voting-room location."""

    for broken, joined in (
        (r"избирател\s+ь", "избиратель"),
        (r"помещени\s+я", "помещения"),
        (r"голосовани\s+я", "голосования"),
        (r"голос\s+ован", "голосован"),
        (r"уча\s+стк", "участк"),
    ):
        text = re.sub(broken, joined, text, flags=re.IGNORECASE)
    if not re.search(r"(?<!\d)2026(?!\d)", f"{url} {text}"):
        return ()
    matches = list(_PDF_RECORD_RE.finditer(text))
    records: dict[int, PrecinctDocumentRow] = {}
    conflicts: set[int] = set()
    for position, match in enumerate(matches):
        number = int(match.group(1))
        if not 0 < number < 100_000:
            continue
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        block = text[match.end() : end]
        location_match = _PDF_COMBINED_LOCATION_RE.search(block)
        if location_match is None:
            continue
        location = _trim_unlabelled_precinct_boundaries(location_match.group(1))
        address, inline_phone = _address_and_phone(location)
        phone_match = _PDF_PHONE_RE.search(block)
        phone = _phone(phone_match.group(1)) if phone_match is not None else inline_phone
        if not address:
            continue
        record = PrecinctDocumentRow(
            number=number,
            voting_address=address,
            voting_phone=phone,
            commission_address=address,
            commission_phone=phone,
        )
        previous = records.get(number)
        if previous is not None and previous != record:
            conflicts.add(number)
        else:
            records[number] = record
    for number in conflicts:
        records.pop(number, None)
    return tuple(records[number] for number in sorted(records))


def parse_labelled_precinct_text(text: str, *, url: str) -> tuple[PrecinctDocumentRow, ...]:
    """Extract explicitly combined UIK/polling locations from current labelled text."""

    return _parse_labelled_pdf_text(text, url=url)


def parse_pdf_precinct_rows(payload: bytes, *, url: str) -> tuple[PrecinctDocumentRow, ...]:
    """Extract explicit combined UIK/voting locations from a current text PDF."""

    if not payload:
        return ()
    if len(payload) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds the byte limit")
    try:
        reader = PdfReader(BytesIO(payload), strict=False)
        if reader.is_encrypted:
            raise ValueError("encrypted PDF is unsupported")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise ValueError("PDF exceeds the page limit")
        parts: list[str] = []
        characters = 0
        for page in reader.pages:
            extracted = page.extract_text() or ""
            characters += len(extracted)
            if characters > MAX_PDF_TEXT_CHARACTERS:
                raise ValueError("PDF exceeds the text limit")
            parts.append(extracted)
    except (PyPdfError, OSError) as error:
        raise ValueError(f"invalid PDF document: {error}") from error
    document_text = "\n".join(parts)
    records = {row.number: row for row in _parse_labelled_pdf_text(document_text, url=url)}
    table_rows: list[list[str]] = []
    try:
        with pdfplumber.open(BytesIO(payload)) as document:
            for page in document.pages:
                for table in page.extract_tables():
                    table_rows.extend([[_clean(cell) for cell in row] for row in table])
    except Exception as error:
        raise ValueError(f"invalid PDF table structure: {error}") from error
    # PDF pages commonly repeat the same table without repeating its header.
    # Parsing their rows together preserves the header schema for later pages.
    # Structured table values take precedence over noisier full-text extraction.
    for record in _parse_table(table_rows, url=url, context_extra=document_text[:100_000]):
        records[record.number] = record
    return tuple(records[number] for number in sorted(records))
