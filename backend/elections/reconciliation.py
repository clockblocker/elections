from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from elections.models import (
    Ballot,
    BallotAccounting,
    BallotKind,
    Candidate,
    PublishedTotal,
    ResultRecord,
    Severity,
    SpecialType,
    ValidationStatus,
    Vote,
)

EXPECTED_SINGLE_MEMBER_DISTRICTS = 225
WINNER_METRICS = {"declared_winner", "winner"}
VOTE_METRICS = {"candidate_votes", "votes"}
PROTOCOL_COUNT_METRICS = {"result_records", "uik_count"}
ACCOUNTING_METRICS = tuple(
    column.name
    for column in BallotAccounting.__table__.columns
    if column.name != "result_record_id"
)


@dataclass(frozen=True)
class ReconciliationFinding:
    result_id: int | None
    scope: str
    identifier: str
    code: str
    severity: Severity
    expected: int | None = None
    actual: int | None = None
    details: dict[str, Any] | None = None


@dataclass(frozen=True)
class SingleMemberReconciliation:
    report: dict[str, Any]
    findings: list[ReconciliationFinding]


def _special_value(value: SpecialType | str) -> str:
    return value.value if isinstance(value, SpecialType) else str(value)


def _district_identifier(ballot: Ballot) -> str:
    return ballot.scope_key


def _protocol_identifier(record: ResultRecord) -> str:
    return (
        f"source={record.source_artifact_id}:row={record.source_row_number}:"
        f"{record.region_name}/{record.oik_name or '-'}"
        f"/{record.tik_name or '-'}#{record.uik_number or '-'}"
    )


def _protocol_details(record: ResultRecord, district: str) -> dict[str, Any]:
    return {
        "district": district,
        "region": record.region_name,
        "tik": record.tik_name,
        "uik": record.uik_number,
        "source_artifact_id": record.source_artifact_id,
        "source_row_number": record.source_row_number,
        "source_url": record.source_url,
        "special_type": _special_value(record.special_type),
    }


def _accounting_dict(row: Any) -> dict[str, int]:
    return {metric: int(getattr(row, metric) or 0) for metric in ACCOUNTING_METRICS}


def _add_accounting(target: dict[str, int], values: dict[str, int]) -> None:
    for key, value in values.items():
        target[key] += value


def _candidate_totals(
    session: Session, ballot_id: int
) -> tuple[dict[int, int], dict[str, dict[int, int]]]:
    totals: dict[int, int] = defaultdict(int)
    by_special: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    rows = session.execute(
        select(ResultRecord.special_type, Vote.candidate_id, func.sum(Vote.votes))
        .join(Vote, Vote.result_record_id == ResultRecord.id)
        .where(ResultRecord.ballot_id == ballot_id, Vote.candidate_id.is_not(None))
        .group_by(ResultRecord.special_type, Vote.candidate_id)
    )
    for special_type, candidate_id, votes in rows:
        value = int(votes or 0)
        totals[int(candidate_id)] += value
        by_special[_special_value(special_type)][int(candidate_id)] += value
    return dict(totals), {key: dict(value) for key, value in by_special.items()}


