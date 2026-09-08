"""Conservative parsers for official 2026 precinct-list Office documents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from io import BytesIO
from typing import Any
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

MAX_WORKBOOK_BYTES = 25_000_000
MAX_WORKSHEETS = 100
MAX_ROWS_PER_SHEET = 200_000
MAX_COLUMNS = 100
HEADER_SCAN_ROWS = 30
MAX_DOCUMENT_XML_BYTES = 100_000_000

_PRECINCT_CONTEXT_RE = re.compile(
    r"(?:переч(?:ень|ня)\s+(?:участков(?:ых)?\s+избирательных\s+комиссий|"
    r"избирательных\s+участков)|сведения\s+об\s+избирательном\s+участке|"
    r"номер\s+избирательного\s+участка|(?:^|\W)уик(?:\W|$))",
    re.IGNORECASE,
)
_REMOTE_VOTING_CONTEXT_RE = re.compile(
    r"(?:групп\w*\s+избирател|отсутствуют\s+помещения\s+для\s+голосования|"
    r"транспортн\w*\s+сообщени\w*\s+с\s+котор\w*\s+затруднен)",
    re.IGNORECASE,
)
_CYRILLIC_OR_LATIN_RE = re.compile(r"[a-zа-яё]", re.IGNORECASE)


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
    return re.sub(r"[^a-zа-я0-9№]+", " ", _clean(value).casefold().replace("ё", "е")).strip()


def _is_2026_source(url: str, rows: list[list[str]], context_extra: str = "") -> bool:
    context = f"{url} {context_extra} " + " ".join(
        cell for row in rows[:HEADER_SCAN_ROWS] for cell in row
    )
    return bool(re.search(r"(?<!\d)2026(?!\d)|20\s*09\s*2026", context, re.IGNORECASE))


def _number_header(value: str) -> bool:
    key = _key(value)
    return key in {"№", "номер", "№ уик", "номер уик"} or bool(
        re.search(r"(?:номер|№)\s+(?:избирательного\s+участка|уик)\b", key)
    )


def _address_header(value: str) -> bool:
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
    return key == "телефон" or key.startswith(("телефон ", "телефон, "))


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
    if re.search(
        r"(?:избирательн.*комисс|адрес уик|место нахождения участковой)", context
    ):
        return "commission"
    return "generic"


def _number(value: str) -> int | None:
    if not re.fullmatch(r"\s*\d+(?:\.0+)?\s*", value):
        return None
    number = int(float(value))
    return number if 0 < number < 100_000 else None


def _address(value: str) -> str:
    text = _clean(value)
    if len(text) < 8 or not _CYRILLIC_OR_LATIN_RE.search(text) or "..." in text or "…" in text:
        return ""
    return text


def _phone(value: str) -> str:
    text = _clean(value)
    return text if sum(character.isdigit() for character in text) >= 5 else ""


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
    if not number_columns or not address_columns:
        return []

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

    result: list[PrecinctDocumentRow] = []
    for row in rows[data_start:]:
        raw_number = row[number_column] if number_column < len(row) else ""
        number = _number(raw_number)
        commission_address_value = _address(value(row, commission_address))
        commission_phone_value = _phone(value(row, commission_phone))
        raw_voting_address = value(row, voting_address)
        raw_voting_phone = value(row, voting_phone)
        voting_address_value = _address(raw_voting_address)
        voting_phone_value = _phone(raw_voting_phone)
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
        result.append(
            PrecinctDocumentRow(
                number=number,
                voting_address=voting_address_value,
                voting_phone=voting_phone_value,
                commission_address=commission_address_value,
                commission_phone=commission_phone_value,
            )
        )
    return result


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
    records: dict[int, PrecinctDocumentRow] = {}
    conflicts: set[int] = set()
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
            previous = records.get(record.number)
            if previous is not None and previous != record:
                conflicts.add(record.number)
            else:
                records[record.number] = record
    for number in conflicts:
        records.pop(number, None)
    return tuple(records[number] for number in sorted(records))
