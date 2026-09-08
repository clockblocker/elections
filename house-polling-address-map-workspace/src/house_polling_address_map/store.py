"""SQLite persistence and content-addressed evidence storage."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from collections.abc import Iterable, Iterator
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
from typing import Self

from .models import (
    AddressIdentity,
    AssignmentEvidence,
    AssignmentStatus,
    BuildingAddress,
    Coverage,
    PollingStation,
    Provenance,
    ResolvedMapping,
    SourceKind,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
    address_id TEXT PRIMARY KEY,
    namespace TEXT NOT NULL,
    source_value TEXT NOT NULL,
    region_code TEXT NOT NULL,
    formatted_address TEXT NOT NULL,
    municipality TEXT,
    locality TEXT,
    street TEXT,
    house TEXT,
    postal_code TEXT,
    UNIQUE(namespace, source_value)
);
CREATE INDEX IF NOT EXISTS buildings_region_idx ON buildings(region_code);

CREATE TABLE IF NOT EXISTS polling_stations (
    station_id TEXT PRIMARY KEY,
    region_code TEXT NOT NULL,
    uik_number INTEGER NOT NULL CHECK (uik_number > 0),
    polling_place_address TEXT NOT NULL,
    commission_address TEXT,
    telephone TEXT,
    UNIQUE(region_code, uik_number)
);

CREATE TABLE IF NOT EXISTS raw_responses (
    sha256 TEXT PRIMARY KEY CHECK(length(sha256) = 64),
    byte_length INTEGER NOT NULL CHECK(byte_length >= 0),
    media_type TEXT,
    relative_path TEXT NOT NULL UNIQUE,
    stored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assignments (
    evidence_id TEXT PRIMARY KEY,
    address_id TEXT NOT NULL REFERENCES buildings(address_id),
    station_id TEXT REFERENCES polling_stations(station_id),
    status TEXT NOT NULL CHECK(status IN ('resolved', 'no_match', 'ambiguous', 'failed')),
    source_kind TEXT NOT NULL,
    source_url TEXT NOT NULL,
    retrieved_at TEXT NOT NULL,
    method TEXT NOT NULL,
    raw_sha256 TEXT REFERENCES raw_responses(sha256),
    valid_from TEXT,
    valid_to TEXT,
    note TEXT,
    CHECK ((status = 'resolved' AND station_id IS NOT NULL)
        OR (status != 'resolved' AND station_id IS NULL)),
    CHECK (valid_from IS NULL OR valid_to IS NULL OR valid_from <= valid_to)
);
CREATE INDEX IF NOT EXISTS assignments_address_idx
    ON assignments(address_id, retrieved_at DESC);
CREATE INDEX IF NOT EXISTS assignments_station_idx ON assignments(station_id);
"""


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if value else None


def _datetime_text(value: datetime) -> str:
    return value.isoformat()


