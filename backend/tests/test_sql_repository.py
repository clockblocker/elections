from datetime import UTC, date, datetime

import pytest
from sqlalchemy import create_engine, delete
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from elections import models
from elections.api.schemas import PointFilters
from elections.api.sql_repository import SqlElectionRepository


@pytest.fixture
def repository() -> SqlElectionRepository:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    models.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with sessions.begin() as session:
        result_artifact = models.SourceArtifact(
            key="results",
            url="https://example.test/results.zip",
            sha256="a" * 64,
            retrieved_at=datetime(2021, 9, 20, tzinfo=UTC),
            metadata_json={},
        )
        commission_artifact = models.SourceArtifact(
            key="commissions",
            url="https://example.test/commissions.zip",
            sha256="b" * 64,
            retrieved_at=datetime(2021, 9, 14, tzinfo=UTC),
            metadata_json={},
        )
        gas_parent_artifact = models.SourceArtifact(
            key="gas-parent",
            url="https://example.test/gas/parent",
            sha256="c" * 64,
            retrieved_at=datetime(2021, 9, 21, tzinfo=UTC),
            metadata_json={},
        )
        gas_detail_artifact = models.SourceArtifact(
            key="gas-detail",
            url="https://example.test/gas/uik-123",
            sha256="d" * 64,
            retrieved_at=datetime(2021, 9, 21, tzinfo=UTC),
            metadata_json={},
        )
        election = models.Election(
            slug="duma-2021",
            name="2021 State Duma",
            election_date=date(2021, 9, 19),
        )
        ballot = models.Ballot(
            election=election,
            kind=models.BallotKind.PARTY_LIST,
            name="Federal party list",
        )
        party = models.BallotOption(
            ballot=ballot,
            position=1,
            name="Example Party",
            short_name="EP",
        )
        commission = models.Commission(
            source_artifact=commission_artifact,
            source_record_id="uik-123",
            gas_vybory_id="gas-123",
            type=models.CommissionType.UIK,
            region="Moscow",
            name="UIK 123",
            number="123",
            address="1 Example Street",
            raw_json={},
        )
        commission.memberships.append(
            models.CommissionMembership(
                source_record_id="member-1",
                full_name="Example Chair",
                role="Chairperson",
                nominator="Example nominator",
                raw_json={},
            )
        )
        result = models.ResultRecord(
            source_artifact=result_artifact,
            source_row_number=10,
            ballot=ballot,
            region_name="Moscow",
            oik_name="OIK 1",
            tik_name="Central TIK",
            uik_number="123",
            gas_vybory_id="gas-123",
            source_url="https://example.test/result/123",
            special_type=models.SpecialType.NONE,
            is_deg=False,
            raw_json={},
            match_status=models.MatchStatus.MATCHED,
            matched_commission=commission,
            validation_status=models.ValidationStatus.WARNING,
        )
        result.accounting = models.BallotAccounting(
            registered_voters=1000,
            ballots_received=900,
            ballots_issued_early=10,
            ballots_issued_at_station=490,
            ballots_issued_outside=10,
            ballots_cancelled=390,
            portable_boxes_ballots=10,
            stationary_boxes_ballots=500,
            invalid_ballots=10,
            valid_ballots=500,
            lost_ballots=0,
            unaccounted_ballots=0,
        )
        result.votes.append(models.Vote(option=party, votes=300))
        session.add_all(
            [
                result,
                models.IngestionRun(
                    kind="results",
                    artifact=result_artifact,
                    status=models.RunStatus.SUCCEEDED,
                    started_at=datetime(2021, 9, 20, tzinfo=UTC),
                    finished_at=datetime(2021, 9, 20, tzinfo=UTC),
                    stats_json={},
                ),
            ]
        )
        session.flush()
        session.add(
            models.GasIdResolution(
                result_record_id=result.id,
                status="resolved",
                gas_vybory_id="gas-123",
                parent_source_artifact=gas_parent_artifact,
                detail_source_artifact=gas_detail_artifact,
                commission_source_artifact=commission_artifact,
                evidence_json={"commission_parameter": "action=ik&vrn"},
            )
        )
        session.add(
            models.MatchEvidence(
                result_record_id=result.id,
                commission_id=commission.id,
                method="gas_vybory_id",
                score=1,
                evidence_json={"selection_decision": "selected_unique_exact_candidate"},
                selected=True,
            )
        )
        session.add(
            models.PublishedTotal(
                ballot_id=ballot.id,
                level="national",
                scope_identifier="Russia",
                metric="valid_ballots",
                option_name="",
                value=500,
                source_url="https://example.test/published-total",
            )
        )
        session.add(
            models.ValidationFinding(
                result_record_id=result.id,
                scope="uik",
                scope_identifier="123",
                code="accounting_identity",
                severity=models.Severity.WARNING,
                expected_value=510,
                actual_value=509,
                details_json={"difference": 1},
            )
        )
    return SqlElectionRepository(sessions)


