from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from elections.acquisition import BLOCKING_GAP_STATUSES, load_snapshot_manifest, verify_snapshot
from elections.models import (
    Ballot,
    BallotAccounting,
    BallotKind,
    BallotOption,
    Candidate,
    ImportReject,
    MatchStatus,
    PublishedTotal,
    ResultRecord,
    Severity,
    SourceArtifact,
    ValidationFinding,
    ValidationStatus,
    Vote,
)
from elections.reconciliation import ACCOUNTING_METRICS, VOTE_METRICS, WINNER_METRICS
from elections.sources import SourceError

EXPECTED_BALLOTS = {BallotKind.PARTY_LIST: 1, BallotKind.SINGLE_MEMBER: 225}
UNRESOLVED_MATCH_STATUSES = {
    MatchStatus.PENDING,
    MatchStatus.RESULT_ONLY,
    MatchStatus.AMBIGUOUS,
    MatchStatus.DATA_INTEGRITY_ERROR,
}


def _count(session: Session, statement: Any) -> int:
    return int(session.scalar(statement) or 0)


def _artifact_kinds(session: Session) -> dict[int, set[BallotKind]]:
    kinds: dict[int, set[BallotKind]] = defaultdict(set)
    for artifact_id, kind in session.execute(
        select(ResultRecord.source_artifact_id, Ballot.kind)
        .join(Ballot, Ballot.id == ResultRecord.ballot_id)
        .distinct()
    ):
        kinds[int(artifact_id)].add(kind)
    for artifact_id, kind in session.execute(
        select(Candidate.source_artifact_id, Ballot.kind)
        .join(Ballot, Ballot.id == Candidate.ballot_id)
        .where(Candidate.source_artifact_id.is_not(None))
        .distinct()
    ):
        kinds[int(artifact_id)].add(kind)
    for artifact in session.scalars(select(SourceArtifact)):
        if (artifact.metadata_json or {}).get("snapshot_key"):
            kinds[artifact.id].add(BallotKind.SINGLE_MEMBER)
    return kinds


def _validation_error_counts(
    session: Session, single_member_scopes: set[str]
) -> tuple[dict[BallotKind, int], int, int]:
    result_kinds = dict(
        session.execute(
            select(ResultRecord.id, Ballot.kind).join(Ballot, Ballot.id == ResultRecord.ballot_id)
        ).all()
    )
    by_kind = dict.fromkeys(BallotKind, 0)
    unscoped = 0
    total = 0
    for finding in session.scalars(
        select(ValidationFinding).where(ValidationFinding.severity == Severity.ERROR)
    ):
        total += 1
        kind = result_kinds.get(finding.result_record_id)
        details = finding.details_json or {}
        district = str(details.get("district") or "")
        if kind is None and (
            district in single_member_scopes
            or finding.code.startswith("single_member_")
            or finding.code
            in {
                "declared_winner_mismatch",
                "district_source_gap",
                "missing_candidate_roster",
                "missing_candidate_votes",
                "missing_official_candidate_total",
                "missing_official_candidate_totals",
                "missing_or_ambiguous_official_winner",
                "official_accounting_total_mismatch",
                "official_candidate_total_mismatch",
                "official_uik_count_mismatch",
                "unknown_official_candidate",
            }
        ):
            kind = BallotKind.SINGLE_MEMBER
        elif kind is None and finding.code == "published_total_mismatch":
            kind = BallotKind.PARTY_LIST
        if kind is None:
            unscoped += 1
        else:
            by_kind[kind] += 1
    return by_kind, unscoped, total


