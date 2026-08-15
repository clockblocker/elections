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
    assert {source.label for source in detail.sources} == {
        "Election result",
        "Result source artifact",
        "Commission snapshot",
    }


def test_missing_uik_returns_none(repository: SqlElectionRepository) -> None:
    assert repository.get_uik(9999) is None