def test_query_metadata_and_status(repository: SqlElectionRepository) -> None:
    status = repository.dataset_status()
    assert status.election_slug == "duma-2021"
    assert status.result_records == 1
    assert status.matched_records == 1
    assert status.published_totals == 1
    assert status.reconciled is True
    assert repository.list_parties()[0].short_name == "EP"
    assert repository.list_regions()[0].result_records == 1
    assert repository.list_tiks(region="Moscow")[0].name == "Central TIK"


def test_dataset_is_not_reconciled_without_published_reference(
    repository: SqlElectionRepository,
) -> None:
    with repository._sessions.begin() as session:
        session.execute(delete(models.PublishedTotal))

    status = repository.dataset_status()

    assert status.published_totals == 0
    assert status.reconciled is False


def test_query_compact_filtered_points(repository: SqlElectionRepository) -> None:
    page = repository.list_points(
        PointFilters(
            party_ids=[1],
            regions=["Moscow"],
            match_statuses=["matched"],
            turnout_min=50,
            turnout_max=52,
            result_min=59,
            result_max=61,
            limit=1,
        )
    )

    assert page.total == 1
    assert page.has_more is False
    point = page.items[0]
    assert point.registered_voters == 1000
    assert point.ballots_counted == 510
    assert point.party_votes == 300
    assert point.turnout_percent == pytest.approx(51)
    assert point.party_percent == pytest.approx(60)
    assert point.matching_method == "gas_vybory_id"


def test_query_complete_uik_detail(repository: SqlElectionRepository) -> None:
    detail = repository.get_uik(1)

    assert detail is not None
    assert detail.hierarchy.model_dump() == {
        "election": "2021 State Duma",
        "ballot": "Federal party list",
        "region": "Moscow",
        "district": "OIK 1",
        "tik": "Central TIK",
        "uik": "123",
    }
    assert detail.accounting.registered_voters == 1000
    assert detail.party_results[0].percent == pytest.approx(60)
    assert detail.commission is not None
    assert detail.commission.chairperson.full_name == "Example Chair"
    assert detail.validation_findings[0].code == "accounting_identity"
    assert detail.gas_vybory_id == "gas-123"
    assert detail.matching_method == "gas_vybory_id"
    assert detail.gas_resolution_status == "resolved"
    assert {source.label for source in detail.sources} == {
        "Election result",
        "Result source artifact",
        "Commission snapshot",
        "GAS parent result table",
        "GAS exact commission identity",
    }


def test_missing_uik_returns_none(repository: SqlElectionRepository) -> None:
    assert repository.get_uik(9999) is None