def _reference_status(
    session: Session, ballot: Ballot, candidates: list[Candidate]
) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(PublishedTotal)
            .where(PublishedTotal.ballot_id == ballot.id)
            .order_by(PublishedTotal.id)
        )
    )
    missing_source_urls = sum(not row.source_url for row in rows)
    if ballot.kind == BallotKind.PARTY_LIST:
        option_names = set(
            session.scalars(
                select(BallotOption.name).where(BallotOption.ballot_id == ballot.id)
            )
        )
        vote_names = {row.option_name for row in rows if row.metric in VOTE_METRICS}
        accounting = {row.metric for row in rows if row.metric in ACCOUNTING_METRICS}
        missing_options = sorted(option_names - vote_names)
        missing_accounting = sorted(set(ACCOUNTING_METRICS) - accounting)
        ready = bool(
            option_names
            and not missing_options
            and not missing_accounting
            and not missing_source_urls
        )
        return {
            "ready": ready,
            "rows": len(rows),
            "missing_source_urls": missing_source_urls,
            "missing_option_totals": missing_options,
            "missing_accounting_totals": missing_accounting,
        }

    candidate_names = {candidate.full_name for candidate in candidates}
    vote_names = {row.option_name for row in rows if row.metric in VOTE_METRICS}
    winners = [row for row in rows if row.metric in WINNER_METRICS and row.value == 1]
    missing_candidates = sorted(candidate_names - vote_names)
    ready = bool(
        candidate_names
        and not missing_candidates
        and len(winners) == 1
        and winners[0].option_name in candidate_names
        and not missing_source_urls
    )
    return {
        "ready": ready,
        "rows": len(rows),
        "missing_source_urls": missing_source_urls,
        "missing_candidate_totals": missing_candidates,
        "winner_rows": len(winners),
    }


def _ballot_report(
    session: Session,
    kind: BallotKind,
    *,
    validation_errors: int,
    import_rejects: int,
    artifact_ids: set[int],
) -> dict[str, Any]:
    ballots = list(
        session.scalars(select(Ballot).where(Ballot.kind == kind).order_by(Ballot.scope_key))
    )
    ballot_ids = [ballot.id for ballot in ballots]
    expected = EXPECTED_BALLOTS[kind]
    if not ballot_ids:
        return {
            "ready": False,
            "expected_ballots": expected,
            "ballots": 0,
            "districts": 0,
            "candidates": 0,
            "result_records": 0,
            "vote_rows": 0,
            "published_totals": 0,
            "official_reference_ballots": 0,
            "missing_accounting": 0,
            "not_validated": 0,
            "invalid": 0,
            "warnings": 0,
            "unresolved_records": 0,
            "validation_errors": validation_errors,
            "import_rejects": import_rejects,
            "source_artifact_ids": sorted(artifact_ids),
        }

    result_ids = select(ResultRecord.id).where(ResultRecord.ballot_id.in_(ballot_ids))
    result_records = _count(session, select(func.count()).select_from(result_ids.subquery()))
    candidates = _count(
        session, select(func.count(Candidate.id)).where(Candidate.ballot_id.in_(ballot_ids))
    )
    vote_rows = _count(
        session,
        select(func.count(Vote.id)).where(Vote.result_record_id.in_(result_ids.scalar_subquery())),
    )
    published = _count(
        session,
        select(func.count(PublishedTotal.id)).where(PublishedTotal.ballot_id.in_(ballot_ids)),
    )
    status_counts = {
        status: count
        for status, count in session.execute(
            select(ResultRecord.validation_status, func.count(ResultRecord.id))
            .where(ResultRecord.ballot_id.in_(ballot_ids))
            .group_by(ResultRecord.validation_status)
        )
    }
    unresolved = _count(
        session,
        select(func.count(ResultRecord.id)).where(
            ResultRecord.ballot_id.in_(ballot_ids),
            ResultRecord.match_status.in_(UNRESOLVED_MATCH_STATUSES),
        ),
    )
    missing_accounting = _count(
        session,
        select(func.count(ResultRecord.id))
        .outerjoin(
            BallotAccounting, BallotAccounting.result_record_id == ResultRecord.id
        )
        .where(
            ResultRecord.ballot_id.in_(ballot_ids),
            BallotAccounting.result_record_id.is_(None),
        ),
    )

    ballot_readiness: list[dict[str, Any]] = []
    for ballot in ballots:
        ballot_candidates = list(
            session.scalars(select(Candidate).where(Candidate.ballot_id == ballot.id))
        )
        records = list(
            session.execute(
                select(ResultRecord.validation_status, ResultRecord.match_status).where(
                    ResultRecord.ballot_id == ballot.id
                )
            )
        )
        reference = _reference_status(session, ballot, ballot_candidates)
        ballot_missing_accounting = _count(
            session,
            select(func.count(ResultRecord.id))
            .outerjoin(
                BallotAccounting,
                BallotAccounting.result_record_id == ResultRecord.id,
            )
            .where(
                ResultRecord.ballot_id == ballot.id,
                BallotAccounting.result_record_id.is_(None),
            ),
        )
        not_validated = sum(
            row.validation_status == ValidationStatus.NOT_VALIDATED for row in records
        )
        invalid = sum(row.validation_status == ValidationStatus.INVALID for row in records)
        unresolved_records = sum(row.match_status in UNRESOLVED_MATCH_STATUSES for row in records)
        ready = bool(
            records
            and (kind == BallotKind.PARTY_LIST or ballot_candidates)
            and reference["ready"]
            and not not_validated
            and not invalid
            and not unresolved_records
            and not ballot_missing_accounting
        )
        ballot_readiness.append(
            {
                "scope_key": ballot.scope_key,
                "ready": ready,
                "result_records": len(records),
                "candidates": len(ballot_candidates),
                "not_validated": not_validated,
                "invalid": invalid,
                "unresolved_records": unresolved_records,
                "missing_accounting": ballot_missing_accounting,
                "official_references": reference,
            }
        )

    ready_ballots = sum(item["ready"] for item in ballot_readiness)
    report: dict[str, Any] = {
        "ready": bool(
            len(ballots) == expected
            and ready_ballots == expected
            and validation_errors == 0
            and import_rejects == 0
        ),
        "expected_ballots": expected,
        "ballots": len(ballots),
        "districts": len(ballots) if kind == BallotKind.SINGLE_MEMBER else 0,
        "ready_ballots": ready_ballots,
        "candidates": candidates,
        "result_records": result_records,
        "vote_rows": vote_rows,
        "published_totals": published,
        "official_reference_ballots": sum(
            item["official_references"]["ready"] for item in ballot_readiness
        ),
        "missing_accounting": missing_accounting,
        "not_validated": int(status_counts.get(ValidationStatus.NOT_VALIDATED, 0)),
        "invalid": int(status_counts.get(ValidationStatus.INVALID, 0)),
        "warnings": int(status_counts.get(ValidationStatus.WARNING, 0)),
        "unresolved_records": unresolved,
        "validation_errors": validation_errors,
        "import_rejects": import_rejects,
        "source_artifact_ids": sorted(artifact_ids),
    }
    if kind == BallotKind.SINGLE_MEMBER:
        expected_scopes = {str(number) for number in range(1, expected + 1)}
        actual_scopes = {ballot.scope_key for ballot in ballots}
        report["oik_readiness"] = {
            "expected": expected,
            "found": len(ballots),
            "ready": ready_ballots,
            "missing_scope_keys": sorted(expected_scopes - actual_scopes, key=int),
            "unexpected_scope_keys": sorted(actual_scopes - expected_scopes),
            "not_ready_scope_keys": [
                item["scope_key"] for item in ballot_readiness if not item["ready"]
            ],
        }
    return report


