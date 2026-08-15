from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from elections.models import (
    Ballot,
    BallotAccounting,
    BallotKind,
    BallotOption,
    Candidate,
    MatchStatus,
    PublishedTotal,
    ResultRecord,
    Severity,
    ValidationFinding,
    ValidationStatus,
    Vote,
)
from elections.reconciliation import (
    EXPECTED_SINGLE_MEMBER_DISTRICTS,
    PROTOCOL_COUNT_METRICS,
    VOTE_METRICS,
    WINNER_METRICS,
    reconcile_single_member,
)


def load_published_totals(session: Session, path: Path, ballot_id: int) -> int:
    document = json.loads(path.read_text(encoding="utf-8"))
    totals = document.get("totals", document)
    if not isinstance(totals, list):
        raise ValueError("published totals file must contain a totals list")
    session.execute(delete(PublishedTotal).where(PublishedTotal.ballot_id == ballot_id))
    for item in totals:
        session.add(
            PublishedTotal(
                ballot_id=ballot_id,
                level=item["level"],
                scope_identifier=item["scope_identifier"],
                metric=item["metric"],
                option_name=item.get("option_name", ""),
                value=int(item["value"]),
                source_url=item.get("source_url"),
            )
        )
    session.flush()
    return len(totals)


def load_single_member_published_totals(session: Session, path: Path) -> int:
    """Load official OIK/candidate references, resolving each row to its district ballot."""

    document = json.loads(path.read_text(encoding="utf-8"))
    totals = document.get("totals", document)
    if not isinstance(totals, list):
        raise ValueError("published totals file must contain a totals list")
    ballots = {
        ballot.scope_key: ballot
        for ballot in session.scalars(
            select(Ballot).where(Ballot.kind == BallotKind.SINGLE_MEMBER)
        )
    }
    normalized: list[tuple[Ballot, dict[str, Any]]] = []
    for item in totals:
        if not isinstance(item, dict):
            raise ValueError("published total rows must be objects")
        scope_key = str(
            item.get("oik_code")
            or item.get("ballot_scope_key")
            or item.get("district")
            or ""
        )
        ballot = ballots.get(scope_key)
        if ballot is None:
            raise ValueError(f"published total references unknown OIK ballot {scope_key!r}")
        normalized.append((ballot, item))
    ballot_ids = [ballot.id for ballot in ballots.values()]
    if ballot_ids:
        session.execute(delete(PublishedTotal).where(PublishedTotal.ballot_id.in_(ballot_ids)))
    for ballot, item in normalized:
        session.add(
            PublishedTotal(
                ballot_id=ballot.id,
                level=item.get("level", "oik"),
                scope_identifier=item.get("scope_identifier", ballot.scope_key),
                metric=item["metric"],
                option_name=item.get("option_name", ""),
                value=int(item["value"]),
                source_url=item.get("source_url"),
            )
        )
    session.flush()
    return len(normalized)


