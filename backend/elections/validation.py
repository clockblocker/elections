from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.orm import Session

from elections.models import (
    BallotAccounting,
    BallotOption,
    MatchStatus,
    PublishedTotal,
    ResultRecord,
    Severity,
    ValidationFinding,
    ValidationStatus,
    Vote,
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
    session.add(
        ValidationFinding(
            result_record_id=result_id,
            scope=scope,
            scope_identifier=identifier,
            code=code,
            severity=severity,
            expected_value=expected,
            actual_value=actual,
            details_json=details or {},
        )
    )
    if result_id is not None:
        result = session.get(ResultRecord, result_id)
        if result:
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
    elif published.level != "national":
        raise ValueError(f"unsupported published total level: {published.level}")

    if published.metric == "votes":
        statement = (
            select(func.coalesce(func.sum(Vote.votes), 0))
            .join(ResultRecord, ResultRecord.id == Vote.result_record_id)
            .join(BallotOption, BallotOption.id == Vote.option_id)
            .where(ResultRecord.ballot_id == published.ballot_id, *result_filters)
        )
        if published.option_name:
            statement = statement.where(BallotOption.name == published.option_name)
        return int(session.scalar(statement) or 0)

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
            "result_records": count,
            "registered_voters": registered,
            "valid_ballots": valid,
            "invalid_ballots": invalid,
        }
        for region, tik, special, count, registered, valid, invalid in session.execute(statement)
    ]


def validate_dataset(session: Session, report_path: Path | None = None) -> dict[str, Any]:
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
        identities = (
            ("ballots_received_identity", expected_received, accounting.ballots_received),
            (
                "ballot_box_identity",
                accounting.portable_boxes_ballots + accounting.stationary_boxes_ballots,
                accounting.valid_ballots + accounting.invalid_ballots,
            ),
            ("party_vote_identity", accounting.valid_ballots, int(vote_sum)),
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
        if result.uik_number is None:
            _finding(
                session,
                result_id=result.id,
                scope="uik",
                identifier=identifier,
                code="missing_uik_identifier",
                severity=Severity.WARNING,
            )
        if result.match_status in {
            MatchStatus.RESULT_ONLY,
            MatchStatus.AMBIGUOUS,
            MatchStatus.PENDING,
        }:
            _finding(
                session,
                result_id=result.id,
                scope="uik",
                identifier=identifier,
                code=f"match_{result.match_status.value}",
                severity=Severity.WARNING,
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

    published_discrepancies = 0
    for published in session.scalars(select(PublishedTotal).order_by(PublishedTotal.id)):
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
        },
        "national_by_special_type": dict(national),
        "tik_aggregates": aggregates,
        "findings": findings,
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    from elections.cli import main

    raise SystemExit(main(["validate"]))