def _snapshot_report(manifest_path: Path | None, raw_dir: Path | None) -> dict[str, Any]:
    if manifest_path is None:
        return {"required": False, "ready": True, "present": False}
    report: dict[str, Any] = {
        "required": True,
        "ready": False,
        "present": manifest_path.is_file(),
        "manifest": str(manifest_path),
    }
    if not manifest_path.is_file():
        report["error"] = "single-member snapshot manifest is missing"
        return report
    try:
        document = load_snapshot_manifest(manifest_path)
        gap_counts = Counter(gap["status"] for gap in document["gaps"])
        blockers = sum(gap_counts[status] for status in BLOCKING_GAP_STATUSES)
        report.update(
            {
                "snapshot_key": document.get("snapshot_key"),
                "generated_at": document.get("generated_at"),
                "coverage": document["coverage"],
                "gaps": len(document["gaps"]),
                "blocking_gaps": blockers,
                "gap_status_counts": dict(sorted(gap_counts.items())),
            }
        )
        if raw_dir is None:
            report["error"] = "raw directory was not supplied for checksum verification"
            return report
        verified = verify_snapshot(manifest_path, raw_dir, require_complete=False)
        report["verified_payloads"] = verified["verified_payloads"]
        report["ready"] = bool(
            document["coverage"]["expected_oiks"] == 225
            and document["coverage"]["covered_oiks"] == 225
            and document["coverage"]["missing_oiks"] == 0
            and blockers == 0
        )
    except (KeyError, OSError, SourceError, TypeError, ValueError) as exc:
        report["error"] = str(exc)
    return report