def _finding(
    session: Session,
    *,
    result_id: int | None,
    scope: str,
    identifier: str,
    code: str,
    severity: Severity,
    expected: int | None = None,
    actual: int | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    finding_details = dict(details or {})
    finding_details.setdefault("district", None)
    finding_details.setdefault("uik", None)
    finding_details.setdefault("source_url", None)
    result = session.get(ResultRecord, result_id) if result_id is not None else None
    if result is not None:
        finding_details.update(
            {
                "district": result.ballot.scope_key
                if result.ballot.kind == BallotKind.SINGLE_MEMBER
                else result.oik_name,
                "region": result.region_name,
                "tik": result.tik_name,
                "uik": result.uik_number,
                "source_artifact_id": result.source_artifact_id,
                "source_row_number": result.source_row_number,
                "source_url": result.source_url,
                "special_type": result.special_type.value,
            }
        )
    session.add(
        ValidationFinding(
            result_record_id=result_id,
            scope=scope,
            scope_identifier=identifier,
            code=code,
            severity=severity,
            expected_value=expected,
            actual_value=actual,
            details_json=finding_details,
        )
    )
    if result is not None:
        if severity == Severity.ERROR:
            result.validation_status = ValidationStatus.INVALID
        elif result.validation_status == ValidationStatus.VALID:
            result.validation_status = ValidationStatus.WARNING


def _actual_published_total(session: Session, published: PublishedTotal) -> int:
    result_filters = []
    if published.level == "region":
        result_filters.append(ResultRecord.region_name == published.scope_identifier)
    elif published.level == "tik":
        region, separator, tik = published.scope_identifier.partition("|")
        if not separator:
            raise ValueError("TIK scope_identifier must be 'region|tik'")
        result_filters.extend([ResultRecord.region_name == region, ResultRecord.tik_name == tik])
    elif published.level in {"district", "oik"}:
        # A single-member ballot is already OIK-scoped. The fallback predicate keeps
        # legacy ballots with several OIK names queryable during migration.
        ballot = session.get(Ballot, published.ballot_id)
        if ballot is None or ballot.kind != BallotKind.SINGLE_MEMBER:
            result_filters.append(ResultRecord.oik_name == published.scope_identifier)
    elif published.level != "national":
        raise ValueError(f"unsupported published total level: {published.level}")

    if published.metric in VOTE_METRICS:
        statement = (
            select(func.coalesce(func.sum(Vote.votes), 0))
            .join(ResultRecord, ResultRecord.id == Vote.result_record_id)
            .where(ResultRecord.ballot_id == published.ballot_id, *result_filters)
        )
        if published.option_name:
            ballot = session.get(Ballot, published.ballot_id)
            if ballot is not None and ballot.kind == BallotKind.SINGLE_MEMBER:
                statement = statement.join(Candidate, Candidate.id == Vote.candidate_id).where(
                    Candidate.full_name == published.option_name
                )
            else:
                statement = statement.join(BallotOption, BallotOption.id == Vote.option_id).where(
                    BallotOption.name == published.option_name
                )
        return int(session.scalar(statement) or 0)

    if published.metric in WINNER_METRICS:
        totals = session.execute(
            select(Candidate.full_name, func.sum(Vote.votes))
            .join(Vote, Vote.candidate_id == Candidate.id)
            .join(ResultRecord, ResultRecord.id == Vote.result_record_id)
            .where(ResultRecord.ballot_id == published.ballot_id, *result_filters)
            .group_by(Candidate.id, Candidate.full_name)
        ).all()
        highest = max((int(total or 0) for _, total in totals), default=0)
        winners = {name for name, total in totals if int(total or 0) == highest}
        return int(published.option_name in winners)

    if published.metric in PROTOCOL_COUNT_METRICS:
        return int(
            session.scalar(
                select(func.count(ResultRecord.id)).where(
                    ResultRecord.ballot_id == published.ballot_id, *result_filters
                )
            )
            or 0
        )

    if published.metric not in BallotAccounting.__table__.columns:
        raise ValueError(f"unsupported published metric: {published.metric}")
    column = BallotAccounting.__table__.columns[published.metric]
    return int(
        session.scalar(
            select(func.coalesce(func.sum(column), 0))
            .join(ResultRecord, ResultRecord.id == BallotAccounting.result_record_id)
            .where(ResultRecord.ballot_id == published.ballot_id, *result_filters)
        )
        or 0
    )


def _aggregate_rows(session: Session) -> list[dict[str, Any]]:
    statement = (
        select(
            ResultRecord.region_name,
            ResultRecord.tik_name,
            ResultRecord.special_type,
            func.count(ResultRecord.id),
            func.sum(BallotAccounting.registered_voters),
            func.sum(BallotAccounting.valid_ballots),
            func.sum(BallotAccounting.invalid_ballots),
        )
        .join(BallotAccounting, BallotAccounting.result_record_id == ResultRecord.id)
        .group_by(ResultRecord.region_name, ResultRecord.tik_name, ResultRecord.special_type)
        .order_by(ResultRecord.region_name, ResultRecord.tik_name, ResultRecord.special_type)
    )
    return [
        {
            "region": region,
            "tik": tik,
            "special_type": special.value,
            "result_records": int(count or 0),
            "registered_voters": int(registered or 0),
            "valid_ballots": int(valid or 0),
            "invalid_ballots": int(invalid or 0),
        }
        for region, tik, special, count, registered, valid, invalid in session.execute(statement)
    ]


def validate_dataset(
    session: Session,
    report_path: Path | None = None,
    *,
    expected_single_member_districts: int = EXPECTED_SINGLE_MEMBER_DISTRICTS,
) -> dict[str, Any]:
    session.execute(delete(ValidationFinding))
    for result in session.scalars(select(ResultRecord)):
        result.validation_status = ValidationStatus.VALID
    session.flush()

    accounting_rows = session.execute(
        select(ResultRecord, BallotAccounting, func.coalesce(func.sum(Vote.votes), 0))
        .join(BallotAccounting, BallotAccounting.result_record_id == ResultRecord.id)
        .outerjoin(Vote, Vote.result_record_id == ResultRecord.id)
        .group_by(ResultRecord.id, BallotAccounting.result_record_id)
    )
    for result, accounting, vote_sum in accounting_rows:
        identifier = (
            f"source={result.source_artifact_id}:row={result.source_row_number}:"
            f"{result.region_name}/{result.tik_name or '-'}#{result.uik_number or '-'}"
        )
        expected_received = (
            accounting.ballots_issued_early
            + accounting.ballots_issued_at_station
            + accounting.ballots_issued_outside
            + accounting.ballots_cancelled
            + accounting.lost_ballots
            - accounting.unaccounted_ballots
        )
        vote_identity_code = (
            "candidate_vote_identity"
            if result.ballot.kind == BallotKind.SINGLE_MEMBER
            else "party_vote_identity"
        )
        identities = (
            ("ballots_received_identity", expected_received, accounting.ballots_received),
            (
                "ballot_box_identity",
                accounting.portable_boxes_ballots + accounting.stationary_boxes_ballots,
                accounting.valid_ballots + accounting.invalid_ballots,
            ),
            (vote_identity_code, accounting.valid_ballots, int(vote_sum)),
        )
        for code, expected, actual in identities:
            if expected != actual:
                _finding(
                    session,
                    result_id=result.id,
                    scope="uik",
                    identifier=identifier,
                    code=code,
                    severity=Severity.ERROR,
                    expected=expected,
                    actual=actual,
                )
        if accounting.valid_ballots + accounting.invalid_ballots > accounting.registered_voters:
            _finding(
                session,
                result_id=result.id,
                scope="uik",
                identifier=identifier,
                code="ballots_exceed_registered",
                severity=Severity.ERROR,
                expected=accounting.registered_voters,
                actual=accounting.valid_ballots + accounting.invalid_ballots,
            )
        for column in BallotAccounting.__table__.columns:
            if column.name == "result_record_id":
                continue
            value = int(getattr(accounting, column.name))
            if value < 0:
                _finding(
                    session,
                    result_id=result.id,
                    scope="uik",
                    identifier=identifier,
                    code="negative_accounting_value",
                    severity=Severity.ERROR,
                    expected=0,
                    actual=value,
                    details={"metric": column.name},
                )
        is_single_member = result.ballot.kind == BallotKind.SINGLE_MEMBER
        if result.uik_number is None and result.special_type.value == "none":
            _finding(
                session,
                result_id=result.id,
                scope="uik",
                identifier=identifier,
                code="missing_uik_identifier",
                severity=Severity.ERROR if is_single_member else Severity.WARNING,
                expected=1,
                actual=0,
            )
        if is_single_member and not result.source_url:
            _finding(
                session,
                result_id=result.id,
                scope="uik",
                identifier=identifier,
                code="missing_protocol_source_link",
                severity=Severity.ERROR,
                expected=1,
                actual=0,
            )
        if result.match_status in {
            MatchStatus.RESULT_ONLY,
            MatchStatus.AMBIGUOUS,
            MatchStatus.PENDING,
            MatchStatus.DATA_INTEGRITY_ERROR,
        }:
            _finding(
                session,
                result_id=result.id,
                scope="uik",
                identifier=identifier,
                code=f"match_{result.match_status.value}",
                severity=Severity.ERROR if is_single_member else Severity.WARNING,
                expected=1,
                actual=0,
            )

    missing_accounting = session.scalars(
        select(ResultRecord)
        .outerjoin(BallotAccounting, BallotAccounting.result_record_id == ResultRecord.id)
        .where(BallotAccounting.result_record_id.is_(None))
    )
    for result in missing_accounting:
        _finding(
            session,
            result_id=result.id,
            scope="uik",
            identifier=(
                f"source={result.source_artifact_id}:row={result.source_row_number}:"
                f"{result.region_name}/{result.tik_name or '-'}#{result.uik_number or '-'}"
            ),
            code="missing_ballot_accounting",
            severity=Severity.ERROR,
            expected=1,
            actual=0,
        )

    duplicate_groups = session.execute(
        select(
            ResultRecord.ballot_id,
            ResultRecord.region_name,
            ResultRecord.tik_name,
            ResultRecord.uik_number,
            func.count(ResultRecord.id),
        )
        .where(ResultRecord.uik_number.is_not(None))
        .group_by(
            ResultRecord.ballot_id,
            ResultRecord.region_name,
            ResultRecord.tik_name,
            ResultRecord.uik_number,
        )
        .having(func.count(ResultRecord.id) > 1)
    ).all()
    for ballot_id, region, tik, uik, count in duplicate_groups:
        duplicate_ids = list(
            session.scalars(
                select(ResultRecord.id).where(
                    tuple_(
                        ResultRecord.ballot_id,
                        ResultRecord.region_name,
                        ResultRecord.tik_name,
                        ResultRecord.uik_number,
                    )
                    == (ballot_id, region, tik, uik)
                )
            )
        )
        for result_id in duplicate_ids:
            _finding(
                session,
                result_id=result_id,
                scope="uik",
                identifier=f"{region}/{tik or '-'}#{uik}",
                code="duplicate_uik_identity",
                severity=Severity.ERROR,
                expected=1,
                actual=count,
                details={"result_record_ids": duplicate_ids},
            )

    single_member = reconcile_single_member(
        session, expected_districts=expected_single_member_districts
    )
    for finding in single_member.findings:
        _finding(
            session,
            result_id=finding.result_id,
            scope=finding.scope,
            identifier=finding.identifier,
            code=finding.code,
            severity=finding.severity,
            expected=finding.expected,
            actual=finding.actual,
            details=finding.details,
        )

    published_discrepancies = sum(
        finding.code
        in {
            "declared_winner_mismatch",
            "official_accounting_total_mismatch",
            "official_candidate_total_mismatch",
            "official_uik_count_mismatch",
        }
        for finding in single_member.findings
    )
    for published in session.scalars(select(PublishedTotal).order_by(PublishedTotal.id)):
        ballot = session.get(Ballot, published.ballot_id)
        if (
            ballot is not None
            and ballot.kind == BallotKind.SINGLE_MEMBER
            and published.level in {"district", "oik"}
        ):
            # OIK references are compared by the district reconciliation, which can
            # also identify missing roster/reference rows.
            continue
        actual = _actual_published_total(session, published)
        if actual != published.value:
            published_discrepancies += 1
            _finding(
                session,
                result_id=None,
                scope=published.level,
                identifier=published.scope_identifier,
                code="published_total_mismatch",
                severity=Severity.ERROR,
                expected=published.value,
                actual=actual,
                details={
                    "metric": published.metric,
                    "option_name": published.option_name,
                    "source_url": published.source_url,
                },
            )
    session.flush()

    finding_counts = defaultdict(int)
    findings = []
    for finding in session.scalars(select(ValidationFinding).order_by(ValidationFinding.id)):
        finding_counts[finding.severity.value] += 1
        findings.append(
            {
                "result_record_id": finding.result_record_id,
                "scope": finding.scope,
                "scope_identifier": finding.scope_identifier,
                "code": finding.code,
                "severity": finding.severity.value,
                "expected": finding.expected_value,
                "actual": finding.actual_value,
                "details": finding.details_json,
            }
        )
    if single_member.report["applicable"]:
        districts_ready = 0
        for district in single_member.report["districts"]:
            district_findings = [
                finding
                for finding in findings
                if finding["details"].get("district") == district["district"]
            ]
            district["finding_codes"] = sorted(
                {finding["code"] for finding in district_findings}
            )
            district["errors"] = sum(
                finding["severity"] == Severity.ERROR.value
                for finding in district_findings
            )
            district["warnings"] = sum(
                finding["severity"] == Severity.WARNING.value
                for finding in district_findings
            )
            district["ready"] = district["errors"] == 0
            districts_ready += int(district["ready"])
        single_member.report["districts_ready"] = districts_ready
        single_member.report["ready"] = bool(
            single_member.report["districts_found"]
            == single_member.report["expected_districts"]
            and districts_ready == single_member.report["districts_found"]
        )
    aggregates = _aggregate_rows(session)
    national: dict[str, dict[str, int]] = defaultdict(
        lambda: {
            "result_records": 0,
            "registered_voters": 0,
            "valid_ballots": 0,
            "invalid_ballots": 0,
        }
    )
    for aggregate in aggregates:
        bucket = national[aggregate["special_type"]]
        for key in bucket:
            bucket[key] += int(aggregate[key] or 0)
    report: dict[str, Any] = {
        "summary": {
            "result_records": session.scalar(select(func.count(ResultRecord.id))) or 0,
            "findings": len(findings),
            "errors": finding_counts[Severity.ERROR.value],
            "warnings": finding_counts[Severity.WARNING.value],
            "published_total_discrepancies": published_discrepancies,
            "single_member_districts": single_member.report["districts_found"],
            "single_member_districts_ready": single_member.report["districts_ready"],
            "single_member_ready": single_member.report["ready"],
        },
        "national_by_special_type": dict(national),
        "tik_aggregates": aggregates,
        "single_member": single_member.report,
        "findings": findings,
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    from elections.cli import main

    raise SystemExit(main(["validate"]))