def _tik_rollups(
    session: Session, ballot_id: int, candidate_names: dict[int, str]
) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str | None, str], dict[str, Any]] = {}
    accounting_rows = session.execute(
        select(
            ResultRecord.region_name,
            ResultRecord.tik_name,
            ResultRecord.special_type,
            func.count(ResultRecord.id),
            *(func.sum(getattr(BallotAccounting, metric)) for metric in ACCOUNTING_METRICS),
        )
        .join(BallotAccounting, BallotAccounting.result_record_id == ResultRecord.id)
        .where(ResultRecord.ballot_id == ballot_id)
        .group_by(ResultRecord.region_name, ResultRecord.tik_name, ResultRecord.special_type)
    )
    for row in accounting_rows:
        region, tik, special_type, count, *accounting_values = row
        special = _special_value(special_type)
        buckets[(region, tik, special)] = {
            "region": region,
            "tik": tik,
            "special_type": special,
            "result_records": int(count),
            "candidate_votes": {},
            **{
                metric: int(value or 0)
                for metric, value in zip(ACCOUNTING_METRICS, accounting_values, strict=True)
            },
        }

    vote_rows = session.execute(
        select(
            ResultRecord.region_name,
            ResultRecord.tik_name,
            ResultRecord.special_type,
            Vote.candidate_id,
            func.sum(Vote.votes),
        )
        .join(Vote, Vote.result_record_id == ResultRecord.id)
        .where(ResultRecord.ballot_id == ballot_id, Vote.candidate_id.is_not(None))
        .group_by(
            ResultRecord.region_name,
            ResultRecord.tik_name,
            ResultRecord.special_type,
            Vote.candidate_id,
        )
    )
    for region, tik, special_type, candidate_id, votes in vote_rows:
        key = (region, tik, _special_value(special_type))
        bucket = buckets.setdefault(
            key,
            {
                "region": region,
                "tik": tik,
                "special_type": key[2],
                "result_records": 0,
                "candidate_votes": {},
                **dict.fromkeys(ACCOUNTING_METRICS, 0),
            },
        )
        bucket["candidate_votes"][candidate_names.get(int(candidate_id), str(candidate_id))] = int(
            votes or 0
        )
    return sorted(
        buckets.values(),
        key=lambda item: (item["region"], item["tik"] or "", item["special_type"]),
    )


def _published_references(
    session: Session, ballot_id: int
) -> tuple[
    dict[str, PublishedTotal],
    list[PublishedTotal],
    list[PublishedTotal],
    list[PublishedTotal],
]:
    rows = list(
        session.scalars(
            select(PublishedTotal)
            .where(PublishedTotal.ballot_id == ballot_id)
            .order_by(PublishedTotal.id)
        )
    )
    candidate_totals = {
        row.option_name: row
        for row in rows
        if row.level in {"district", "oik"}
        and row.metric in VOTE_METRICS
        and row.option_name
    }
    winners = [
        row
        for row in rows
        if row.level in {"district", "oik"}
        and row.metric in WINNER_METRICS
        and row.option_name
        and row.value
    ]
    accounting = [
        row
        for row in rows
        if row.level in {"district", "oik"} and row.metric in ACCOUNTING_METRICS
    ]
    protocol_counts = [
        row
        for row in rows
        if row.level in {"district", "oik"} and row.metric in PROTOCOL_COUNT_METRICS
    ]
    return candidate_totals, winners, accounting, protocol_counts


def single_member_ready_for_api(
    session: Session,
    *,
    expected_districts: int = EXPECTED_SINGLE_MEMBER_DISTRICTS,
) -> bool:
    """Return whether single-member data has the references needed to claim readiness.

    No single-member ballots means that the gate is not applicable. Once any such
    ballot exists, all 225 district ballots, protocols, candidate totals, one declared
    winner per district, and a completed validation pass are required.
    """

    ballot_ids = list(
        session.scalars(select(Ballot.id).where(Ballot.kind == BallotKind.SINGLE_MEMBER))
    )
    if not ballot_ids:
        return True
    if len(ballot_ids) != expected_districts:
        return False

    candidate_counts = dict(
        session.execute(
            select(Candidate.ballot_id, func.count(Candidate.id))
            .where(Candidate.ballot_id.in_(ballot_ids))
            .group_by(Candidate.ballot_id)
        ).all()
    )
    record_counts = dict(
        session.execute(
            select(ResultRecord.ballot_id, func.count(ResultRecord.id))
            .where(ResultRecord.ballot_id.in_(ballot_ids))
            .group_by(ResultRecord.ballot_id)
        ).all()
    )
    official_candidate_counts = dict(
        session.execute(
            select(PublishedTotal.ballot_id, func.count(PublishedTotal.id))
            .where(
                PublishedTotal.ballot_id.in_(ballot_ids),
                PublishedTotal.level.in_(["district", "oik"]),
                PublishedTotal.metric.in_(VOTE_METRICS),
                PublishedTotal.option_name != "",
            )
            .group_by(PublishedTotal.ballot_id)
        ).all()
    )
    official_winner_counts = dict(
        session.execute(
            select(PublishedTotal.ballot_id, func.count(PublishedTotal.id))
            .where(
                PublishedTotal.ballot_id.in_(ballot_ids),
                PublishedTotal.level.in_(["district", "oik"]),
                PublishedTotal.metric.in_(WINNER_METRICS),
                PublishedTotal.option_name != "",
                PublishedTotal.value == 1,
            )
            .group_by(PublishedTotal.ballot_id)
        ).all()
    )
    for ballot_id in ballot_ids:
        candidate_count = candidate_counts.get(ballot_id, 0)
        record_count = record_counts.get(ballot_id, 0)
        official_candidate_count = official_candidate_counts.get(ballot_id, 0)
        official_winner_count = official_winner_counts.get(ballot_id, 0)
        if (
            not candidate_count
            or not record_count
            or official_candidate_count != candidate_count
            or official_winner_count != 1
        ):
            return False
    not_validated = session.scalar(
        select(func.count(ResultRecord.id)).where(
            ResultRecord.ballot_id.in_(ballot_ids),
            ResultRecord.validation_status == ValidationStatus.NOT_VALIDATED,
        )
    ) or 0
    return not not_validated