class PollingMapStore:
    """Persistent map with a small domain-oriented interface.

    Descriptive building and station rows are updated in place.  Assignment
    evidence is immutable and content-identified, preserving every historical
    observation while making retries safe.
    """

    def __init__(self, database: str | Path, raw_directory: str | Path) -> None:
        self.database = Path(database) if database != ":memory:" else database
        self.raw_directory = Path(raw_directory)
        if isinstance(self.database, Path):
            self.database.parent.mkdir(parents=True, exist_ok=True)
        self.raw_directory.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.database)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.executescript(_SCHEMA)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    def upsert_building(self, building: BuildingAddress) -> str:
        """Insert or refresh one canonical building and return its stable key."""

        self._upsert_building(building)
        self._connection.commit()
        return building.identity.key

    def upsert_buildings(
        self, buildings: Iterable[BuildingAddress], *, batch_size: int = 10_000
    ) -> int:
        """Stream canonical buildings into SQLite using bounded transactions."""

        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        count = 0
        for building in buildings:
            self._upsert_building(building)
            count += 1
            if count % batch_size == 0:
                self._connection.commit()
        self._connection.commit()
        return count

    def _upsert_building(self, building: BuildingAddress) -> None:
        self._connection.execute(
            """
            INSERT INTO buildings (
                address_id, namespace, source_value, region_code, formatted_address,
                municipality, locality, street, house, postal_code
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address_id) DO UPDATE SET
                region_code = excluded.region_code,
                formatted_address = excluded.formatted_address,
                municipality = excluded.municipality,
                locality = excluded.locality,
                street = excluded.street,
                house = excluded.house,
                postal_code = excluded.postal_code
            """,
            (
                building.identity.key,
                building.identity.namespace,
                building.identity.value,
                building.region_code,
                building.formatted_address,
                building.municipality,
                building.locality,
                building.street,
                building.house,
                building.postal_code,
            ),
        )

    def upsert_station(self, station: PollingStation) -> str:
        """Insert or refresh one UIK and return its region-qualified key."""

        self._connection.execute(
            """
            INSERT INTO polling_stations (
                station_id, region_code, uik_number, polling_place_address,
                commission_address, telephone
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(station_id) DO UPDATE SET
                polling_place_address = excluded.polling_place_address,
                commission_address = excluded.commission_address,
                telephone = excluded.telephone
            """,
            (
                station.station_id,
                station.region_code,
                station.uik_number,
                station.polling_place_address,
                station.commission_address,
                station.telephone,
            ),
        )
        self._connection.commit()
        return station.station_id

    def record_assignment(self, evidence: AssignmentEvidence) -> str:
        """Record immutable evidence, returning its idempotency key.

        The referenced building and, for a resolved result, station must already
        have been upserted.  A raw digest must likewise have been stored first.
        """

        try:
            self._connection.execute(
                """
                INSERT INTO assignments (
                    evidence_id, address_id, station_id, status, source_kind,
                    source_url, retrieved_at, method, raw_sha256, valid_from,
                    valid_to, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(evidence_id) DO NOTHING
                """,
                (
                    evidence.evidence_id,
                    evidence.address.key,
                    evidence.station_id,
                    evidence.status.value,
                    evidence.provenance.source.value,
                    evidence.provenance.source_url,
                    _datetime_text(evidence.provenance.retrieved_at),
                    evidence.provenance.method,
                    evidence.provenance.raw_sha256,
                    _date_text(evidence.valid_from),
                    _date_text(evidence.valid_to),
                    evidence.note,
                ),
            )
        except sqlite3.IntegrityError as error:
            raise ValueError(
                "assignment references an unknown building, polling station, or raw response"
            ) from error
        self._connection.commit()
        return evidence.evidence_id

    def assignments_for(
        self, address: AddressIdentity, *, as_of: date | None = None
    ) -> tuple[AssignmentEvidence, ...]:
        """Return evidence newest first, optionally restricted to an inclusive date."""

        parameters: list[str] = [address.key]
        predicate = "address_id = ?"
        if as_of is not None:
            predicate += " AND (valid_from IS NULL OR valid_from <= ?)"
            predicate += " AND (valid_to IS NULL OR valid_to >= ?)"
            parameters.extend((as_of.isoformat(), as_of.isoformat()))
        rows = self._connection.execute(
            f"""SELECT * FROM assignments WHERE {predicate}
                ORDER BY retrieved_at DESC, evidence_id DESC""",
            parameters,
        ).fetchall()
        return tuple(self._evidence_from_row(row) for row in rows)

    def coverage(self, region_code: str | None = None, *, as_of: date | None = None) -> Coverage:
        """Summarize the latest applicable outcome for every canonical building.

        With no ``as_of`` date, all observations are eligible and the most
        recently retrieved one wins.  Supplying a date first filters evidence by
        its inclusive validity interval.  Buildings without eligible evidence are
        pending.
        """

        region_predicate = "" if region_code is None else "WHERE b.region_code = ?"
        parameters: list[str] = [] if region_code is None else [region_code]
        validity = ""
        if as_of is not None:
            validity = """AND (a.valid_from IS NULL OR a.valid_from <= ?)
                AND (a.valid_to IS NULL OR a.valid_to >= ?)"""
            parameters.extend((as_of.isoformat(), as_of.isoformat()))
        row = self._connection.execute(
            f"""
            WITH eligible AS (
                SELECT a.*, ROW_NUMBER() OVER (
                    PARTITION BY a.address_id
                    ORDER BY a.retrieved_at DESC, a.evidence_id DESC
                ) AS recency
                FROM assignments a
                JOIN buildings b ON b.address_id = a.address_id
                {region_predicate}
                {validity}
            ), latest AS (
                SELECT address_id, status FROM eligible WHERE recency = 1
            )
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(latest.status = 'resolved'), 0) AS resolved,
                COALESCE(SUM(latest.status = 'no_match'), 0) AS no_match,
                COALESCE(SUM(latest.status = 'ambiguous'), 0) AS ambiguous,
                COALESCE(SUM(latest.status = 'failed'), 0) AS failed,
                COALESCE(SUM(latest.status IS NULL), 0) AS pending
            FROM buildings all_buildings
            LEFT JOIN latest ON latest.address_id = all_buildings.address_id
            {"WHERE all_buildings.region_code = ?" if region_code is not None else ""}
            """,
            parameters + ([region_code] if region_code is not None else []),
        ).fetchone()
        assert row is not None
        return Coverage(*(int(row[name]) for name in Coverage.__dataclass_fields__))

    def pending_buildings(
        self,
        *,
        region_code: str | None = None,
        limit: int | None = None,
        include_failed: bool = False,
        after: tuple[str, str] | None = None,
    ) -> tuple[BuildingAddress, ...]:
        """Return lookup-eligible buildings in stable keyset order.

        By default only never-attempted buildings are returned.  Setting
        ``include_failed`` also returns buildings whose newest evidence is a
        failure, allowing a later process invocation to retry transient CEC
        errors.  ``after`` is a ``(region_code, address_id)`` cursor.
        """

        if limit is not None and limit <= 0:
            raise ValueError("limit must be positive")
        parameters: list[object] = [int(include_failed)]
        region_predicate = ""
        if region_code is not None:
            region_predicate = "AND b.region_code = ?"
            parameters.append(region_code)
        cursor_predicate = ""
        if after is not None:
            cursor_predicate = """AND (
                b.region_code > ? OR (b.region_code = ? AND b.address_id > ?)
            )"""
            parameters.extend((after[0], after[0], after[1]))
        limit_clause = ""
        if limit is not None:
            limit_clause = "LIMIT ?"
            parameters.append(limit)
        rows = self._connection.execute(
            f"""
            WITH latest AS (
                SELECT address_id, status
                  FROM (
                    SELECT address_id, status, ROW_NUMBER() OVER (
                        PARTITION BY address_id
                        ORDER BY retrieved_at DESC, evidence_id DESC
                    ) AS recency
                      FROM assignments
                  )
                 WHERE recency = 1
            )
            SELECT b.*
              FROM buildings b
              LEFT JOIN latest ON latest.address_id = b.address_id
             WHERE (latest.address_id IS NULL OR (? = 1 AND latest.status = 'failed'))
             {region_predicate}
             {cursor_predicate}
             ORDER BY b.region_code, b.address_id
             {limit_clause}
            """,
            parameters,
        ).fetchall()
        return tuple(
            BuildingAddress(
                identity=AddressIdentity(row["namespace"], row["source_value"]),
                region_code=row["region_code"],
                formatted_address=row["formatted_address"],
                municipality=row["municipality"],
                locality=row["locality"],
                street=row["street"],
                house=row["house"],
                postal_code=row["postal_code"],
            )
            for row in rows
        )

    def iter_latest_mappings(self, *, region_code: str | None = None) -> Iterator[ResolvedMapping]:
        """Stream the latest resolved observation for each mapped address."""

        predicate = "" if region_code is None else "WHERE b.region_code = ?"
        parameters = () if region_code is None else (region_code,)
        rows = self._connection.execute(
            f"""
            WITH ranked AS (
                SELECT a.*, ROW_NUMBER() OVER (
                    PARTITION BY a.address_id
                    ORDER BY a.retrieved_at DESC, a.evidence_id DESC
                ) AS recency
                  FROM assignments a
                  JOIN buildings b ON b.address_id = a.address_id
                  {predicate}
            )
            SELECT b.address_id, b.region_code, b.formatted_address,
                   s.station_id, s.uik_number, s.polling_place_address,
                   s.commission_address, s.telephone, r.source_url,
                   r.retrieved_at, r.raw_sha256
              FROM ranked r
              JOIN buildings b ON b.address_id = r.address_id
              JOIN polling_stations s ON s.station_id = r.station_id
             WHERE r.recency = 1 AND r.status = 'resolved'
             ORDER BY b.region_code, b.address_id
            """,
            parameters,
        )
        for row in rows:
            yield ResolvedMapping(
                address_id=row["address_id"],
                region_code=row["region_code"],
                address=row["formatted_address"],
                station_id=row["station_id"],
                uik_number=int(row["uik_number"]),
                polling_place_address=row["polling_place_address"],
                commission_address=row["commission_address"],
                telephone=row["telephone"],
                source_url=row["source_url"],
                retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
                raw_sha256=row["raw_sha256"],
            )

    def put_raw_response(self, body: bytes, *, media_type: str | None = None) -> str:
        """Store raw bytes exactly once and return their SHA-256 digest."""

        if not isinstance(body, bytes):
            raise TypeError("body must be bytes")
        digest = sha256(body).hexdigest()
        relative_path = Path(digest[:2]) / digest[2:4] / digest
        target = self.raw_directory / relative_path
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=".raw-")
            try:
                with os.fdopen(descriptor, "wb") as temporary:
                    temporary.write(body)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_name, target)
            finally:
                if os.path.exists(temporary_name):
                    os.unlink(temporary_name)
        elif target.read_bytes() != body:
            raise OSError(f"content-address collision or corrupt raw artifact: {digest}")

        self._connection.execute(
            """
            INSERT INTO raw_responses (
                sha256, byte_length, media_type, relative_path, stored_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(sha256) DO UPDATE SET
                media_type = COALESCE(raw_responses.media_type, excluded.media_type)
            """,
            (
                digest,
                len(body),
                media_type,
                relative_path.as_posix(),
                datetime.now().astimezone().isoformat(),
            ),
        )
        self._connection.commit()
        return digest

    def read_raw_response(self, digest: str) -> bytes:
        """Read raw evidence by digest, checking its integrity before returning it."""

        row = self._connection.execute(
            "SELECT relative_path FROM raw_responses WHERE sha256 = ?", (digest.lower(),)
        ).fetchone()
        if row is None:
            raise KeyError(digest)
        body = (self.raw_directory / row["relative_path"]).read_bytes()
        if sha256(body).hexdigest() != digest.lower():
            raise OSError(f"raw artifact failed integrity check: {digest}")
        return body

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> AssignmentEvidence:
        provenance = Provenance(
            source=SourceKind(row["source_kind"]),
            source_url=row["source_url"],
            retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
            method=row["method"],
            raw_sha256=row["raw_sha256"],
        )
        return AssignmentEvidence(
            address=AddressIdentity.from_key(row["address_id"]),
            station_id=row["station_id"],
            status=AssignmentStatus(row["status"]),
            provenance=provenance,
            valid_from=date.fromisoformat(row["valid_from"]) if row["valid_from"] else None,
            valid_to=date.fromisoformat(row["valid_to"]) if row["valid_to"] else None,
            note=row["note"],
        )
