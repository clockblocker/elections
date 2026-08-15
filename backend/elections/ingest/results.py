from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, TextIO

from sqlalchemy import delete, insert, select
from sqlalchemy.orm import Session

from elections.ingest.common import (
    clean,
    extract_number,
    finish_run,
    parse_int,
    register_artifact,
    start_run,
)
from elections.models import (
    Ballot,
    BallotAccounting,
    BallotKind,
    BallotOption,
    Election,
    ImportReject,
    MatchStatus,
    ResultRecord,
    SpecialType,
    ValidationStatus,
    Vote,
)
from elections.sources import Artifact

ACCOUNTING_FIELDS = (
    "registered_voters",
    "ballots_received",
    "ballots_issued_early",
    "ballots_issued_at_station",
    "ballots_issued_outside",
    "ballots_cancelled",
    "portable_boxes_ballots",
    "stationary_boxes_ballots",
    "invalid_ballots",
    "valid_ballots",
    "lost_ballots",
    "unaccounted_ballots",
)
GEO_COLUMNS = ("level", "region", "oik", "tik", "uik")


@contextmanager
def _open_csv(path: Path, archive_member: str | None = None) -> Iterator[TextIO]:
    if path.suffix.casefold() == ".zip":
        with zipfile.ZipFile(path) as archive:
            candidates = [name for name in archive.namelist() if name.casefold().endswith(".csv")]
            member = archive_member or (candidates[0] if len(candidates) == 1 else None)
            if not member or member not in archive.namelist():
                raise ValueError(f"cannot choose CSV member from {path}")
            with archive.open(member) as raw:
                yield io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
    else:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            yield stream


def _classify(row: dict[str, str]) -> SpecialType:
    haystack = " ".join(clean(row.get(key)) or "" for key in (*GEO_COLUMNS, "url")).casefold()
    if "дистанцион" in haystack or re.search(r"(^|\W)дэг($|\W)", haystack):
        return SpecialType.DEG
    if any(token in haystack for token in ("зарубеж", "за пределами", "иностран")):
        return SpecialType.FOREIGN
    if any(token in haystack for token in ("временн", "мест временного")):
        return SpecialType.TEMPORARY
    if extract_number(row.get("uik")) is None:
        return SpecialType.OTHER
    return SpecialType.NONE


def _ensure_ballot(session: Session) -> Ballot:
    election = session.scalar(select(Election).where(Election.slug == "duma-2021"))
    if election is None:
        election = Election(
            slug="duma-2021",
            name="State Duma election, eighth convocation",
            election_date=date(2021, 9, 19),
        )
        session.add(election)
        session.flush()
    ballot = session.scalar(
        select(Ballot).where(
            Ballot.election_id == election.id, Ballot.kind == BallotKind.PARTY_LIST
        )
    )
    if ballot is None:
        ballot = Ballot(
            election_id=election.id,
            kind=BallotKind.PARTY_LIST,
            name="Federal party list",
        )
        session.add(ballot)
        session.flush()
    return ballot


def _layout(fieldnames: list[str]) -> tuple[list[str], str]:
    if len(fieldnames) < 5 + len(ACCOUNTING_FIELDS) + 1:
        raise ValueError("results CSV does not contain geography and 12 accounting columns")
    url_column = "url" if "url" in fieldnames else fieldnames[-1]
    start = 5 + len(ACCOUNTING_FIELDS)
    options = [name for name in fieldnames[start:] if name != url_column]
    if not options:
        raise ValueError("results CSV has no ballot option columns")
    return options, url_column


