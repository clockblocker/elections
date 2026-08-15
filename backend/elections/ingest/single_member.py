from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from elections.acquisition import PRESERVED_STATUSES, load_snapshot_manifest
from elections.ingest.common import (
    clean,
    extract_number,
    finish_run,
    parse_int,
    register_artifact,
    start_run,
)
from elections.ingest.results import ACCOUNTING_FIELDS
from elections.models import (
    Ballot,
    BallotAccounting,
    BallotKind,
    Candidate,
    Election,
    Geography,
    GeographyType,
    ImportReject,
    MatchStatus,
    ResultRecord,
    SourceArtifact,
    SpecialType,
    ValidationStatus,
    Vote,
)
from elections.sources import Artifact, SourceError, verify_artifact

from .single_member_html import parse_single_member_html


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "unknown"


def _as_list(value: Any, field: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise ValueError(f"{field} must be a list of objects")
    return value


def _load_snapshot(path: Path) -> list[dict[str, Any]]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read single-member snapshot {path}: {exc}") from exc
    if isinstance(document, list):
        districts = document
    elif isinstance(document, dict):
        districts = document.get("districts")
    else:
        districts = None
    return _as_list(districts, "districts")


def _ensure_election(session: Session) -> Election:
    election = session.scalar(select(Election).where(Election.slug == "duma-2021"))
    if election is None:
        election = Election(
            slug="duma-2021",
            name="State Duma election, eighth convocation",
            election_date=date(2021, 9, 19),
        )
        session.add(election)
        session.flush()
    return election


def _ensure_geography(
    session: Session,
    *,
    kind: GeographyType,
    code: str,
    name: str,
    parent: Geography | None = None,
) -> Geography:
    geography = session.scalar(
        select(Geography).where(Geography.type == kind, Geography.code == code)
    )
    if geography is None:
        geography = Geography(type=kind, code=code, name=name, parent=parent)
        session.add(geography)
        session.flush()
    else:
        geography.name = name
        if parent is not None:
            geography.parent = parent
    return geography


def _district_identity(raw: dict[str, Any]) -> tuple[str, str, str]:
    region = clean(raw.get("region_name") or raw.get("region"))
    oik_code = clean(raw.get("oik_code") or raw.get("district_code") or raw.get("code"))
    oik_name = clean(raw.get("oik_name") or raw.get("district_name") or raw.get("name"))
    if not region or not oik_code or not oik_name:
        raise ValueError("district requires region_name, oik_code, and oik_name")
    return region, oik_code, oik_name


def _ensure_ballot(
    session: Session, election: Election, raw: dict[str, Any]
) -> tuple[Ballot, str, str, str]:
    region, oik_code, oik_name = _district_identity(raw)
    region_code = clean(raw.get("region_code")) or f"region:{_slug(region)}"
    region_geography = _ensure_geography(
        session, kind=GeographyType.REGION, code=region_code, name=region
    )
    oik = _ensure_geography(
        session,
        kind=GeographyType.OIK,
        code=oik_code,
        name=oik_name,
        parent=region_geography,
    )
    ballot = session.scalar(
        select(Ballot).where(
            Ballot.election_id == election.id,
            Ballot.kind == BallotKind.SINGLE_MEMBER,
            Ballot.scope_key == oik_code,
        )
    )
    if ballot is None:
        ballot = Ballot(
            election=election,
            kind=BallotKind.SINGLE_MEMBER,
            scope_key=oik_code,
            oik=oik,
            name=f"Single-member district {oik_name}",
        )
        session.add(ballot)
        session.flush()
    else:
        ballot.oik = oik
        ballot.name = f"Single-member district {oik_name}"
    return ballot, region, oik_code, oik_name


def _candidate_position(raw: dict[str, Any]) -> int:
    return parse_int(raw.get("position") or raw.get("ballot_position"), "candidate position")


def _ensure_candidates(
    session: Session,
    artifact_id: int,
    ballot: Ballot,
    oik_code: str,
    rows: Iterable[dict[str, Any]],
) -> dict[int, Candidate]:
    candidates: dict[int, Candidate] = {}
    for raw in rows:
        position = _candidate_position(raw)
        if position <= 0:
            raise ValueError("candidate position must be positive")
        if position in candidates:
            raise ValueError(f"duplicate candidate position {position}")
        full_name = clean(raw.get("full_name") or raw.get("name"))
        if not full_name:
            raise ValueError(f"candidate {position} has no full name")
        candidate = session.scalar(
            select(Candidate).where(
                Candidate.ballot_id == ballot.id, Candidate.position == position
            )
        )
        source_record_id = clean(raw.get("source_record_id") or raw.get("source_id"))
        if source_record_id is None:
            source_record_id = f"{oik_code}:{position}"
        affiliation = clean(raw.get("party_affiliation") or raw.get("party"))
        self_nominated = bool(raw.get("is_self_nominated", raw.get("self_nominated", False)))
        if affiliation and "самовыдв" in affiliation.casefold():
            self_nominated = True
        values = {
            "full_name": full_name,
            "party_affiliation": affiliation,
            "is_self_nominated": self_nominated,
            "registration_status": clean(raw.get("registration_status") or raw.get("status")),
            "source_artifact_id": artifact_id,
            "source_record_id": source_record_id,
            "gas_vybory_id": clean(raw.get("gas_vybory_id")),
            "raw_json": raw,
        }
        if candidate is None:
            candidate = Candidate(ballot=ballot, position=position, **values)
            session.add(candidate)
            session.flush()
        else:
            for key, value in values.items():
                setattr(candidate, key, value)
        candidates[position] = candidate
    if not candidates:
        raise ValueError(f"district {oik_code} has no candidates")
    stale_candidates = list(
        session.scalars(
            select(Candidate).where(
                Candidate.ballot_id == ballot.id,
                Candidate.position.not_in(candidates),
            )
        )
    )
    for candidate in stale_candidates:
        referenced = session.scalar(
            select(func.count(Vote.id)).where(Vote.candidate_id == candidate.id)
        )
        if not referenced:
            session.delete(candidate)
    return candidates


def _special_type(raw: dict[str, Any]) -> SpecialType:
    explicit = clean(raw.get("special_type"))
    if explicit:
        try:
            return SpecialType(explicit.casefold())
        except ValueError as exc:
            raise ValueError(f"unknown special_type {explicit!r}") from exc
    haystack = " ".join(
        clean(raw.get(key)) or "" for key in ("tik_name", "tik", "uik_number", "uik")
    ).casefold()
    if "дистанцион" in haystack or "дэг" in haystack:
        return SpecialType.DEG
    if "зарубеж" in haystack or "иностран" in haystack:
        return SpecialType.FOREIGN
    if "временн" in haystack:
        return SpecialType.TEMPORARY
    has_uik = extract_number(raw.get("uik_number") or raw.get("uik")) is not None
    return SpecialType.NONE if has_uik else SpecialType.OTHER


def _accounting(raw: dict[str, Any]) -> dict[str, int]:
    values = raw.get("accounting", raw)
    if not isinstance(values, dict):
        raise ValueError("protocol accounting must be an object")
    return {field: parse_int(values.get(field), field) for field in ACCOUNTING_FIELDS}


def _vote_values(raw: dict[str, Any]) -> dict[int, int]:
    values = raw.get("votes")
    if isinstance(values, dict):
        return {
            parse_int(key, "candidate position"): parse_int(value, "candidate votes")
            for key, value in values.items()
        }
    if isinstance(values, list):
        result: dict[int, int] = {}
        for item in values:
            if not isinstance(item, dict):
                raise ValueError("protocol votes must contain objects")
            position = _candidate_position(item)
            if position in result:
                raise ValueError(f"duplicate vote value for candidate position {position}")
            result[position] = parse_int(item.get("votes"), "candidate votes")
        return result
    raise ValueError("protocol votes must be an object or list")


def import_single_member_results(
    session: Session,
    artifact: Artifact,
    path: Path,
    *,
    districts: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Import the canonical preserved single-member JSON snapshot.

    The snapshot has a ``districts`` array. Each district carries its OIK identity,
    candidate list, and UIK ``protocols``. Raw district, candidate, and protocol
    objects are retained verbatim for evidence and rejected protocols are recorded.
    """

    districts = _load_snapshot(path) if districts is None else districts
    source = register_artifact(session, artifact, path)
    run = start_run(session, "single_member_results", source.id)
    election = _ensure_election(session)

    session.execute(delete(ImportReject).where(ImportReject.artifact_id == source.id))
    session.execute(delete(ResultRecord).where(ResultRecord.source_artifact_id == source.id))
    session.flush()
    # Bulk/database cascades can leave loaded rows in SQLAlchemy's identity map.
    # Evict only the source-derived objects so reused SQLite primary keys do not
    # collide when a verified artifact is rebuilt in the same session.
    for loaded in list(session.identity_map.values()):
        if isinstance(loaded, (ResultRecord, BallotAccounting, Vote, ImportReject)):
            session.expunge(loaded)

    imported = rejected = vote_rows = source_rows = 0
    ballot_ids: set[int] = set()
    candidate_ids: set[int] = set()
    tik_keys: set[tuple[str, str]] = set()
    uik_keys: set[tuple[str, str, str]] = set()
    row_number = 0
    for district in districts:
        ballot, region, oik_code, oik_name = _ensure_ballot(session, election, district)
        ballot_ids.add(ballot.id)
        candidates = _ensure_candidates(
            session,
            source.id,
            ballot,
            oik_code,
            _as_list(district.get("candidates"), "district candidates"),
        )
        candidate_ids.update(candidate.id for candidate in candidates.values())
        for protocol in _as_list(district.get("protocols"), "district protocols"):
            row_number += 1
            source_rows += 1
            try:
                accounting = _accounting(protocol)
                votes = _vote_values(protocol)
                missing = sorted(set(votes) - set(candidates))
                if missing:
                    raise ValueError(f"votes reference unknown candidate positions: {missing}")
                absent = sorted(set(candidates) - set(votes))
                if absent:
                    raise ValueError(f"protocol omits candidate positions: {absent}")
                tik_name = clean(protocol.get("tik_name") or protocol.get("tik"))
                uik_number = extract_number(protocol.get("uik_number") or protocol.get("uik"))
                special = _special_type(protocol)
                result = ResultRecord(
                    source_artifact_id=source.id,
                    source_row_number=row_number,
                    ballot=ballot,
                    region_name=clean(protocol.get("region_name")) or region,
                    oik_name=clean(protocol.get("oik_name")) or oik_name,
                    tik_name=tik_name,
                    uik_number=uik_number,
                    gas_vybory_id=clean(protocol.get("gas_vybory_id")),
                    source_url=clean(protocol.get("source_url") or district.get("source_url")),
                    special_type=special,
                    is_deg=special == SpecialType.DEG,
                    raw_json=protocol,
                    match_status=(
                        MatchStatus.SPECIAL if special != SpecialType.NONE else MatchStatus.PENDING
                    ),
                    validation_status=ValidationStatus.NOT_VALIDATED,
                )
                result.accounting = BallotAccounting(**accounting)
                result.votes = [
                    Vote(candidate=candidates[position], votes=value)
                    for position, value in sorted(votes.items())
                ]
                session.add(result)
                imported += 1
                vote_rows += len(votes)
                if tik_name:
                    tik_keys.add((oik_code, tik_name))
                if uik_number:
                    uik_keys.add((oik_code, tik_name or "", uik_number))
            except (TypeError, ValueError) as exc:
                rejected += 1
                session.add(
                    ImportReject(
                        run_id=run.id,
                        artifact_id=source.id,
                        source_row_number=row_number,
                        source_record_id=clean(
                            protocol.get("source_record_id") or protocol.get("uik")
                        ),
                        error=str(exc),
                        raw_json=protocol,
                    )
                )
    stats = {
        "source_rows": source_rows,
        "imported_records": imported,
        "rejected_records": rejected,
        "districts": len(ballot_ids),
        "tiks": len(tik_keys),
        "uiks": len(uik_keys),
        "candidates": len(candidate_ids),
        "vote_rows": vote_rows,
    }
    finish_run(run, stats)
    session.flush()
    return stats


def _payload_path(raw_dir: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise SourceError("preserved payload has no path")
    root = raw_dir.resolve()
    target = (root / value).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SourceError(f"payload path escapes raw directory: {value}") from exc
    return target


def _decode_html(path: Path) -> str:
    payload = path.read_bytes()
    for encoding in ("utf-8", "windows-1251"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError(f"cannot decode HTML payload {path} as UTF-8 or Windows-1251")


def _region_hints(document: dict[str, Any]) -> dict[str, str]:
    hints: dict[str, str] = {}
    collections: list[Any] = [
        document.get("oiks"),
        document.get("expected_oiks"),
        document.get("catalog"),
    ]
    coverage = document.get("coverage")
    if isinstance(coverage, dict):
        collections.append(coverage.get("by_oik"))
    for collection in collections:
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, dict):
                continue
            number = item.get("oik_number", item.get("number"))
            region = clean(item.get("region_name") or item.get("region"))
            if number is not None and region:
                hints[str(number)] = region
    return hints


def _payload_sort_key(payload: dict[str, Any]) -> tuple[int, str, str, str]:
    number = payload.get("oik_number")
    return (
        int(number) if isinstance(number, int) else 0,
        clean(payload.get("tik_name")) or "",
        clean(payload.get("source_url") or payload.get("source")) or "",
        clean(payload.get("key")) or "",
    )


def _artifact_from_payload(
    payload: dict[str, Any], path: Path, snapshot_key: str | None
) -> Artifact:
    key = clean(payload.get("key"))
    source_url = clean(payload.get("source_url") or payload.get("source"))
    final_url = clean(payload.get("final_url"))
    checksum = clean(payload.get("sha256") or payload.get("checksum"))
    if not key:
        raise SourceError("preserved payload has no key")
    if not source_url:
        raise SourceError(f"preserved payload {key} has no source URL")
    if not final_url:
        raise SourceError(f"preserved payload {key} has no final URL")
    if not checksum or not re.fullmatch(r"[0-9a-fA-F]{64}", checksum):
        raise SourceError(f"preserved payload {key} has no valid SHA-256 checksum")
    try:
        size_bytes = int(payload["size_bytes"])
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceError(f"preserved payload {key} has no valid byte size") from exc
    return Artifact(
        key=key,
        url=final_url,
        filename=path.name,
        sha256=checksum.casefold(),
        size_bytes=size_bytes,
        media_type=str(payload.get("media_type") or "text/html"),
        metadata={
            "snapshot_key": snapshot_key,
            **{
                field: value
                for field, value in payload.items()
                if field not in {"key", "sha256", "size_bytes", "media_type"}
            },
        },
    )


def _clear_source_rows(session: Session, source_ids: set[int]) -> None:
    if not source_ids:
        return
    session.execute(delete(ImportReject).where(ImportReject.artifact_id.in_(source_ids)))
    session.execute(delete(ResultRecord).where(ResultRecord.source_artifact_id.in_(source_ids)))
    session.flush()
    for loaded in list(session.identity_map.values()):
        if isinstance(loaded, (ResultRecord, BallotAccounting, Vote, ImportReject)):
            session.expunge(loaded)


def _payload_reject(
    session: Session,
    source: SourceArtifact,
    payload: dict[str, Any],
    error: str,
) -> None:
    run = start_run(session, "single_member_payload", source.id)
    session.add(
        ImportReject(
            run_id=run.id,
            artifact_id=source.id,
            source_row_number=1,
            source_record_id=clean(payload.get("key")),
            error=error,
            raw_json=payload,
        )
    )
    finish_run(
        run,
        {
            "source_rows": 0,
            "imported_records": 0,
            "rejected_records": 1,
            "vote_rows": 0,
        },
    )


def _candidate_signature(district: dict[str, Any]) -> tuple[tuple[int, str], ...]:
    rows = _as_list(district.get("candidates"), "district candidates")
    return tuple(
        sorted(
            (
                _candidate_position(row),
                clean(row.get("full_name") or row.get("name")) or "",
            )
            for row in rows
        )
    )


def _protocol_keys(district: dict[str, Any]) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for protocol in _as_list(district.get("protocols"), "district protocols"):
        tik = clean(protocol.get("tik_name") or protocol.get("tik")) or ""
        uik = clean(protocol.get("uik_number") or protocol.get("uik")) or ""
        key = (tik, uik)
        if key in keys:
            raise ValueError(f"payload contains duplicate protocol {tik or '-'}#{uik or '-'}")
        keys.add(key)
    return keys


def import_single_member_snapshot(
    session: Session, manifest_path: Path, raw_dir: Path
) -> dict[str, Any]:
    """Verify and import every preserved HTML payload in acquisition manifest v1."""

    document = load_snapshot_manifest(manifest_path)
    regions = _region_hints(document)
    snapshot_key = clean(document.get("snapshot_key"))
    raw_payloads = document["payloads"]
    if not all(isinstance(payload, dict) for payload in raw_payloads):
        raise SourceError("single-member acquisition manifest payloads must be objects")
    payloads = sorted(raw_payloads, key=_payload_sort_key)
    keys = [clean(payload.get("key")) for payload in payloads]
    nonempty_keys = [key for key in keys if key]
    if len(set(nonempty_keys)) != len(nonempty_keys):
        raise SourceError("single-member acquisition manifest contains duplicate payload keys")

    totals = {
        "payloads": len(payloads),
        "preserved_payloads": 0,
        "redirected_payloads": 0,
        "skipped_payloads": 0,
        "source_rows": 0,
        "imported_records": 0,
        "rejected_records": 0,
        "vote_rows": 0,
    }
    parse_errors: list[dict[str, Any]] = []
    source_gaps = [dict(item) for item in document.get("gaps", []) if isinstance(item, dict)]
    prepared: list[tuple[dict[str, Any], Artifact, Path, SourceArtifact, dict[str, Any]]] = []
    registered_sources: dict[str, SourceArtifact] = {}

    for payload in payloads:
        status = clean(payload.get("status"))
        if status not in PRESERVED_STATUSES:
            totals["skipped_payloads"] += 1
            source_gaps.append(
                {
                    "key": clean(payload.get("key")),
                    "oik_number": payload.get("oik_number"),
                    "status": status or "inconsistent",
                    "detail": "payload is not in a preserved status",
                    "source_url": clean(payload.get("source_url") or payload.get("source")),
                }
            )
            continue
        totals["preserved_payloads"] += 1
        totals["redirected_payloads"] += int(status == "redirected")
        key = clean(payload.get("key"))
        oik_number = payload.get("oik_number")
        region = clean(payload.get("region_name") or payload.get("region"))
        if not region and oik_number is not None:
            region = regions.get(str(oik_number))
        try:
            path = _payload_path(raw_dir, payload.get("path"))
            artifact = _artifact_from_payload(payload, path, snapshot_key)
            verify_artifact(artifact, path)
            source = register_artifact(session, artifact, path)
            registered_sources[artifact.key] = source
            if oik_number is None:
                totals["skipped_payloads"] += 1
                continue
            if not region:
                raise ValueError("payload lacks region_name and has no manifest region hint")
            district = parse_single_member_html(
                _decode_html(path),
                region_name=region,
                oik_code=str(oik_number),
                oik_name=clean(payload.get("oik_name")),
                tik_name=clean(payload.get("tik_name")),
                source_url=artifact.url,
            )
            prepared.append((payload, artifact, path, source, district))
        except (KeyError, OSError, SourceError, TypeError, ValueError) as exc:
            parse_errors.append(
                {
                    "key": key,
                    "oik_number": oik_number,
                    "tik_name": clean(payload.get("tik_name")),
                    "source_url": clean(
                        payload.get("final_url")
                        or payload.get("source_url")
                        or payload.get("source")
                    ),
                    "error": str(exc),
                }
            )

    # The current manifest is authoritative for its snapshot. Clear both current
    # payload keys and artifacts retained from an earlier version of this snapshot,
    # so a now-malformed or removed page cannot leave stale protocols behind.
    snapshot_source_ids = {source.id for source in registered_sources.values()}
    for source in session.scalars(select(SourceArtifact)):
        metadata = source.metadata_json or {}
        if source.key in nonempty_keys or (
            snapshot_key and metadata.get("snapshot_key") == snapshot_key
        ):
            snapshot_source_ids.add(source.id)
    _clear_source_rows(session, snapshot_source_ids)
    affected_oik_codes = {
        str(payload["oik_number"])
        for payload in payloads
        if payload.get("oik_number") is not None
    }
    affected_ballots = list(
        session.scalars(
            select(Ballot).where(
                Ballot.kind == BallotKind.SINGLE_MEMBER,
                Ballot.scope_key.in_(affected_oik_codes),
            )
        )
    )
    for ballot in affected_ballots:
        for candidate in list(ballot.candidates):
            referenced = session.scalar(
                select(func.count(Vote.id)).where(Vote.candidate_id == candidate.id)
            )
            if not referenced and candidate.source_artifact_id in snapshot_source_ids:
                session.delete(candidate)
    session.flush()

    # Repeated TIK pages must agree on the district roster, and a protocol identity
    # may only be imported once across the manifest.
    signatures: dict[str, tuple[tuple[int, str], ...]] = {}
    seen_protocols: dict[str, set[tuple[str, str]]] = {}
    accepted: list[tuple[dict[str, Any], Artifact, Path, SourceArtifact, dict[str, Any]]] = []
    for item in prepared:
        payload, _, _, _, district = item
        oik_code = str(district["oik_code"])
        try:
            signature = _candidate_signature(district)
            prior_signature = signatures.setdefault(oik_code, signature)
            if signature != prior_signature:
                raise ValueError("candidate roster conflicts with another payload for this OIK")
            protocol_keys = _protocol_keys(district)
            duplicate_keys = seen_protocols.setdefault(oik_code, set()) & protocol_keys
            if duplicate_keys:
                formatted = sorted(f"{tik or '-'}#{uik or '-'}" for tik, uik in duplicate_keys)
                raise ValueError(f"protocol identities duplicate another payload: {formatted}")
            seen_protocols[oik_code].update(protocol_keys)
            accepted.append(item)
        except (TypeError, ValueError) as exc:
            parse_errors.append(
                {
                    "key": clean(payload.get("key")),
                    "oik_number": payload.get("oik_number"),
                    "tik_name": clean(payload.get("tik_name")),
                    "source_url": clean(payload.get("final_url") or payload.get("source_url")),
                    "error": str(exc),
                }
            )

    rejected_keys = {item["key"] for item in parse_errors if item.get("key")}
    for payload in payloads:
        key = clean(payload.get("key"))
        source = registered_sources.get(key or "")
        error = next((item["error"] for item in parse_errors if item.get("key") == key), None)
        if source is not None and error is not None:
            _payload_reject(session, source, payload, error)
            totals["rejected_records"] += 1

    imported_source_ids: set[int] = set()
    imported_ballot_ids: set[int] = set()
    for payload, artifact, path, source, district in accepted:
        if artifact.key in rejected_keys:
            continue
        stats = import_single_member_results(session, artifact, path, districts=[district])
        imported_source_ids.add(source.id)
        ballot = session.scalar(
            select(Ballot).where(
                Ballot.kind == BallotKind.SINGLE_MEMBER,
                Ballot.scope_key == str(district["oik_code"]),
            )
        )
        if ballot is not None:
            imported_ballot_ids.add(ballot.id)
        for field in ("source_rows", "imported_records", "rejected_records", "vote_rows"):
            totals[field] += stats[field]

    imported_results = list(
        session.scalars(
            select(ResultRecord).where(ResultRecord.source_artifact_id.in_(imported_source_ids))
        )
    ) if imported_source_ids else []
    tik_keys = {
        (record.ballot_id, record.tik_name)
        for record in imported_results
        if record.tik_name
    }
    uik_keys = {
        (record.ballot_id, record.tik_name or "", record.uik_number)
        for record in imported_results
        if record.uik_number
    }
    totals.update(
        {
            "districts": len(imported_ballot_ids),
            "tiks": len(tik_keys),
            "uiks": len(uik_keys),
            "candidates": int(
                session.scalar(
                    select(func.count(Candidate.id)).where(
                        Candidate.ballot_id.in_(imported_ballot_ids)
                    )
                )
                or 0
            )
            if imported_ballot_ids
            else 0,
            "parse_errors": parse_errors,
            "source_gaps": source_gaps,
        }
    )
    session.flush()
    return totals
