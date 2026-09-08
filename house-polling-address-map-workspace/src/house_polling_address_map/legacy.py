"""Resumable crawler for the CEC's 2015–2018 address hierarchy."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path

from .cec import CecLegacyTree, CecTreeNode
from .models import (
    AddressIdentity,
    AssignmentEvidence,
    AssignmentStatus,
    BuildingAddress,
    PollingStation,
    Provenance,
    SourceKind,
)
from .store import PollingMapStore

_REGION_CODES = {
    "Республика Адыгея (Адыгея)": "1",
    "Республика Алтай": "2",
    "Республика Башкортостан": "3",
    "Республика Бурятия": "4",
    "Республика Дагестан": "5",
    "Республика Ингушетия": "6",
    "Кабардино-Балкарская Республика": "7",
    "Республика Калмыкия": "8",
    "Карачаево-Черкесская Республика": "9",
    "Республика Карелия": "10",
    "Республика Коми": "11",
    "Республика Марий Эл": "12",
    "Республика Мордовия": "13",
    "Республика Саха (Якутия)": "14",
    "Республика Северная Осетия-Алания": "15",
    "Республика Татарстан (Татарстан)": "16",
    "Республика Тыва": "17",
    "Удмуртская Республика": "18",
    "Республика Хакасия": "19",
    "Чеченская Республика": "20",
    "Чувашская Республика - Чувашия": "21",
    "Алтайский край": "22",
    "Краснодарский край": "23",
    "Красноярский край": "24",
    "Приморский край": "25",
    "Ставропольский край": "26",
    "Хабаровский край": "27",
    "Амурская область": "28",
    "Архангельская область": "29",
    "Астраханская область": "30",
    "Белгородская область": "31",
    "Брянская область": "32",
    "Владимирская область": "33",
    "Волгоградская область": "34",
    "Вологодская область": "35",
    "Воронежская область": "36",
    "Ивановская область": "37",
    "Иркутская область": "38",
    "Калининградская область": "39",
    "Калужская область": "40",
    "Кемеровская область": "42",
    "Кемеровская область - Кузбасс": "42",
    "Кировская область": "43",
    "Костромская область": "44",
    "Курганская область": "45",
    "Курская область": "46",
    "Ленинградская область": "47",
    "Липецкая область": "48",
    "Магаданская область": "49",
    "Московская область": "50",
    "Мурманская область": "51",
    "Нижегородская область": "52",
    "Новгородская область": "53",
    "Новосибирская область": "54",
    "Омская область": "55",
    "Оренбургская область": "56",
    "Орловская область": "57",
    "Пензенская область": "58",
    "Псковская область": "60",
    "Ростовская область": "61",
    "Рязанская область": "62",
    "Самарская область": "63",
    "Саратовская область": "64",
    "Сахалинская область": "65",
    "Свердловская область": "66",
    "Смоленская область": "67",
    "Тамбовская область": "68",
    "Тверская область": "69",
    "Томская область": "70",
    "Тульская область": "71",
    "Тюменская область": "72",
    "Ульяновская область": "73",
    "Челябинская область": "74",
    "Ярославская область": "76",
    "город Москва": "77",
    "Москва": "77",
    "город Санкт-Петербург": "78",
    "Санкт-Петербург": "78",
    "Еврейская автономная область": "79",
    "Ненецкий автономный округ": "83",
    "Ханты-Мансийский автономный округ": "86",
    "Чукотский автономный округ": "87",
    "Ямало-Ненецкий автономный округ": "89",
    "Пермский край": "90",
    "Камчатский край": "91",
    "Забайкальский край": "92",
    "Республика Крым": "93",
    "город Севастополь": "94",
}


def _region_key(value: str) -> str:
    return re.sub(r"[\s‐‑‒–—-]+", " ", value.casefold()).strip()


_NORMALIZED_REGION_CODES = {_region_key(name): code for name, code in _REGION_CODES.items()}


@dataclass(frozen=True, slots=True)
class LegacyResult:
    status: AssignmentStatus
    uik_number: int | None = None
    commission_address: str | None = None
    polling_place_address: str | None = None
    telephone: str | None = None
    note: str | None = None


@dataclass(frozen=True, slots=True)
class CrawlSummary:
    requests: int
    pending_jobs: int
    resolved: int
    ambiguous: int
    failed: int


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _decode(body: bytes) -> str:
    for encoding in ("utf-8-sig", "windows-1251"):
        try:
            return body.decode(encoding)
        except UnicodeDecodeError:
            pass
    return body.decode("windows-1251", errors="replace")


def parse_legacy_result(body: bytes) -> LegacyResult:
    """Parse only explicitly labelled CEC result fields."""

    parser = _Text()
    parser.feed(_decode(body))
    lines = [re.sub(r"\s+", " ", part).strip() for part in "".join(parser.parts).splitlines()]
    lines = [line for line in lines if line]
    numbers = {
        int(value)
        for line in lines
        for value in re.findall(
            r"Участковая избирательная комиссия\s*№\s*(\d+)", line, re.IGNORECASE
        )
    }
    if len(numbers) > 1:
        return LegacyResult(AssignmentStatus.AMBIGUOUS, note="conflicting UIK numbers")
    number = next(iter(numbers), None)
    values: dict[str, str] = {}
    labels = {
        "Адрес помещения УИК": "commission",
        "Телефон УИК": "commission_phone",
        "Адрес помещения для голосования": "polling",
        "Телефон помещения для голосования": "polling_phone",
    }
    current_key: str | None = None
    for line in lines:
        matched = False
        for label, key in labels.items():
            match = re.search(rf"{re.escape(label)}\s*:\s*(.+)", line, re.IGNORECASE)
            if match:
                values[key] = match.group(1).strip()
                current_key = key
                matched = True
                break
        if not matched and current_key in {"commission", "polling"} and ":" not in line:
            values[current_key] = f"{values[current_key]} {line}"
    missing = []
    if number is None:
        missing.append("UIK number")
    if not values.get("polling"):
        missing.append("polling-place address")
    if missing:
        return LegacyResult(AssignmentStatus.FAILED, note="missing " + " and ".join(missing))
    return LegacyResult(
        AssignmentStatus.RESOLVED,
        uik_number=number,
        commission_address=values.get("commission"),
        polling_place_address=values["polling"],
        telephone=values.get("polling_phone") or values.get("commission_phone"),
    )


_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS legacy_jobs (
    crawl_id TEXT NOT NULL,
    job_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    node_id TEXT,
    address_id TEXT,
    result_token TEXT,
    region_code TEXT,
    path_json TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (crawl_id, job_key)
);
"""


