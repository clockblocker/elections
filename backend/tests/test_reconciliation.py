from __future__ import annotations

from datetime import date

from sqlalchemy import delete, select

from elections.models import (
    Ballot,
    BallotAccounting,
    BallotKind,
    Candidate,
    Election,
    Geography,
    GeographyType,
    MatchStatus,
    PublishedTotal,
    ResultRecord,
    SourceArtifact,
    SpecialType,
    ValidationFinding,
    ValidationStatus,
    Vote,
)
from elections.reconciliation import reconcile_single_member, single_member_ready_for_api
from elections.validation import validate_dataset


def _single_member_dataset(session) -> tuple[Ballot, ResultRecord, list[Candidate]]:
    artifact = SourceArtifact(
        key="single-member-test",
        url="https://example.test/archive",
        sha256="a" * 64,
        size_bytes=1,
        media_type="text/html",
        local_path="single-member.html",
        metadata_json={},
    )
    election = Election(
        slug="duma-2021-single-member-test",
        name="2021 State Duma test",
        election_date=date(2021, 9, 19),
    )
    oik = Geography(type=GeographyType.OIK, name="OIK 77", code="77")
    session.add_all([artifact, election, oik])
    session.flush()
    ballot = Ballot(
        election_id=election.id,
        kind=BallotKind.SINGLE_MEMBER,
        scope_key="77",
        oik_id=oik.id,
        name="Single-member district 77",
    )
    session.add(ballot)
    session.flush()
    candidates = [
        Candidate(
            ballot_id=ballot.id,
            position=1,
            full_name="Candidate One",
            party_affiliation="Party One",
            is_self_nominated=False,
            registration_status="registered",
            source_artifact_id=artifact.id,
            source_record_id="candidate-1",
            raw_json={},
        ),
        Candidate(
            ballot_id=ballot.id,
            position=2,
            full_name="Candidate Two",
            party_affiliation=None,
            is_self_nominated=True,
            registration_status="registered",
            source_artifact_id=artifact.id,
            source_record_id="candidate-2",
            raw_json={},
        ),
    ]
    session.add_all(candidates)
    session.flush()
    result = ResultRecord(
        source_artifact_id=artifact.id,
        source_row_number=10,
        ballot_id=ballot.id,
        region_name="Test Region",
        oik_name="OIK 77",
        tik_name="Central TIK",
        uik_number="101",
        source_url="https://example.test/protocol/101",
        special_type=SpecialType.NONE,
        is_deg=False,
        raw_json={},
        match_status=MatchStatus.MATCHED,
        validation_status=ValidationStatus.NOT_VALIDATED,
    )
    session.add(result)
    session.flush()
    session.add(
        BallotAccounting(
            result_record_id=result.id,
            registered_voters=100,
            ballots_received=100,
            ballots_issued_early=0,
            ballots_issued_at_station=90,
            ballots_issued_outside=10,
            ballots_cancelled=0,
            portable_boxes_ballots=10,
            stationary_boxes_ballots=80,
            invalid_ballots=1,
            valid_ballots=89,
            lost_ballots=0,
            unaccounted_ballots=0,
        )
    )
    session.add_all(
        [
            Vote(result_record_id=result.id, candidate_id=candidates[0].id, votes=50),
            Vote(result_record_id=result.id, candidate_id=candidates[1].id, votes=39),
        ]
    )
    session.add_all(
        [
            PublishedTotal(
                ballot_id=ballot.id,
                level="oik",
                scope_identifier="77",
                metric="votes",
                option_name="Candidate One",
                value=50,
                source_url="https://example.test/official/77",
            ),
            PublishedTotal(
                ballot_id=ballot.id,
                level="oik",
                scope_identifier="77",
                metric="votes",
                option_name="Candidate Two",
                value=39,
                source_url="https://example.test/official/77",
            ),
            PublishedTotal(
                ballot_id=ballot.id,
                level="oik",
                scope_identifier="77",
                metric="winner",
                option_name="Candidate One",
                value=1,
                source_url="https://example.test/official/77",
            ),
        ]
    )
    session.flush()
    return ballot, result, candidates


