"""SQLAlchemy implementation of the exploration repository."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, joinedload, selectinload, sessionmaker

from elections import models
from elections.api.schemas import (
    Accounting,
    CommissionMember,
    CommissionMetadata,
    DatasetStatus,
    Hierarchy,
    Party,
    PartyResult,
    PointFilters,
    PointPage,
    Region,
    ScatterPoint,
    SourceLink,
    SpecialType,
    Tik,
    UikDetail,
    ValidationFinding,
)
from elections.db import session_factory


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _percentage(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(100 * numerator / denominator, 6)


def _uik_label(value: str | None) -> str:
    return value if value else "Unknown UIK"


def _flags(record: models.ResultRecord) -> list[str]:
    flags: list[str] = []
    special_type = _enum_value(record.special_type)
    if special_type != models.SpecialType.NONE.value:
        flags.append(special_type)
    if record.is_deg and models.SpecialType.DEG.value not in flags:
        flags.append(models.SpecialType.DEG.value)
    return flags


class SqlElectionRepository:
    """Execute read-only queries with one short-lived session per operation."""

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def dataset_status(self) -> DatasetStatus:
        with self._sessions() as session:
            election = session.scalar(
                select(models.Election).order_by(models.Election.election_date.desc()).limit(1)
            )
            latest_run = session.scalar(
                select(models.IngestionRun)
                .order_by(models.IngestionRun.started_at.desc(), models.IngestionRun.id.desc())
                .limit(1)
            )
            result_records = session.scalar(select(func.count(models.ResultRecord.id))) or 0
            matched_records = (
                session.scalar(
                    select(func.count(models.ResultRecord.id)).where(
                        models.ResultRecord.match_status == models.MatchStatus.MATCHED
                    )
                )
                or 0
            )
            unresolved_records = (
                session.scalar(
                    select(func.count(models.ResultRecord.id)).where(
                        models.ResultRecord.match_status.in_(
                            [
                                models.MatchStatus.PENDING,
                                models.MatchStatus.RESULT_ONLY,
                                models.MatchStatus.AMBIGUOUS,
                            ]
                        )
                    )
                )
                or 0
            )
            validation_errors = (
                session.scalar(
                    select(func.count(models.ValidationFinding.id)).where(
                        models.ValidationFinding.severity == models.Severity.ERROR
                    )
                )
                or 0
            )
            source_artifacts = session.scalar(select(func.count(models.SourceArtifact.id))) or 0
            published_totals = session.scalar(select(func.count(models.PublishedTotal.id))) or 0
            not_validated = (
                session.scalar(
                    select(func.count(models.ResultRecord.id)).where(
                        models.ResultRecord.validation_status
                        == models.ValidationStatus.NOT_VALIDATED
                    )
                )
                or 0
            )
            latest_source = session.scalar(
                select(models.SourceArtifact).order_by(models.SourceArtifact.id.desc()).limit(1)
            )
            ingestion_succeeded = bool(
                latest_run and latest_run.status == models.RunStatus.SUCCEEDED
            )
            return DatasetStatus(
                election_slug=election.slug if election else None,
                election_name=election.name if election else None,
                election_date=election.election_date if election else None,
                ingestion_status=_enum_value(latest_run.status) if latest_run else "empty",
                ready=bool(result_records and ingestion_succeeded),
                reconciled=bool(
                    result_records
                    and published_totals
                    and not validation_errors
                    and not not_validated
                ),
                imported_at=latest_run.finished_at if latest_run else None,
                result_records=result_records,
                matched_records=matched_records,
                unresolved_records=unresolved_records,
                validation_errors=validation_errors,
                source_artifacts=source_artifacts,
                published_totals=published_totals,
                latest_source_key=latest_source.key if latest_source else None,
                latest_source_sha256=latest_source.sha256 if latest_source else None,
            )

    def list_parties(self) -> list[Party]:
        with self._sessions() as session:
            options = session.scalars(
                select(models.BallotOption)
                .join(models.Ballot)
                .where(models.Ballot.kind == models.BallotKind.PARTY_LIST)
                .order_by(models.BallotOption.position, models.BallotOption.id)
            ).all()
            return [
                Party(
                    id=option.id,
                    ballot_id=option.ballot_id,
                    name=option.name,
                    short_name=option.short_name,
                    position=option.position,
                )
                for option in options
            ]

    def list_regions(self) -> list[Region]:
        with self._sessions() as session:
            rows = session.execute(
                select(models.ResultRecord.region_name, func.count(models.ResultRecord.id))
                .group_by(models.ResultRecord.region_name)
                .order_by(models.ResultRecord.region_name)
            ).all()
            return [Region(name=name, result_records=count) for name, count in rows]

    def list_tiks(self, region: str | None = None) -> list[Tik]:
        statement = (
            select(
                models.ResultRecord.tik_name,
                models.ResultRecord.region_name,
                func.count(models.ResultRecord.id),
            )
            .where(models.ResultRecord.tik_name.is_not(None))
            .group_by(models.ResultRecord.region_name, models.ResultRecord.tik_name)
            .order_by(models.ResultRecord.region_name, models.ResultRecord.tik_name)
        )
        if region is not None:
            statement = statement.where(models.ResultRecord.region_name == region)
        with self._sessions() as session:
            rows = session.execute(statement).all()
            return [
                Tik(name=name, region_name=region_name, result_records=count)
                for name, region_name, count in rows
            ]

    def list_special_types(self) -> list[SpecialType]:
        labels = {
            models.SpecialType.NONE.value: "Ordinary precinct",
            models.SpecialType.DEG.value: "Remote electronic voting (DEG)",
            models.SpecialType.FOREIGN.value: "Foreign precinct",
            models.SpecialType.TEMPORARY.value: "Temporary precinct",
            models.SpecialType.OTHER.value: "Other special record",
        }
        with self._sessions() as session:
            counts = dict(
                session.execute(
                    select(
                        models.ResultRecord.special_type, func.count(models.ResultRecord.id)
                    ).group_by(models.ResultRecord.special_type)
                ).all()
            )
        return [
            SpecialType(
                value=value.value,
                label=labels[value.value],
                result_records=counts.get(value, 0),
                is_deg=value == models.SpecialType.DEG,
            )
            for value in models.SpecialType
        ]

    def list_points(self, filters: PointFilters) -> PointPage:
        accounted_ballots = (
            models.BallotAccounting.portable_boxes_ballots
            + models.BallotAccounting.stationary_boxes_ballots
        )
        turnout = case(
            (
                models.BallotAccounting.registered_voters > 0,
                100.0 * accounted_ballots / models.BallotAccounting.registered_voters,
            ),
            else_=None,
        )
        party_percent = case(
            (
                models.BallotAccounting.valid_ballots > 0,
                100.0 * models.Vote.votes / models.BallotAccounting.valid_ballots,
            ),
            else_=None,
        )
        conditions = [models.Ballot.kind == models.BallotKind.PARTY_LIST]
        if filters.party_ids:
            conditions.append(models.Vote.option_id.in_(filters.party_ids))
        if filters.regions:
            conditions.append(models.ResultRecord.region_name.in_(filters.regions))
        if filters.tiks:
            conditions.append(models.ResultRecord.tik_name.in_(filters.tiks))
        if filters.special_types:
            conditions.append(models.ResultRecord.special_type.in_(filters.special_types))
        if filters.match_statuses:
            conditions.append(models.ResultRecord.match_status.in_(filters.match_statuses))
        if filters.validation_statuses:
            conditions.append(
                models.ResultRecord.validation_status.in_(filters.validation_statuses)
            )
        if filters.is_deg is not None:
            conditions.append(models.ResultRecord.is_deg == filters.is_deg)
        if filters.turnout_min is not None:
            conditions.append(turnout >= filters.turnout_min)
        if filters.turnout_max is not None:
            conditions.append(turnout <= filters.turnout_max)
        if filters.result_min is not None:
            conditions.append(party_percent >= filters.result_min)
        if filters.result_max is not None:
            conditions.append(party_percent <= filters.result_max)

        base = (
            select(
                models.ResultRecord,
                models.Vote.option_id,
                models.BallotAccounting.registered_voters,
                accounted_ballots.label("accounted_ballots"),
                models.Vote.votes,
                turnout.label("turnout"),
                party_percent.label("party_percent"),
            )
            .join(models.Ballot, models.Ballot.id == models.ResultRecord.ballot_id)
            .join(
                models.BallotAccounting,
                models.BallotAccounting.result_record_id == models.ResultRecord.id,
            )
            .join(models.Vote, models.Vote.result_record_id == models.ResultRecord.id)
            .where(*conditions)
        )
        count_statement = select(func.count()).select_from(base.order_by(None).subquery())
        page_statement = (
            base.order_by(models.ResultRecord.id, models.Vote.option_id)
            .offset(filters.offset)
            .limit(filters.limit)
        )
        with self._sessions() as session:
            total = session.scalar(count_statement) or 0
            rows = session.execute(page_statement).all()
            items = [
                ScatterPoint(
                    result_record_id=record.id,
                    party_id=party_id,
                    uik_number=_uik_label(record.uik_number),
                    tik_name=record.tik_name,
                    region_name=record.region_name,
                    registered_voters=registered_voters,
                    ballots_counted=ballots_counted,
                    party_votes=party_votes,
                    turnout_percent=float(turnout_value) if turnout_value is not None else None,
                    party_percent=(
                        float(party_percent_value) if party_percent_value is not None else None
                    ),
                    match_status=_enum_value(record.match_status),
                    validation_status=_enum_value(record.validation_status),
                    special_type=_enum_value(record.special_type),
                    is_deg=record.is_deg,
                    flags=_flags(record),
                )
                for (
                    record,
                    party_id,
                    registered_voters,
                    ballots_counted,
                    party_votes,
                    turnout_value,
                    party_percent_value,
                ) in rows
            ]
        return PointPage(
            items=items,
            offset=filters.offset,
            limit=filters.limit,
            total=total,
            has_more=filters.offset + len(items) < total,
        )

    def get_uik(self, result_record_id: int) -> UikDetail | None:
        statement = (
            select(models.ResultRecord)
            .where(models.ResultRecord.id == result_record_id)
            .options(
                joinedload(models.ResultRecord.ballot).joinedload(models.Ballot.election),
                joinedload(models.ResultRecord.source_artifact),
                joinedload(models.ResultRecord.accounting),
                selectinload(models.ResultRecord.votes).joinedload(models.Vote.option),
                joinedload(models.ResultRecord.matched_commission).joinedload(
                    models.Commission.source_artifact
                ),
                joinedload(models.ResultRecord.matched_commission).selectinload(
                    models.Commission.memberships
                ),
            )
        )
        with self._sessions() as session:
            record = session.scalar(statement)
            if record is None:
                return None
            findings = session.scalars(
                select(models.ValidationFinding)
                .where(models.ValidationFinding.result_record_id == record.id)
                .order_by(models.ValidationFinding.id)
            ).all()
            return self._detail(record, findings)

    @staticmethod
    def _detail(
        record: models.ResultRecord,
        findings: list[models.ValidationFinding],
    ) -> UikDetail:
        accounting = record.accounting
        counted = (
            accounting.portable_boxes_ballots + accounting.stationary_boxes_ballots
            if accounting
            else None
        )
        party_results = [
            PartyResult(
                party_id=vote.option_id,
                name=vote.option.name,
                short_name=vote.option.short_name,
                position=vote.option.position,
                votes=vote.votes,
                percent=_percentage(vote.votes, accounting.valid_ballots if accounting else None),
            )
            for vote in sorted(record.votes, key=lambda item: (item.option.position, item.id))
        ]
        return UikDetail(
            result_record_id=record.id,
            uik_number=_uik_label(record.uik_number),
            gas_vybory_id=record.gas_vybory_id,
            source_row_number=record.source_row_number,
            hierarchy=Hierarchy(
                election=record.ballot.election.name,
                ballot=record.ballot.name,
                region=record.region_name,
                district=record.oik_name,
                tik=record.tik_name,
                uik=_uik_label(record.uik_number),
            ),
            accounting=Accounting(
                registered_voters=accounting.registered_voters if accounting else None,
                ballots_received=accounting.ballots_received if accounting else None,
                ballots_issued_early=accounting.ballots_issued_early if accounting else None,
                ballots_issued_at_station=(
                    accounting.ballots_issued_at_station if accounting else None
                ),
                ballots_issued_outside_station=(
                    accounting.ballots_issued_outside if accounting else None
                ),
                ballots_cancelled=accounting.ballots_cancelled if accounting else None,
                ballots_in_mobile_boxes=(accounting.portable_boxes_ballots if accounting else None),
                ballots_in_stationary_boxes=(
                    accounting.stationary_boxes_ballots if accounting else None
                ),
                invalid_ballots=accounting.invalid_ballots if accounting else None,
                valid_ballots=accounting.valid_ballots if accounting else None,
                lost_ballots=accounting.lost_ballots if accounting else None,
                unaccounted_ballots=accounting.unaccounted_ballots if accounting else None,
            ),
            turnout_percent=_percentage(
                counted, accounting.registered_voters if accounting else None
            ),
            party_results=party_results,
            commission=SqlElectionRepository._commission(record.matched_commission),
            match_status=_enum_value(record.match_status),
            validation_status=_enum_value(record.validation_status),
            special_type=_enum_value(record.special_type),
            is_deg=record.is_deg,
            flags=_flags(record),
            validation_findings=[
                ValidationFinding(
                    code=finding.code,
                    severity=_enum_value(finding.severity),
                    expected=(
                        str(finding.expected_value) if finding.expected_value is not None else None
                    ),
                    actual=(
                        str(finding.actual_value) if finding.actual_value is not None else None
                    ),
                    details=finding.details_json,
                )
                for finding in findings
            ],
            sources=SqlElectionRepository._sources(record),
        )

    @staticmethod
    def _commission(value: models.Commission | None) -> CommissionMetadata | None:
        if value is None:
            return None
        members = [
            CommissionMember(
                full_name=membership.full_name,
                role=membership.role,
                nominator=membership.nominator,
                start_date=membership.term_start,
                end_date=membership.term_end,
            )
            for membership in sorted(value.memberships, key=lambda item: item.id)
        ]
        chairperson = next(
            (
                member
                for member in members
                if member.role
                and ("chair" in member.role.casefold() or "председател" in member.role.casefold())
            ),
            None,
        )
        return CommissionMetadata(
            id=value.id,
            gas_vybory_id=value.gas_vybory_id,
            name=value.name,
            number=value.number,
            address=value.address,
            latitude=float(value.latitude) if value.latitude is not None else None,
            longitude=float(value.longitude) if value.longitude is not None else None,
            chairperson=chairperson,
            members=members,
        )

    @staticmethod
    def _sources(record: models.ResultRecord) -> list[SourceLink]:
        sources: list[SourceLink] = []
        seen: set[str] = set()

        def add(label: str, url: str | None, artifact: models.SourceArtifact | None) -> None:
            if not url or url in seen:
                return
            seen.add(url)
            sources.append(
                SourceLink(
                    label=label,
                    url=url,
                    artifact_id=artifact.id if artifact else None,
                    sha256=artifact.sha256 if artifact else None,
                    retrieved_at=artifact.retrieved_at if artifact else None,
                )
            )

        add("Election result", record.source_url, record.source_artifact)
        add("Result source artifact", record.source_artifact.url, record.source_artifact)
        if record.matched_commission is not None:
            artifact = record.matched_commission.source_artifact
            add("Commission snapshot", artifact.url, artifact)
        return sources


@lru_cache(maxsize=1)
def create_repository() -> SqlElectionRepository:
    return SqlElectionRepository(session_factory())
