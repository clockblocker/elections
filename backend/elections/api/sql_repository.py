"""SQLAlchemy implementation of the exploration repository."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import case, func, literal, or_, select
from sqlalchemy.orm import Session, aliased, joinedload, selectinload, sessionmaker

from elections import models
from elections.api.schemas import (
    Accounting,
    Affiliation,
    BallotSummary,
    CandidateResult,
    CandidateSummary,
    CommissionMember,
    CommissionMetadata,
    DatasetStatus,
    District,
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
    UikProtocol,
    ValidationFinding,
)
from elections.db import session_factory
from elections.reconciliation import single_member_ready_for_api


def _enum_value(value: object) -> str:
    return str(getattr(value, "value", value))


def _percentage(numerator: int | None, denominator: int | None) -> float | None:
    if numerator is None or not denominator:
        return None
    return round(100 * numerator / denominator, 6)


def _uik_label(value: str | None) -> str:
    return value if value else "Unknown UIK"


def _flags(record: models.ResultRecord) -> list[str]:
    return _flags_from_values(record.special_type, record.is_deg)


def _flags_from_values(special_type: object, is_deg: bool) -> list[str]:
    flags: list[str] = []
    special_value = _enum_value(special_type)
    if special_value != models.SpecialType.NONE.value:
        flags.append(special_value)
    if is_deg and models.SpecialType.DEG.value not in flags:
        flags.append(models.SpecialType.DEG.value)
    return flags


WINNER_METRICS = ("winner", "declared_winner")


def _winner_expression():
    return (
        select(models.PublishedTotal.id)
        .where(
            models.PublishedTotal.ballot_id == models.Candidate.ballot_id,
            models.PublishedTotal.metric.in_(WINNER_METRICS),
            models.PublishedTotal.option_name == models.Candidate.full_name,
            models.PublishedTotal.value > 0,
        )
        .exists()
    )


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
                                models.MatchStatus.DATA_INTEGRITY_ERROR,
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
            single_member_ready = single_member_ready_for_api(session)
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
                    and single_member_ready
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

    def list_ballots(self) -> list[BallotSummary]:
        with self._sessions() as session:
            ballots = session.scalars(
                select(models.Ballot).order_by(
                    models.Ballot.kind, models.Ballot.scope_key, models.Ballot.id
                )
            ).all()
            return [
                BallotSummary(
                    id=ballot.id,
                    election_id=ballot.election_id,
                    kind=_enum_value(ballot.kind),
                    name=ballot.name,
                    scope_key=ballot.scope_key,
                    oik_id=ballot.oik_id,
                )
                for ballot in ballots
            ]

    def list_districts(self) -> list[District]:
        region = aliased(models.Geography)
        statement = (
            select(models.Ballot, models.Geography, region.name)
            .join(models.Geography, models.Geography.id == models.Ballot.oik_id)
            .outerjoin(region, region.id == models.Geography.parent_id)
            .where(models.Ballot.kind == models.BallotKind.SINGLE_MEMBER)
            .order_by(models.Ballot.scope_key, models.Ballot.id)
        )
        with self._sessions() as session:
            candidate_counts = dict(
                session.execute(
                    select(models.Candidate.ballot_id, func.count(models.Candidate.id)).group_by(
                        models.Candidate.ballot_id
                    )
                ).all()
            )
            result_counts = dict(
                session.execute(
                    select(
                        models.ResultRecord.ballot_id, func.count(models.ResultRecord.id)
                    ).group_by(models.ResultRecord.ballot_id)
                ).all()
            )
            rows = session.execute(statement).all()
            return [
                District(
                    id=oik.id,
                    code=oik.code,
                    name=oik.name,
                    region_name=region_name,
                    ballot_id=ballot.id,
                    candidate_count=candidate_counts.get(ballot.id, 0),
                    result_records=result_counts.get(ballot.id, 0),
                )
                for ballot, oik, region_name in rows
            ]

    def list_candidates(
        self,
        *,
        ballot_id: int | None = None,
        oik_id: int | None = None,
        affiliation: str | None = None,
        winner: bool | None = None,
    ) -> list[CandidateSummary]:
        is_winner = _winner_expression()
        statement = (
            select(models.Candidate, models.Ballot, models.Geography.code, is_winner)
            .join(models.Ballot, models.Ballot.id == models.Candidate.ballot_id)
            .join(models.Geography, models.Geography.id == models.Ballot.oik_id)
            .order_by(models.Ballot.scope_key, models.Candidate.position, models.Candidate.id)
        )
        if ballot_id is not None:
            statement = statement.where(models.Candidate.ballot_id == ballot_id)
        if oik_id is not None:
            statement = statement.where(models.Ballot.oik_id == oik_id)
        if affiliation is not None:
            statement = statement.where(models.Candidate.party_affiliation == affiliation)
        if winner is not None:
            statement = statement.where(is_winner if winner else ~is_winner)
        with self._sessions() as session:
            rows = session.execute(statement).all()
            return [
                CandidateSummary(
                    id=candidate.id,
                    ballot_id=candidate.ballot_id,
                    oik_id=ballot.oik_id,
                    district_code=district_code,
                    position=candidate.position,
                    full_name=candidate.full_name,
                    party_affiliation=candidate.party_affiliation,
                    is_self_nominated=candidate.is_self_nominated,
                    registration_status=candidate.registration_status,
                    is_winner=bool(candidate_is_winner),
                )
                for candidate, ballot, district_code, candidate_is_winner in rows
            ]

    def list_affiliations(self, *, oik_id: int | None = None) -> list[Affiliation]:
        statement = (
            select(models.Candidate.party_affiliation, func.count(models.Candidate.id))
            .join(models.Ballot, models.Ballot.id == models.Candidate.ballot_id)
            .where(models.Candidate.party_affiliation.is_not(None))
            .group_by(models.Candidate.party_affiliation)
            .order_by(models.Candidate.party_affiliation)
        )
        if oik_id is not None:
            statement = statement.where(models.Ballot.oik_id == oik_id)
        with self._sessions() as session:
            return [
                Affiliation(value=value, candidates=count)
                for value, count in session.execute(statement)
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
        accounted_ballots = func.coalesce(
            models.BallotAccounting.portable_boxes_ballots, 0
        ) + func.coalesce(models.BallotAccounting.stationary_boxes_ballots, 0)
        registered_voters = func.coalesce(models.BallotAccounting.registered_voters, 0)
        turnout = case(
            (
                models.BallotAccounting.registered_voters > 0,
                100.0 * accounted_ballots / models.BallotAccounting.registered_voters,
            ),
            else_=None,
        )
        result_percent = case(
            (
                models.BallotAccounting.valid_ballots > 0,
                100.0 * models.Vote.votes / models.BallotAccounting.valid_ballots,
            ),
            else_=None,
        )
        try:
            ballot_kinds = [models.BallotKind(value) for value in filters.ballot_kinds]
        except ValueError:
            ballot_kinds = []
        selected_ballot_kinds = ballot_kinds or [models.BallotKind.PARTY_LIST]
        conditions = [models.Ballot.kind.in_(selected_ballot_kinds)]
        if filters.ballot_ids:
            conditions.append(models.Ballot.id.in_(filters.ballot_ids))
        if filters.oik_ids:
            conditions.append(models.Ballot.oik_id.in_(filters.oik_ids))
        if filters.party_ids:
            conditions.append(models.Vote.option_id.in_(filters.party_ids))
        if filters.candidate_ids:
            conditions.append(models.Vote.candidate_id.in_(filters.candidate_ids))
        if filters.affiliations:
            conditions.append(models.Candidate.party_affiliation.in_(filters.affiliations))
        is_winner = (
            _winner_expression()
            if models.BallotKind.SINGLE_MEMBER in selected_ballot_kinds
            else literal(False)
        )
        if filters.winner is not None:
            conditions.append(is_winner if filters.winner else ~is_winner)
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
            conditions.append(result_percent >= filters.result_min)
        if filters.result_max is not None:
            conditions.append(result_percent <= filters.result_max)

        def with_point_joins(statement):
            return (
                statement.select_from(models.ResultRecord)
                .join(models.Ballot, models.Ballot.id == models.ResultRecord.ballot_id)
                .outerjoin(
                    models.BallotAccounting,
                    models.BallotAccounting.result_record_id == models.ResultRecord.id,
                )
                .join(models.Vote, models.Vote.result_record_id == models.ResultRecord.id)
                .outerjoin(models.Candidate, models.Candidate.id == models.Vote.candidate_id)
                .where(*conditions)
            )

        count_statement = with_point_joins(select(func.count()))
        page_statement = with_point_joins(
            select(
                models.ResultRecord.id.label("result_record_id"),
                models.Ballot.id.label("ballot_id"),
                models.Ballot.kind.label("ballot_kind"),
                models.Ballot.scope_key.label("scope_key"),
                models.Ballot.oik_id.label("oik_id"),
                models.Vote.option_id.label("party_id"),
                models.Candidate.id.label("candidate_id"),
                models.Candidate.full_name.label("candidate_name"),
                models.Candidate.party_affiliation.label("party_affiliation"),
                is_winner.label("is_winner"),
                models.ResultRecord.uik_number.label("uik_number"),
                models.ResultRecord.tik_name.label("tik_name"),
                models.ResultRecord.region_name.label("region_name"),
                registered_voters.label("registered_voters"),
                accounted_ballots.label("accounted_ballots"),
                models.Vote.votes.label("party_votes"),
                turnout.label("turnout"),
                result_percent.label("result_percent"),
                models.ResultRecord.match_status.label("match_status"),
                models.ResultRecord.validation_status.label("validation_status"),
                models.ResultRecord.special_type.label("special_type"),
                models.ResultRecord.is_deg.label("is_deg"),
            )
        )
        page_statement = (
            page_statement
            .order_by(
                models.ResultRecord.id,
                models.Vote.option_id,
                models.Vote.candidate_id,
            )
            .offset(filters.offset)
            .limit(filters.limit if filters.include_total else filters.limit + 1)
        )
        with self._sessions() as session:
            total = (session.scalar(count_statement) or 0) if filters.include_total else None
            rows = session.execute(page_statement).mappings().all()
            if total is None:
                has_more = len(rows) > filters.limit
                rows = rows[: filters.limit]
            else:
                has_more = filters.offset + len(rows) < total
            items = [
                # Database constraints and the explicit projection establish these types.
                # Avoid re-validating ~100k trusted rows one model at a time.
                ScatterPoint.model_construct(
                    result_record_id=row["result_record_id"],
                    ballot_id=row["ballot_id"],
                    ballot_kind=_enum_value(row["ballot_kind"]),
                    scope_key=row["scope_key"],
                    oik_id=row["oik_id"],
                    party_id=row["party_id"],
                    candidate_id=row["candidate_id"],
                    candidate_name=row["candidate_name"],
                    party_affiliation=row["party_affiliation"],
                    is_winner=bool(row["is_winner"]),
                    uik_number=_uik_label(row["uik_number"]),
                    tik_name=row["tik_name"],
                    region_name=row["region_name"],
                    registered_voters=row["registered_voters"],
                    ballots_counted=row["accounted_ballots"],
                    party_votes=row["party_votes"],
                    turnout_percent=(
                        float(row["turnout"]) if row["turnout"] is not None else None
                    ),
                    party_percent=(
                        float(row["result_percent"])
                        if row["result_percent"] is not None
                        else None
                    ),
                    match_status=_enum_value(row["match_status"]),
                    matching_method=None,
                    validation_status=_enum_value(row["validation_status"]),
                    special_type=(
                        None
                        if _enum_value(row["special_type"]) == models.SpecialType.NONE.value
                        else _enum_value(row["special_type"])
                    ),
                    is_deg=row["is_deg"],
                    flags=_flags_from_values(row["special_type"], row["is_deg"]),
                )
                for row in rows
            ]
        return PointPage.model_construct(
            items=items,
            offset=filters.offset,
            limit=filters.limit,
            total=total,
            has_more=has_more,
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
                selectinload(models.ResultRecord.votes).joinedload(models.Vote.candidate),
                joinedload(models.ResultRecord.matched_commission).joinedload(
                    models.Commission.source_artifact
                ),
                joinedload(models.ResultRecord.matched_commission).selectinload(
                    models.Commission.memberships
                ),
                selectinload(models.ResultRecord.match_evidence),
                joinedload(models.ResultRecord.gas_resolution).joinedload(
                    models.GasIdResolution.parent_source_artifact
                ),
                joinedload(models.ResultRecord.gas_resolution).joinedload(
                    models.GasIdResolution.detail_source_artifact
                ),
            )
        )
        with self._sessions() as session:
            record = session.scalar(statement)
            if record is None:
                return None
            linked_statement = (
                select(models.ResultRecord)
                .join(models.Ballot)
                .where(
                    models.Ballot.election_id == record.ballot.election_id,
                    or_(
                        (models.ResultRecord.matched_commission_id == record.matched_commission_id)
                        if record.matched_commission_id is not None
                        else False,
                        (
                            (models.ResultRecord.region_name == record.region_name)
                            & (models.ResultRecord.tik_name == record.tik_name)
                            & (models.ResultRecord.uik_number == record.uik_number)
                        )
                        if record.uik_number is not None
                        else False,
                        models.ResultRecord.id == record.id,
                    ),
                )
                .options(
                    joinedload(models.ResultRecord.ballot).joinedload(models.Ballot.election),
                    joinedload(models.ResultRecord.source_artifact),
                    joinedload(models.ResultRecord.accounting),
                    selectinload(models.ResultRecord.votes).joinedload(models.Vote.option),
                    selectinload(models.ResultRecord.votes).joinedload(models.Vote.candidate),
                    selectinload(models.ResultRecord.match_evidence),
                    joinedload(models.ResultRecord.gas_resolution).joinedload(
                        models.GasIdResolution.parent_source_artifact
                    ),
                    joinedload(models.ResultRecord.gas_resolution).joinedload(
                        models.GasIdResolution.detail_source_artifact
                    ),
                )
                .order_by(models.Ballot.kind, models.ResultRecord.id)
            )
            linked_records = list(session.scalars(linked_statement).unique())
            ballot_ids = {item.ballot_id for item in linked_records}
            winner_rows = session.execute(
                select(models.PublishedTotal.ballot_id, models.PublishedTotal.option_name).where(
                    models.PublishedTotal.ballot_id.in_(ballot_ids),
                    models.PublishedTotal.metric.in_(WINNER_METRICS),
                    models.PublishedTotal.value > 0,
                )
            )
            winner_names = {(ballot_id, name) for ballot_id, name in winner_rows}
            findings = session.scalars(
                select(models.ValidationFinding)
                .where(models.ValidationFinding.result_record_id == record.id)
                .order_by(models.ValidationFinding.id)
            ).all()
            protocols = [self._protocol(item, winner_names) for item in linked_records]
            return self._detail(record, findings, protocols, winner_names)

    @staticmethod
    def _detail(
        record: models.ResultRecord,
        findings: list[models.ValidationFinding],
        protocols: list[UikProtocol],
        winner_names: set[tuple[int, str]],
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
            for vote in sorted(
                (item for item in record.votes if item.option is not None),
                key=lambda item: (item.option.position, item.id),
            )
        ]
        candidate_results = SqlElectionRepository._candidate_results(record, winner_names)
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
            candidate_results=candidate_results,
            protocols=protocols,
            commission=SqlElectionRepository._commission(record.matched_commission),
            match_status=_enum_value(record.match_status),
            matching_method=SqlElectionRepository._matching_method(record),
            gas_resolution_status=(record.gas_resolution.status if record.gas_resolution else None),
            gas_resolution_reason=(
                record.gas_resolution.reason_code if record.gas_resolution else None
            ),
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
    def _accounting(value: models.BallotAccounting | None) -> Accounting:
        return Accounting(
            registered_voters=value.registered_voters if value else None,
            ballots_received=value.ballots_received if value else None,
            ballots_issued_early=value.ballots_issued_early if value else None,
            ballots_issued_at_station=value.ballots_issued_at_station if value else None,
            ballots_issued_outside_station=value.ballots_issued_outside if value else None,
            ballots_cancelled=value.ballots_cancelled if value else None,
            ballots_in_mobile_boxes=value.portable_boxes_ballots if value else None,
            ballots_in_stationary_boxes=value.stationary_boxes_ballots if value else None,
            invalid_ballots=value.invalid_ballots if value else None,
            valid_ballots=value.valid_ballots if value else None,
            lost_ballots=value.lost_ballots if value else None,
            unaccounted_ballots=value.unaccounted_ballots if value else None,
        )

    @staticmethod
    def _candidate_results(
        record: models.ResultRecord,
        winner_names: set[tuple[int, str]],
    ) -> list[CandidateResult]:
        accounting = record.accounting
        return [
            CandidateResult(
                candidate_id=vote.candidate.id,
                full_name=vote.candidate.full_name,
                position=vote.candidate.position,
                party_affiliation=vote.candidate.party_affiliation,
                is_self_nominated=vote.candidate.is_self_nominated,
                registration_status=vote.candidate.registration_status,
                votes=vote.votes,
                percent=_percentage(vote.votes, accounting.valid_ballots if accounting else None),
                is_winner=(record.ballot_id, vote.candidate.full_name) in winner_names,
            )
            for vote in sorted(
                (item for item in record.votes if item.candidate is not None),
                key=lambda item: (item.candidate.position, item.id),
            )
        ]

    @staticmethod
    def _protocol(
        record: models.ResultRecord,
        winner_names: set[tuple[int, str]],
    ) -> UikProtocol:
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
            for vote in sorted(
                (item for item in record.votes if item.option is not None),
                key=lambda item: (item.option.position, item.id),
            )
        ]
        return UikProtocol(
            result_record_id=record.id,
            ballot_id=record.ballot_id,
            ballot_kind=_enum_value(record.ballot.kind),
            ballot_name=record.ballot.name,
            scope_key=record.ballot.scope_key,
            accounting=SqlElectionRepository._accounting(accounting),
            turnout_percent=_percentage(
                counted, accounting.registered_voters if accounting else None
            ),
            party_results=party_results,
            candidate_results=SqlElectionRepository._candidate_results(record, winner_names),
            match_status=_enum_value(record.match_status),
            matching_method=SqlElectionRepository._matching_method(record),
            validation_status=_enum_value(record.validation_status),
            special_type=_enum_value(record.special_type),
            is_deg=record.is_deg,
            flags=_flags(record),
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
    def _matching_method(record: models.ResultRecord) -> str | None:
        selected = [item.method for item in record.match_evidence if item.selected]
        return selected[0] if len(selected) == 1 else None

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
        for vote in record.votes:
            if vote.candidate is not None and vote.candidate.source_artifact is not None:
                artifact = vote.candidate.source_artifact
                add("Candidate roster source", artifact.url, artifact)
        if record.matched_commission is not None:
            artifact = record.matched_commission.source_artifact
            add("Commission snapshot", artifact.url, artifact)
        if record.gas_resolution is not None:
            add(
                "GAS parent result table",
                (
                    record.gas_resolution.parent_source_artifact.url
                    if record.gas_resolution.parent_source_artifact
                    else None
                ),
                record.gas_resolution.parent_source_artifact,
            )
            add(
                "GAS exact commission identity",
                (
                    record.gas_resolution.detail_source_artifact.url
                    if record.gas_resolution.detail_source_artifact
                    else None
                ),
                record.gas_resolution.detail_source_artifact,
            )
        return sources


@lru_cache(maxsize=1)
def create_repository() -> SqlElectionRepository:
    return SqlElectionRepository(session_factory())