def test_single_member_reconciliation_reports_complete_district_rollup(session) -> None:
    _, result, _ = _single_member_dataset(session)

    report = validate_dataset(session, expected_single_member_districts=1)

    district = report["single_member"]["districts"][0]
    assert report["single_member"]["ready"] is True
    assert report["single_member"]["districts_ready"] == 1
    assert report["summary"]["errors"] == 0
    assert district["candidate_totals"] == {"Candidate One": 50, "Candidate Two": 39}
    assert district["derived_winners"] == ["Candidate One"]
    assert district["official_winners"] == ["Candidate One"]
    assert district["totals_by_special_type"]["none"]["valid_ballots"] == 89
    assert district["tik_rollups"][0]["candidate_votes"] == {
        "Candidate One": 50,
        "Candidate Two": 39,
    }
    assert result.validation_status == ValidationStatus.VALID


def test_single_member_reconciliation_enumerates_missing_candidate_vote(session) -> None:
    _, result, candidates = _single_member_dataset(session)
    session.execute(delete(Vote).where(Vote.candidate_id == candidates[1].id))

    report = validate_dataset(session, expected_single_member_districts=1)
    findings = {
        item.code: item
        for item in session.scalars(select(ValidationFinding).order_by(ValidationFinding.id))
    }

    assert {
        "candidate_vote_identity",
        "missing_candidate_votes",
        "official_candidate_total_mismatch",
    } <= findings.keys()
    missing = findings["missing_candidate_votes"]
    assert missing.expected_value == 2
    assert missing.actual_value == 1
    assert missing.details_json["district"] == "77"
    assert missing.details_json["uik"] == "101"
    assert missing.details_json["source_url"] == "https://example.test/protocol/101"
    assert report["single_member"]["ready"] is False
    assert result.validation_status == ValidationStatus.INVALID


def test_single_member_reconciliation_enforces_225_district_coverage(session) -> None:
    _single_member_dataset(session)

    reconciliation = reconcile_single_member(session)

    coverage = next(
        finding
        for finding in reconciliation.findings
        if finding.code == "single_member_district_coverage"
    )
    assert coverage.expected == 225
    assert coverage.actual == 1
    assert reconciliation.report["districts_found"] == 1
    assert reconciliation.report["ready"] is False


def test_validation_reports_negative_single_member_accounting_values(session) -> None:
    _, result, _ = _single_member_dataset(session)
    assert result.accounting is not None
    result.accounting.ballots_cancelled = -1

    validate_dataset(session, expected_single_member_districts=1)

    finding = session.scalar(
        select(ValidationFinding).where(ValidationFinding.code == "negative_accounting_value")
    )
    assert finding is not None
    assert finding.expected_value == 0
    assert finding.actual_value == -1
    assert finding.details_json["metric"] == "ballots_cancelled"


def test_single_member_api_gate_requires_validation_and_official_references(session) -> None:
    _single_member_dataset(session)
    assert single_member_ready_for_api(session, expected_districts=1) is False

    validate_dataset(session, expected_single_member_districts=1)
    assert single_member_ready_for_api(session, expected_districts=1) is True

    session.execute(delete(PublishedTotal).where(PublishedTotal.metric == "winner"))
    assert single_member_ready_for_api(session, expected_districts=1) is False


def test_single_member_official_accounting_and_uik_totals_reconcile(session) -> None:
    ballot, _, _ = _single_member_dataset(session)
    session.add_all(
        [
            PublishedTotal(
                ballot_id=ballot.id,
                level="oik",
                scope_identifier="77",
                metric="valid_ballots",
                option_name="",
                value=88,
                source_url="https://example.test/official/77",
            ),
            PublishedTotal(
                ballot_id=ballot.id,
                level="oik",
                scope_identifier="77",
                metric="uik_count",
                option_name="",
                value=2,
                source_url="https://example.test/official/77",
            ),
        ]
    )

    report = validate_dataset(session, expected_single_member_districts=1)
    findings = list(session.scalars(select(ValidationFinding)))

    assert {"official_accounting_total_mismatch", "official_uik_count_mismatch"} <= {
        finding.code for finding in findings
    }
    assert report["summary"]["published_total_discrepancies"] == 2
    assert report["single_member"]["ready"] is False