def _row_values(
    row: dict[str, str],
    fieldnames: list[str],
    ballot_id: int,
    artifact_id: int,
    row_number: int,
    url_column: str,
) -> tuple[dict[str, Any], dict[str, int]]:
    values = [row.get(fieldnames[5 + offset]) for offset in range(len(ACCOUNTING_FIELDS))]
    accounting = {
        field: parse_int(value, field)
        for field, value in zip(ACCOUNTING_FIELDS, values, strict=True)
    }
    special = _classify(row)
    result = {
        "source_artifact_id": artifact_id,
        "source_row_number": row_number,
        "ballot_id": ballot_id,
        "region_name": clean(row.get("region")) or "",
        "oik_name": clean(row.get("oik")),
        "tik_name": clean(row.get("tik")),
        "uik_number": extract_number(row.get("uik")),
        "gas_vybory_id": clean(row.get("gas_vybory_id")),
        "source_url": clean(row.get(url_column)),
        "special_type": special,
        "is_deg": special == SpecialType.DEG,
        "raw_json": row,
        "match_status": MatchStatus.SPECIAL if special != SpecialType.NONE else MatchStatus.PENDING,
        "validation_status": ValidationStatus.NOT_VALIDATED,
    }
    return result, accounting


def _batched_insert(
    session: Session, table: Any, rows: list[dict[str, Any]], size: int = 2000
) -> None:
    for offset in range(0, len(rows), size):
        session.execute(insert(table), rows[offset : offset + size])


def import_results(session: Session, artifact: Artifact, path: Path) -> dict[str, int]:
    source = register_artifact(session, artifact, path)
    ballot = _ensure_ballot(session)
    run = start_run(session, "results", source.id)

    # Rebuild this artifact as a unit so reruns cannot duplicate source records.
    session.execute(delete(ImportReject).where(ImportReject.artifact_id == source.id))
    session.execute(delete(ResultRecord).where(ResultRecord.source_artifact_id == source.id))

    archive_member = artifact.metadata.get("archive_member")
    with _open_csv(path, archive_member) as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError("results CSV is empty")
        option_names, url_column = _layout(reader.fieldnames)
        option_ids: dict[str, int] = {}
        for position, name in enumerate(option_names, start=1):
            option = session.scalar(
                select(BallotOption).where(
                    BallotOption.ballot_id == ballot.id, BallotOption.position == position
                )
            )
            if option is None:
                option = BallotOption(
                    ballot_id=ballot.id, position=position, name=name, short_name=None
                )
                session.add(option)
                session.flush()
            else:
                option.name = name
            option_ids[name] = option.id

        result_rows: list[dict[str, Any]] = []
        rejected = 0
        total = 0
        for row_number, row in enumerate(reader, start=2):
            total += 1
            try:
                result, _ = _row_values(
                    row, reader.fieldnames, ballot.id, source.id, row_number, url_column
                )
                result_rows.append(result)
            except ValueError as exc:
                rejected += 1
                session.add(
                    ImportReject(
                        run_id=run.id,
                        artifact_id=source.id,
                        source_row_number=row_number,
                        source_record_id=clean(row.get("uik")),
                        error=str(exc),
                        raw_json=row,
                    )
                )
        _batched_insert(session, ResultRecord.__table__, result_rows)
        session.flush()

    result_ids = dict(
        session.execute(
            select(ResultRecord.source_row_number, ResultRecord.id).where(
                ResultRecord.source_artifact_id == source.id
            )
        ).all()
    )
    accounting_rows: list[dict[str, Any]] = []
    vote_rows: list[dict[str, Any]] = []
    with _open_csv(path, archive_member) as stream:
        reader = csv.DictReader(stream)
        assert reader.fieldnames is not None
        option_names, url_column = _layout(reader.fieldnames)
        for row_number, row in enumerate(reader, start=2):
            result_id = result_ids.get(row_number)
            if result_id is None:
                continue
            _, accounting = _row_values(
                row, reader.fieldnames, ballot.id, source.id, row_number, url_column
            )
            accounting_rows.append({"result_record_id": result_id, **accounting})
            for option_name in option_names:
                vote_rows.append(
                    {
                        "result_record_id": result_id,
                        "option_id": option_ids[option_name],
                        "votes": parse_int(row.get(option_name), option_name),
                    }
                )
    _batched_insert(session, BallotAccounting.__table__, accounting_rows)
    _batched_insert(session, Vote.__table__, vote_rows)
    stats = {
        "source_rows": total,
        "imported_records": len(result_rows),
        "rejected_records": rejected,
        "vote_rows": len(vote_rows),
        "options": len(option_ids),
    }
    finish_run(run, stats)
    session.flush()
    return stats