def reconcile_single_member(
    session: Session,
    *,
    expected_districts: int = EXPECTED_SINGLE_MEMBER_DISTRICTS,
) -> SingleMemberReconciliation:
    """Build the OIK/TIK/UIK reconciliation trail for single-member ballots.

    Published candidate totals use ``PublishedTotal(level="oik", metric="votes")``.
    A declared winner is represented by an OIK row whose metric is ``winner`` (or
    ``declared_winner``), option name is the candidate name, and value is 1.
    """

    ballots = list(
        session.scalars(
            select(Ballot)
            .where(Ballot.kind == BallotKind.SINGLE_MEMBER)
            .order_by(Ballot.scope_key, Ballot.id)
        )
    )
    if not ballots:
        return SingleMemberReconciliation(
            report={
                "applicable": False,
                "expected_districts": expected_districts,
                "districts_found": 0,
                "districts_ready": 0,
                "ready": False,
                "districts": [],
            },
            findings=[],
        )

    findings: list[ReconciliationFinding] = []
    if len(ballots) != expected_districts:
        findings.append(
            ReconciliationFinding(
                result_id=None,
                scope="national",
                identifier="single-member-districts",
                code="single_member_district_coverage",
                severity=Severity.ERROR,
                expected=expected_districts,
                actual=len(ballots),
                details={
                    "district": None,
                    "uik": None,
                    "source_url": None,
                    "district_scope_keys": [_district_identifier(ballot) for ballot in ballots],
                },
            )
        )

    district_reports: list[dict[str, Any]] = []
    ready_count = 0
    for ballot in ballots:
        district = _district_identifier(ballot)
        candidates = list(
            session.scalars(
                select(Candidate)
                .where(Candidate.ballot_id == ballot.id)
                .order_by(Candidate.position, Candidate.id)
            )
        )
        candidate_names = {candidate.id: candidate.full_name for candidate in candidates}
        records = list(
            session.scalars(
                select(ResultRecord)
                .where(ResultRecord.ballot_id == ballot.id)
                .options(selectinload(ResultRecord.accounting))
                .order_by(ResultRecord.region_name, ResultRecord.tik_name, ResultRecord.id)
            )
        )
        candidate_totals, candidate_totals_by_special = _candidate_totals(session, ballot.id)
        (
            published_totals,
            published_winners,
            published_accounting,
            published_protocol_counts,
        ) = _published_references(session, ballot.id)
        district_finding_start = len(findings)
        accounting_totals = dict.fromkeys(ACCOUNTING_METRICS, 0)
        for record in records:
            if record.accounting is not None:
                _add_accounting(accounting_totals, _accounting_dict(record.accounting))

        if not records:
            findings.append(
                ReconciliationFinding(
                    result_id=None,
                    scope="oik",
                    identifier=district,
                    code="district_source_gap",
                    severity=Severity.ERROR,
                    expected=1,
                    actual=0,
                    details={"district": district, "uik": None, "source_url": None},
                )
            )
        if not candidates:
            findings.append(
                ReconciliationFinding(
                    result_id=None,
                    scope="oik",
                    identifier=district,
                    code="missing_candidate_roster",
                    severity=Severity.ERROR,
                    expected=1,
                    actual=0,
                    details={"district": district, "uik": None, "source_url": None},
                )
            )

        expected_candidate_ids = set(candidate_names)
        if expected_candidate_ids:
            vote_candidate_rows = session.execute(
                select(Vote.result_record_id, Vote.candidate_id)
                .join(ResultRecord, ResultRecord.id == Vote.result_record_id)
                .where(ResultRecord.ballot_id == ballot.id, Vote.candidate_id.is_not(None))
            )
            actual_by_result: dict[int, set[int]] = defaultdict(set)
            for result_id, candidate_id in vote_candidate_rows:
                actual_by_result[int(result_id)].add(int(candidate_id))
            for record in records:
                missing_ids = expected_candidate_ids - actual_by_result[record.id]
                if missing_ids:
                    details = _protocol_details(record, district)
                    details["missing_candidates"] = [
                        candidate_names[candidate_id]
                        for candidate_id in sorted(missing_ids, key=candidate_names.get)
                    ]
                    findings.append(
                        ReconciliationFinding(
                            result_id=record.id,
                            scope="uik",
                            identifier=_protocol_identifier(record),
                            code="missing_candidate_votes",
                            severity=Severity.ERROR,
                            expected=len(expected_candidate_ids),
                            actual=len(actual_by_result[record.id]),
                            details=details,
                        )
                    )

        roster_names = {candidate.full_name for candidate in candidates}
        if not published_totals:
            findings.append(
                ReconciliationFinding(
                    result_id=None,
                    scope="oik",
                    identifier=district,
                    code="missing_official_candidate_totals",
                    severity=Severity.ERROR,
                    expected=len(candidates),
                    actual=0,
                    details={"district": district, "uik": None, "source_url": None},
                )
            )
        else:
            for candidate in candidates:
                published = published_totals.get(candidate.full_name)
                if published is None:
                    findings.append(
                        ReconciliationFinding(
                            result_id=None,
                            scope="oik",
                            identifier=district,
                            code="missing_official_candidate_total",
                            severity=Severity.ERROR,
                            expected=1,
                            actual=0,
                            details={
                                "district": district,
                                "candidate": candidate.full_name,
                                "uik": None,
                                "source_url": None,
                            },
                        )
                    )
                elif published.value != candidate_totals.get(candidate.id, 0):
                    findings.append(
                        ReconciliationFinding(
                            result_id=None,
                            scope="oik",
                            identifier=district,
                            code="official_candidate_total_mismatch",
                            severity=Severity.ERROR,
                            expected=published.value,
                            actual=candidate_totals.get(candidate.id, 0),
                            details={
                                "district": district,
                                "candidate": candidate.full_name,
                                "uik": None,
                                "source_url": published.source_url,
                            },
                        )
                    )
            for name, published in published_totals.items():
                if name not in roster_names:
                    findings.append(
                        ReconciliationFinding(
                            result_id=None,
                            scope="oik",
                            identifier=district,
                            code="unknown_official_candidate",
                            severity=Severity.ERROR,
                            expected=0,
                            actual=published.value,
                            details={
                                "district": district,
                                "candidate": name,
                                "uik": None,
                                "source_url": published.source_url,
                            },
                        )
                    )

        highest_total = max(candidate_totals.values(), default=0)
        derived_winners = sorted(
            candidate_names[candidate_id]
            for candidate_id, total in candidate_totals.items()
            if total == highest_total
        )
        official_winner_names = sorted({row.option_name for row in published_winners})
        if len(official_winner_names) != 1:
            findings.append(
                ReconciliationFinding(
                    result_id=None,
                    scope="oik",
                    identifier=district,
                    code="missing_or_ambiguous_official_winner",
                    severity=Severity.ERROR,
                    expected=1,
                    actual=len(official_winner_names),
                    details={
                        "district": district,
                        "official_winners": official_winner_names,
                        "uik": None,
                        "source_url": published_winners[0].source_url
                        if published_winners
                        else None,
                    },
                )
            )

        elif official_winner_names != derived_winners:
            findings.append(
                ReconciliationFinding(
                    result_id=None,
                    scope="oik",
                    identifier=district,
                    code="declared_winner_mismatch",
                    severity=Severity.ERROR,
                    expected=1,
                    actual=int(official_winner_names[0] in derived_winners),
                    details={
                        "district": district,
                        "declared_winner": official_winner_names[0],
                        "derived_winners": derived_winners,
                        "uik": None,
                        "source_url": published_winners[0].source_url,
                    },
                )
            )

        for published in published_accounting:
            actual = accounting_totals[published.metric]
            if actual != published.value:
                findings.append(
                    ReconciliationFinding(
                        result_id=None,
                        scope="oik",
                        identifier=district,
                        code="official_accounting_total_mismatch",
                        severity=Severity.ERROR,
                        expected=published.value,
                        actual=actual,
                        details={
                            "district": district,
                            "metric": published.metric,
                            "uik": None,
                            "source_url": published.source_url,
                        },
                    )
                )

        for published in published_protocol_counts:
            if len(records) != published.value:
                findings.append(
                    ReconciliationFinding(
                        result_id=None,
                        scope="oik",
                        identifier=district,
                        code="official_uik_count_mismatch",
                        severity=Severity.ERROR,
                        expected=published.value,
                        actual=len(records),
                        details={
                            "district": district,
                            "metric": published.metric,
                            "uik": None,
                            "source_url": published.source_url,
                        },
                    )
                )

        totals_by_special: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "result_records": 0,
                "candidate_votes": {},
                **dict.fromkeys(ACCOUNTING_METRICS, 0),
            }
        )
        for record in records:
            special = _special_value(record.special_type)
            bucket = totals_by_special[special]
            bucket["result_records"] += 1
            if record.accounting is not None:
                _add_accounting(bucket, _accounting_dict(record.accounting))
        for special, special_totals in candidate_totals_by_special.items():
            totals_by_special[special]["candidate_votes"] = {
                candidate_names.get(candidate_id, str(candidate_id)): value
                for candidate_id, value in sorted(
                    special_totals.items(), key=lambda item: candidate_names.get(item[0], "")
                )
            }

        tik_rollups = _tik_rollups(session, ballot.id, candidate_names)
        tik_candidate_totals: dict[str, int] = defaultdict(int)
        for rollup in tik_rollups:
            for candidate_name, votes in rollup["candidate_votes"].items():
                tik_candidate_totals[candidate_name] += votes
        named_candidate_totals = {
            candidate.full_name: candidate_totals.get(candidate.id, 0) for candidate in candidates
        }
        district_findings = findings[district_finding_start:]
        district_ready = not any(item.severity == Severity.ERROR for item in district_findings)
        ready_count += int(district_ready)
        district_reports.append(
            {
                "district": district,
                "ballot_id": ballot.id,
                "ballot_name": ballot.name,
                "result_records": len(records),
                "candidate_count": len(candidates),
                "accounting_totals": accounting_totals,
                "candidate_totals": named_candidate_totals,
                "derived_winners": derived_winners,
                "official_candidate_totals": {
                    name: row.value for name, row in published_totals.items()
                },
                "official_winners": official_winner_names,
                "official_references_present": bool(published_totals and published_winners),
                "totals_by_special_type": dict(totals_by_special),
                "tik_rollups": tik_rollups,
                "uik_to_tik_reconciled": True,
                "tik_to_oik_reconciled": dict(tik_candidate_totals)
                == named_candidate_totals,
                "finding_codes": sorted({item.code for item in district_findings}),
                "ready": district_ready,
            }
        )

    all_ready = len(ballots) == expected_districts and ready_count == len(ballots)
    return SingleMemberReconciliation(
        report={
            "applicable": True,
            "expected_districts": expected_districts,
            "districts_found": len(ballots),
            "districts_ready": ready_count,
            "ready": all_ready,
            "districts": district_reports,
        },
        findings=findings,
    )
