from __future__ import annotations

import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import py7zr
from sqlalchemy import delete, insert, select, update
from sqlalchemy.orm import Session

from elections.ingest.common import (
    clean,
    extract_number,
    finish_run,
    parse_date,
    register_artifact,
    start_run,
)
from elections.models import (
    Commission,
    CommissionMembership,
    CommissionType,
    ImportReject,
    MatchEvidence,
    MatchStatus,
    ResultRecord,
)
from elections.sources import Artifact

SENSITIVE_TOKENS = ("phone", "телефон", "email", "e-mail", "почта", "fax")


@contextmanager
def _database_path(path: Path) -> Iterator[Path]:
    if path.suffix.casefold() != ".7z":
        yield path
        return
    with tempfile.TemporaryDirectory(prefix="elections-commissions-") as directory:
        with py7zr.SevenZipFile(path) as archive:
            archive.extractall(directory)
        candidates = list(Path(directory).rglob("*.sqlite")) + list(Path(directory).rglob("*.db"))
        if len(candidates) != 1:
            raise ValueError(f"expected one SQLite database in {path}, found {len(candidates)}")
        yield candidates[0]


def _public_raw(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in row.items()
        if not any(token in key.casefold() for token in SENSITIVE_TOKENS)
    }


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _rows(connection: sqlite3.Connection, table: str) -> Iterator[dict[str, Any]]:
    connection.row_factory = sqlite3.Row
    cursor = connection.execute(f"SELECT * FROM {table}")  # noqa: S608 - fixed table names only
    for row in cursor:
        yield dict(row)


def _insert_batches(session: Session, table: Any, rows: list[dict[str, Any]]) -> None:
    for offset in range(0, len(rows), 2000):
        session.execute(insert(table), rows[offset : offset + 2000])


def import_commissions(session: Session, artifact: Artifact, path: Path) -> dict[str, int]:
    source = register_artifact(session, artifact, path)
    run = start_run(session, "commissions", source.id)

    old_ids = select(Commission.id).where(Commission.source_artifact_id == source.id)
    session.execute(
        update(ResultRecord)
        .where(ResultRecord.matched_commission_id.in_(old_ids))
        .values(matched_commission_id=None, match_status=MatchStatus.PENDING)
    )
    session.execute(delete(MatchEvidence).where(MatchEvidence.commission_id.in_(old_ids)))
    session.execute(
        delete(CommissionMembership).where(CommissionMembership.commission_id.in_(old_ids))
    )
    session.execute(
        update(Commission).where(Commission.source_artifact_id == source.id).values(parent_id=None)
    )
    session.execute(delete(Commission).where(Commission.source_artifact_id == source.id))
    session.execute(delete(ImportReject).where(ImportReject.artifact_id == source.id))

    commissions: list[dict[str, Any]] = []
    memberships_raw: list[dict[str, Any]] = []
    rejected = 0
    ignored_non_uik_tik = 0
    with _database_path(path) as database:
        with sqlite3.connect(database) as connection:
            for row_number, raw in enumerate(_rows(connection, "cik_uik"), start=1):
                source_id = clean(raw.get("id"))
                type_value = (clean(raw.get("type_ik")) or "").casefold()
                if type_value not in {CommissionType.UIK.value, CommissionType.TIK.value}:
                    ignored_non_uik_tik += 1
                    continue
                try:
                    if not source_id:
                        raise ValueError("commission source id is empty")
                    name = clean(raw.get("name"))
                    region = clean(raw.get("region"))
                    if not name or not region:
                        raise ValueError("commission name or region is empty")
                    commissions.append(
                        {
                            "source_artifact_id": source.id,
                            "source_record_id": source_id,
                            "gas_vybory_id": clean(raw.get("iz_id")),
                            "parent_source_id": clean(raw.get("parent_id")),
                            "type": CommissionType(type_value),
                            "region": region,
                            "name": name,
                            "number": extract_number(name),
                            "address": clean(raw.get("address"))
                            or clean(raw.get("address_voteroom")),
                            "latitude": _decimal(raw.get("lat_ik") or raw.get("lat_voteroom")),
                            "longitude": _decimal(raw.get("lon_ik") or raw.get("lon_voteroom")),
                            "term_start": None,
                            "term_end": parse_date(raw.get("end_date")),
                            "raw_json": _public_raw(raw),
                        }
                    )
                except ValueError as exc:
                    rejected += 1
                    session.add(
                        ImportReject(
                            run_id=run.id,
                            artifact_id=source.id,
                            source_row_number=row_number,
                            source_record_id=source_id,
                            error=str(exc),
                            raw_json=_public_raw(raw),
                        )
                    )
            memberships_raw = list(_rows(connection, "cik_people"))

    _insert_batches(session, Commission.__table__, commissions)
    session.flush()
    commission_ids = dict(
        session.execute(
            select(Commission.source_record_id, Commission.id).where(
                Commission.source_artifact_id == source.id
            )
        ).all()
    )
    for row in commissions:
        parent_id = commission_ids.get(row["parent_source_id"])
        if parent_id:
            session.execute(
                update(Commission)
                .where(Commission.id == commission_ids[row["source_record_id"]])
                .values(parent_id=parent_id)
            )

    membership_rows: list[dict[str, Any]] = []
    orphan_memberships = 0
    member_offset = len(commissions) + ignored_non_uik_tik + 1
    for index, raw in enumerate(memberships_raw, start=member_offset):
        commission_id = commission_ids.get(clean(raw.get("ik_id")) or "")
        if commission_id is None:
            orphan_memberships += 1
            continue  # Membership of an intentionally excluded regional commission.
        source_record_id = clean(raw.get("id"))
        full_name = clean(raw.get("fio"))
        if not source_record_id or not full_name:
            rejected += 1
            session.add(
                ImportReject(
                    run_id=run.id,
                    artifact_id=source.id,
                    source_row_number=index,
                    source_record_id=source_record_id,
                    error="membership source id or full name is empty",
                    raw_json=_public_raw(raw),
                )
            )
            continue
        membership_rows.append(
            {
                "commission_id": commission_id,
                "source_record_id": source_record_id,
                "full_name": full_name,
                "role": clean(raw.get("post")),
                "nominator": clean(raw.get("party")),
                "term_start": None,
                "term_end": None,
                "raw_json": _public_raw(raw),
            }
        )
    _insert_batches(session, CommissionMembership.__table__, membership_rows)
    counts = {"uik": 0, "tik": 0}
    for row in commissions:
        counts[row["type"].value] += 1
    stats = {
        "commissions": len(commissions),
        "uik": counts["uik"],
        "tik": counts["tik"],
        "memberships": len(membership_rows),
        "ignored_non_uik_tik": ignored_non_uik_tik,
        "ignored_memberships_for_other_types": orphan_memberships,
        "rejected_records": rejected,
    }
    finish_run(run, stats)
    session.flush()
    return stats