class LegacyTreeCrawler:
    """Exhaustively walk the legacy tree with a durable, idempotent queue."""

    def __init__(
        self,
        tree: CecLegacyTree,
        store: PollingMapStore,
        *,
        crawl_id: str,
        delay_seconds: float = 0.5,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        state_database: str | Path | None = None,
    ) -> None:
        if not crawl_id.strip():
            raise ValueError("crawl_id must not be empty")
        if delay_seconds < 0:
            raise ValueError("delay_seconds must not be negative")
        if state_database is None:
            if not isinstance(store.database, Path):
                raise ValueError("state_database is required for an in-memory map store")
            state_database = store.database.with_name(f"{store.database.stem}-legacy-state.sqlite3")
        self.tree = tree
        self.store = store
        self.crawl_id = crawl_id
        self.delay_seconds = delay_seconds
        self.now = now
        self.state_database = Path(state_database)

    def run(self, *, request_limit: int | None = None) -> CrawlSummary:
        if request_limit is not None and request_limit <= 0:
            raise ValueError("request_limit must be positive")
        self.state_database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.state_database)
        connection.row_factory = sqlite3.Row
        connection.executescript(_STATE_SCHEMA)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(legacy_jobs)")}
        if "result_token" not in columns:
            connection.execute("ALTER TABLE legacy_jobs ADD COLUMN result_token TEXT")
        connection.execute(
            """INSERT OR IGNORE INTO legacy_jobs
               (crawl_id, job_key, kind, path_json) VALUES (?, 'root', 'root', '[]')""",
            (self.crawl_id,),
        )
        connection.commit()
        attempted: set[str] = set()
        requests = resolved = ambiguous = failed = 0
        try:
            while request_limit is None or requests < request_limit:
                rows = connection.execute(
                    """SELECT * FROM legacy_jobs
                       WHERE crawl_id = ? AND state = 'pending' ORDER BY job_key""",
                    (self.crawl_id,),
                ).fetchall()
                row = next((item for item in rows if item["job_key"] not in attempted), None)
                if row is None:
                    break
                attempted.add(row["job_key"])
                requests += 1
                try:
                    outcome = self._execute(connection, row)
                    resolved += outcome == AssignmentStatus.RESOLVED
                    ambiguous += outcome == AssignmentStatus.AMBIGUOUS
                    failed += outcome == AssignmentStatus.FAILED
                # Any one remote/parser/storage failure must leave this durable
                # job pending without aborting the nationwide crawl.
                except Exception as error:  # noqa: BLE001
                    connection.execute(
                        """UPDATE legacy_jobs SET attempts = attempts + 1, last_error = ?
                           WHERE crawl_id = ? AND job_key = ?""",
                        (str(error), self.crawl_id, row["job_key"]),
                    )
                    connection.commit()
                if self.delay_seconds and (request_limit is None or requests < request_limit):
                    time.sleep(self.delay_seconds)
            pending = connection.execute(
                "SELECT COUNT(*) FROM legacy_jobs WHERE crawl_id = ? AND state = 'pending'",
                (self.crawl_id,),
            ).fetchone()[0]
            return CrawlSummary(requests, int(pending), resolved, ambiguous, failed)
        finally:
            connection.close()

    def _execute(self, connection: sqlite3.Connection, row: sqlite3.Row) -> AssignmentStatus | None:
        kind = row["kind"]
        path = json.loads(row["path_json"])
        if kind == "root":
            _, _, body, nodes = self.tree.roots()
            self.store.put_raw_response(body, media_type="application/json; charset=windows-1251")
            self._enqueue_nodes(connection, nodes, [])
            outcome = None
        elif kind == "children":
            if row["result_token"] is None:
                _, _, body, nodes = self.tree.children(row["node_id"])
            else:
                _, _, body, nodes = self.tree.children(
                    row["node_id"], result_token=row["result_token"]
                )
            self.store.put_raw_response(body, media_type="application/json; charset=windows-1251")
            if nodes:
                self._enqueue_nodes(connection, nodes, path)
            else:
                self._enqueue_result(connection, row)
            outcome = None
        elif kind == "result":
            url, _, body = self.tree.result(row["address_id"])
            digest = self.store.put_raw_response(body, media_type="text/html; charset=windows-1251")
            result = parse_legacy_result(body)
            region_code = row["region_code"]
            if not region_code:
                raise ValueError("terminal legacy path has no recognized region")
            identity = AddressIdentity("cec-legacy", row["address_id"])
            building = BuildingAddress(
                identity,
                region_code,
                ", ".join(item["text"] for item in path if item["text"] != "Россия"),
                locality=self._component(path, {"4", "6"}),
                street=self._component(path, {"7"}),
                house=self._component(path, {"8", "11"}),
            )
            self.store.upsert_building(building)
            station_id = None
            if result.status is AssignmentStatus.RESOLVED:
                station = PollingStation(
                    region_code,
                    result.uik_number or 0,
                    result.polling_place_address or "",
                    result.commission_address,
                    result.telephone,
                )
                station_id = self.store.upsert_station(station)
            evidence = AssignmentEvidence(
                identity,
                result.status,
                Provenance(SourceKind.ARCHIVED_LOOKUP, url, self.now(), "cec-legacy-tree", digest),
                station_id=station_id,
                note=result.note,
            )
            self.store.record_assignment(evidence)
            outcome = result.status
        else:
            raise ValueError(f"unknown legacy job kind: {kind}")
        connection.execute(
            """UPDATE legacy_jobs SET state = 'done', attempts = attempts + 1, last_error = NULL
               WHERE crawl_id = ? AND job_key = ?""",
            (self.crawl_id, row["job_key"]),
        )
        connection.commit()
        return outcome

    def _enqueue_nodes(
        self,
        connection: sqlite3.Connection,
        nodes: tuple[CecTreeNode, ...],
        parent_path: list[dict[str, str]],
    ) -> None:
        for node in nodes:
            path = [*parent_path, {"text": node.text, "level_id": node.level_id or ""}]
            region = self._region(path)
            kind = "children" if node.has_children else "result"
            if kind == "result" and not node.address_id:
                continue
            connection.execute(
                """INSERT OR IGNORE INTO legacy_jobs
                   (crawl_id, job_key, kind, node_id, address_id, result_token,
                    region_code, path_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    self.crawl_id,
                    f"{kind}:{node.node_id}",
                    kind,
                    node.node_id,
                    node.address_id,
                    node.result_token,
                    region,
                    json.dumps(path, ensure_ascii=False),
                ),
            )
        connection.commit()

    def _enqueue_result(self, connection: sqlite3.Connection, row: sqlite3.Row) -> None:
        if not row["address_id"]:
            raise ValueError("terminal tree node has no address identifier")
        connection.execute(
            """INSERT OR IGNORE INTO legacy_jobs
               (crawl_id, job_key, kind, address_id, region_code, path_json)
               VALUES (?, ?, 'result', ?, ?, ?)""",
            (
                self.crawl_id,
                f"result:{row['address_id']}",
                row["address_id"],
                row["region_code"],
                row["path_json"],
            ),
        )

    @staticmethod
    def _component(path: list[dict[str, str]], levels: set[str]) -> str | None:
        values = [item["text"] for item in path if item["level_id"] in levels]
        return values[-1] if values else None

    @staticmethod
    def _region(path: list[dict[str, str]]) -> str | None:
        for item in path:
            code = _NORMALIZED_REGION_CODES.get(_region_key(item["text"]))
            if code:
                return code
        return None
