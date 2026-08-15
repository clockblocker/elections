from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

from elections.ingest.results import ACCOUNTING_FIELDS


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() == "tr":
            self._row = []
        elif tag.casefold() in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag.casefold() == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        normalized = tag.casefold()
        if normalized in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif normalized == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


_UIK = re.compile(r"(?:УИК\s*)?(?:№\s*)?(\d{1,6})(?:\D.*)?$", re.IGNORECASE)
_LEADING_POSITION = re.compile(r"^\s*(\d+)\s*[.)-]?\s*(.+)$")


def _normalized(value: str) -> str:
    return value.casefold().replace("ё", "е")


def _accounting_field(label: str) -> str | None:
    value = _normalized(label)
    if ("не учтенных" in value or "неучтенных" in value) and "получении" in value:
        return "unaccounted_ballots"
    if "утраченных" in value:
        return "lost_ballots"
    if ("включенных" in value or "внесенных" in value) and "список" in value:
        return "registered_voters"
    if "полученных участковой" in value or "полученных уик" in value:
        return "ballots_received"
    if "проголосовавшим досрочно" in value:
        return "ballots_issued_early"
    if "выданных" in value and "вне помещения" in value:
        return "ballots_issued_outside"
    if "выданных" in value and "в помещении" in value:
        return "ballots_issued_at_station"
    tests = (
        ("ballots_cancelled", "погашенных"),
        ("portable_boxes_ballots", "в переносных ящиках"),
        ("stationary_boxes_ballots", "в стационарных ящиках"),
        ("invalid_ballots", "недействительных"),
        ("valid_ballots", "действительных"),
    )
    for field, needle in tests:
        if needle in value:
            return field
    return None


def _integer(value: str) -> int | None:
    compact = value.replace("\xa0", "").replace(" ", "")
    return int(compact) if re.fullmatch(r"\d+", compact) else None


def _header(rows: list[list[str]]) -> tuple[int, dict[int, str]]:
    best: tuple[int, dict[int, str]] | None = None
    for row_index, row in enumerate(rows):
        columns: dict[int, str] = {}
        has_uik_word = any("уик" in _normalized(cell) for cell in row)
        for column, cell in enumerate(row):
            match = _UIK.search(cell.strip())
            if match and (has_uik_word or "уик" in _normalized(cell)):
                columns[column] = match.group(1)
            elif "дэг" in _normalized(cell) or "дистанцион" in _normalized(cell):
                columns[column] = "ДЭГ"
        if columns and (best is None or len(columns) > len(best[1])):
            best = (row_index, columns)
    if best is None:
        raise ValueError("single-member HTML contains no UIK column header")
    return best


def _label(row: list[str], first_value_column: int) -> str:
    candidates = [cell for cell in row[:first_value_column] if cell and _integer(cell) is None]
    return candidates[-1] if candidates else ""


def parse_single_member_html(
    html: str,
    *,
    region_name: str,
    oik_code: str,
    oik_name: str | None,
    tik_name: str | None,
    source_url: str,
) -> dict[str, Any]:
    """Parse the CEC's transposed UIK protocol table into the canonical import shape."""

    parser = _TableParser()
    parser.feed(html)
    header_index, uik_columns = _header(parser.rows)
    identifiers = list(uik_columns.values())
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("single-member HTML contains duplicate UIK/special columns")
    first_value_column = min(uik_columns)
    accounting: dict[str, dict[str, int]] = {uik: {} for uik in uik_columns.values()}
    candidate_rows: list[tuple[int, str, dict[str, int]]] = []
    next_position = 1
    for row in parser.rows[header_index + 1 :]:
        if len(row) <= max(uik_columns):
            continue
        values: dict[str, int] = {}
        for column, uik in uik_columns.items():
            number = _integer(row[column])
            if number is not None:
                values[uik] = number
        label = _label(row, first_value_column)
        field = _accounting_field(label)
        if field:
            if len(values) != len(uik_columns):
                raise ValueError(f"accounting row {field} contains a non-integer value")
            for uik, value in values.items():
                accounting[uik][field] = value
            continue
        if not label or len(accounting[next(iter(accounting))]) < len(ACCOUNTING_FIELDS):
            continue
        match = _LEADING_POSITION.match(label)
        position = int(match.group(1)) if match else next_position
        full_name = match.group(2).strip() if match else label.strip()
        if any(token in _normalized(full_name) for token in ("итого", "число голосов")):
            continue
        if len(values) != len(uik_columns):
            raise ValueError(f"candidate row {position} contains a non-integer value")
        candidate_rows.append((position, full_name, values))
        next_position = max(next_position, position + 1)
    missing = {
        uik: sorted(set(ACCOUNTING_FIELDS) - set(values))
        for uik, values in accounting.items()
        if set(values) != set(ACCOUNTING_FIELDS)
    }
    if missing:
        raise ValueError(f"single-member HTML is missing accounting fields: {missing}")
    if not candidate_rows:
        raise ValueError("single-member HTML contains no candidate rows")
    positions = [position for position, _, _ in candidate_rows]
    if len(set(positions)) != len(positions):
        raise ValueError("single-member HTML contains duplicate candidate positions")
    candidates = [
        {
            "position": position,
            "full_name": full_name,
            "party_affiliation": None,
            "is_self_nominated": "самовыдв" in _normalized(full_name),
            "source_record_id": f"{oik_code}:{position}",
        }
        for position, full_name, _ in candidate_rows
    ]
    protocols = [
        {
            "tik_name": tik_name,
            "uik_number": uik,
            "source_record_id": f"{oik_code}:{tik_name or '-'}:{uik}",
            "source_url": source_url,
            "accounting": values,
            "votes": {str(position): votes[uik] for position, _, votes in candidate_rows},
        }
        for uik, values in accounting.items()
    ]
    return {
        "region_name": region_name,
        "oik_code": oik_code,
        "oik_name": oik_name or f"OIK {oik_code}",
        "source_url": source_url,
        "candidates": candidates,
        "protocols": protocols,
    }