def verify_complete_dataset(
    session: Session,
    report_path: Path | None = None,
    *,
    snapshot_manifest_path: Path | None = None,
    raw_dir: Path | None = None,
) -> dict[str, Any]:
    """Create the machine-readable two-ballot completion and provenance report."""

    artifact_kinds = _artifact_kinds(session)
    single_member_scopes = set(
        session.scalars(select(Ballot.scope_key).where(Ballot.kind == BallotKind.SINGLE_MEMBER))
    )
    errors_by_kind, unscoped_errors, validation_errors = _validation_error_counts(
        session, single_member_scopes
    )
    rejects_by_artifact = dict(
        session.execute(
            select(ImportReject.artifact_id, func.count(ImportReject.id)).group_by(
                ImportReject.artifact_id
            )
        ).all()
    )
    import_rejects = sum(int(value) for value in rejects_by_artifact.values())
    rejects_by_kind = dict.fromkeys(BallotKind, 0)
    unassigned_rejects = 0
    for artifact_id, count in rejects_by_artifact.items():
        kinds = artifact_kinds.get(int(artifact_id), set())
        if not kinds:
            unassigned_rejects += int(count)
        for kind in kinds:
            rejects_by_kind[kind] += int(count)

    artifacts = list(session.scalars(select(SourceArtifact).order_by(SourceArtifact.key)))
    artifact_rows = [
        {
            "id": item.id,
            "key": item.key,
            "url": item.url,
            "sha256": item.sha256,
            "size_bytes": item.size_bytes,
            "media_type": item.media_type,
            "retrieved_at": item.retrieved_at,
            "snapshot_key": (item.metadata_json or {}).get("snapshot_key"),
        }
        for item in artifacts
    ]
    source_versions = {
        kind.value: [
            row
            for row in artifact_rows
            if kind in artifact_kinds.get(int(row["id"]), set())
        ]
        for kind in BallotKind
    }
    source_versions["unassigned"] = [
        row for row in artifact_rows if not artifact_kinds.get(int(row["id"]), set())
    ]

    ballot_kinds = {
        kind.value: _ballot_report(
            session,
            kind,
            validation_errors=errors_by_kind[kind],
            import_rejects=rejects_by_kind[kind],
            artifact_ids={
                artifact_id for artifact_id, kinds in artifact_kinds.items() if kind in kinds
            },
        )
        for kind in BallotKind
    }
    snapshot = _snapshot_report(snapshot_manifest_path, raw_dir)
    unresolved_records = sum(
        item["unresolved_records"] for item in ballot_kinds.values()
    )
    ready = bool(
        all(item["ready"] for item in ballot_kinds.values())
        and snapshot["ready"]
        and validation_errors == 0
        and import_rejects == 0
        and unresolved_records == 0
    )
    report: dict[str, Any] = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "ready": ready,
        "ballot_kinds": ballot_kinds,
        "source_snapshot": snapshot,
        "source_versions": source_versions,
        "unresolved": {
            "validation_errors": validation_errors,
            "unscoped_validation_errors": unscoped_errors,
            "import_rejects": import_rejects,
            "unassigned_import_rejects": unassigned_rejects,
            "match_records": unresolved_records,
            "source_gaps": int(snapshot.get("gaps", 0)),
            "blocking_source_gaps": int(snapshot.get("blocking_gaps", 0)),
        },
        "summary": {
            "ballot_kinds_ready": sum(item["ready"] for item in ballot_kinds.values()),
            "ballot_kinds_expected": 2,
            "single_member_oiks_found": ballot_kinds[BallotKind.SINGLE_MEMBER.value][
                "ballots"
            ],
            "single_member_oiks_ready": ballot_kinds[BallotKind.SINGLE_MEMBER.value].get(
                "ready_ballots", 0
            ),
            "validation_errors": validation_errors,
            "import_rejects": import_rejects,
            "unresolved_records": unresolved_records,
            "source_artifacts": len(artifacts),
            "source_gaps": int(snapshot.get("gaps", 0)),
        },
        "source_artifacts": artifact_rows,
    }
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        partial = report_path.with_name(f".{report_path.name}.part")
        partial.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        os.replace(partial, report_path)
    return report