def _add_single_member_protocol(repository: SqlElectionRepository) -> int:
    with repository._sessions.begin() as session:
        election = session.query(models.Election).one()
        artifact = session.query(models.SourceArtifact).filter_by(key="results").one()
        commission = session.query(models.Commission).one()
        region = models.Geography(type=models.GeographyType.REGION, code="77", name="Moscow")
        oik = models.Geography(
            type=models.GeographyType.OIK,
            code="77-001",
            name="OIK 1",
            parent=region,
        )
        ballot = models.Ballot(
            election=election,
            kind=models.BallotKind.SINGLE_MEMBER,
            scope_key=oik.code,
            oik=oik,
            name="OIK 1 single-member ballot",
        )
        winner = models.Candidate(
            ballot=ballot,
            position=1,
            full_name="Candidate Winner",
            party_affiliation="Example Party",
            registration_status="registered",
        )
        other = models.Candidate(
            ballot=ballot,
            position=2,
            full_name="Candidate Other",
            is_self_nominated=True,
            registration_status="registered",
        )
        result = models.ResultRecord(
            source_artifact=artifact,
            source_row_number=11,
            ballot=ballot,
            region_name="Moscow",
            oik_name="OIK 1",
            tik_name="Central TIK",
            uik_number="123",
            source_url="https://example.test/result/123/district",
            special_type=models.SpecialType.NONE,
            is_deg=False,
            raw_json={},
            match_status=models.MatchStatus.MATCHED,
            matched_commission=commission,
            validation_status=models.ValidationStatus.VALID,
        )
        result.accounting = models.BallotAccounting(
            registered_voters=1000,
            ballots_received=900,
            ballots_issued_early=10,
            ballots_issued_at_station=490,
            ballots_issued_outside=10,
            ballots_cancelled=390,
            portable_boxes_ballots=10,
            stationary_boxes_ballots=500,
            invalid_ballots=10,
            valid_ballots=500,
            lost_ballots=0,
            unaccounted_ballots=0,
        )
        result.votes = [
            models.Vote(candidate=winner, votes=320),
            models.Vote(candidate=other, votes=180),
        ]
        session.add(result)
        session.flush()
        session.add_all(
            [
                models.PublishedTotal(
                    ballot_id=ballot.id,
                    level="oik",
                    scope_identifier=oik.code,
                    metric="votes",
                    option_name=winner.full_name,
                    value=320,
                ),
                models.PublishedTotal(
                    ballot_id=ballot.id,
                    level="oik",
                    scope_identifier=oik.code,
                    metric="votes",
                    option_name=other.full_name,
                    value=180,
                ),
                models.PublishedTotal(
                    ballot_id=ballot.id,
                    level="oik",
                    scope_identifier=oik.code,
                    metric="winner",
                    option_name=winner.full_name,
                    value=1,
                ),
            ]
        )
        return result.id


def test_candidate_metadata_and_filtered_points(repository: SqlElectionRepository) -> None:
    _add_single_member_protocol(repository)

    district = repository.list_districts()[0]
    candidates = repository.list_candidates(oik_id=district.id)
    affiliations = repository.list_affiliations(oik_id=district.id)
    page = repository.list_points(
        PointFilters(
            ballot_kinds=["single_member"],
            oik_ids=[district.id],
            candidate_ids=[candidates[0].id],
            affiliations=["Example Party"],
            winner=True,
        )
    )

    assert district.candidate_count == 2
    assert [candidate.full_name for candidate in candidates] == [
        "Candidate Winner",
        "Candidate Other",
    ]
    assert candidates[0].is_winner is True
    assert affiliations[0].model_dump() == {"value": "Example Party", "candidates": 1}
    assert page.total == 1
    assert page.items[0].candidate_name == "Candidate Winner"
    assert page.items[0].party_affiliation == "Example Party"
    assert page.items[0].party_id is None
    assert page.items[0].is_winner is True


def test_uik_detail_contains_both_ballot_protocols(repository: SqlElectionRepository) -> None:
    result_id = _add_single_member_protocol(repository)

    detail = repository.get_uik(result_id)

    assert detail is not None
    assert {protocol.ballot_kind for protocol in detail.protocols} == {
        "party_list",
        "single_member",
    }
    district_protocol = next(
        protocol for protocol in detail.protocols if protocol.ballot_kind == "single_member"
    )
    assert district_protocol.candidate_results[0].full_name == "Candidate Winner"
    assert district_protocol.candidate_results[0].is_winner is True


def test_candidate_points_keep_missing_accounting_visible(
    repository: SqlElectionRepository,
) -> None:
    result_id = _add_single_member_protocol(repository)
    with repository._sessions.begin() as session:
        session.execute(
            delete(models.BallotAccounting).where(
                models.BallotAccounting.result_record_id == result_id
            )
        )

    page = repository.list_points(PointFilters(ballot_kinds=["single_member"]))

    assert page.total == 2
    assert {point.turnout_percent for point in page.items} == {None}
    assert {point.registered_voters for point in page.items} == {0}
