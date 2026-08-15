from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

from conftest import artifact_for
from sqlalchemy import select

from elections.acquisition import acquire_snapshot
from elections.ingest.results import import_results
from elections.ingest.single_member import import_single_member_results
from elections.models import (
    Ballot,
    BallotAccounting,
    BallotKind,
    BallotOption,
    Candidate,
    Election,
    Geography,
    GeographyType,
    MatchStatus,
    PublishedTotal,
    ResultRecord,
    SourceArtifact,
    SpecialType,
    ValidationStatus,
    Vote,
)
from elections.reconciliation import ACCOUNTING_METRICS
from elections.verification import verify_complete_dataset


def _accounting(result_id: int) -> BallotAccounting:
    return BallotAccounting(
        result_record_id=result_id,
        registered_voters=10,
        ballots_received=10,
        ballots_issued_early=0,
        ballots_issued_at_station=10,
        ballots_issued_outside=0,
        ballots_cancelled=0,
        portable_boxes_ballots=0,
        stationary_boxes_ballots=10,
        invalid_ballots=0,
        valid_ballots=10,
        lost_ballots=0,
        unaccounted_ballots=0,
    )


def test_verification_reports_unresolved_state_by_ballot_kind(
    session, results_source, tmp_path: Path
) -> None:
    party_path, party_artifact = results_source
    import_results(session, party_artifact, party_path)
    single_path = tmp_path / "single.json"
    single_path.write_text(
        json.dumps(
            {
                "districts": [
                    {
                        "region_name": "Test Region",
                        "oik_code": "1",
                        "oik_name": "OIK 1",
                        "candidates": [{"position": 1, "full_name": "Candidate A"}],
                        "protocols": [
                            {
                                "tik": "Central TIK",
                                "uik": "UIK 10",
                                "accounting": {
                                    "registered_voters": 10,
                                    "ballots_received": 10,
                                    "ballots_issued_early": 0,
                                    "ballots_issued_at_station": 10,
                                    "ballots_issued_outside": 0,
                                    "ballots_cancelled": 0,
                                    "portable_boxes_ballots": 0,
                                    "stationary_boxes_ballots": 10,
                                    "invalid_ballots": 0,
                                    "valid_ballots": 10,
                                    "lost_ballots": 0,
                                    "unaccounted_ballots": 0,
                                },
                                "votes": {"1": 10},
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    import_single_member_results(
        session,
        artifact_for(single_path, "single-verification", "application/json"),
        single_path,
    )
    for result in session.scalars(select(ResultRecord)):
        result.validation_status = ValidationStatus.VALID
        result.match_status = (
            MatchStatus.SPECIAL if result.special_type != SpecialType.NONE else MatchStatus.MATCHED
        )
    for ballot in session.scalars(select(Ballot)):
        session.add(
            PublishedTotal(
                ballot_id=ballot.id,
                level="national" if ballot.kind == BallotKind.PARTY_LIST else "oik",
                scope_identifier="Russia" if ballot.kind == BallotKind.PARTY_LIST else "1",
                metric="valid_ballots",
                option_name="",
                value=1,
                source_url="https://example.test/official",
            )
        )
    session.flush()

    report_path = tmp_path / "complete-dataset.json"
    report = verify_complete_dataset(session, report_path)

    assert report["ballot_kinds"]["party_list"]["ballots"] == 1
    assert report["ballot_kinds"]["party_list"]["ready"] is False
    assert report["ballot_kinds"]["single_member"]["districts"] == 1
    assert report["ballot_kinds"]["single_member"]["candidates"] == 1
    assert report["ballot_kinds"]["single_member"]["oik_readiness"]["expected"] == 225
    assert report["unresolved"]["import_rejects"] == 1
    assert report["ready"] is False
    written = json.loads(report_path.read_text(encoding="utf-8"))
    assert written["schema_version"] == 1
    assert set(written["source_versions"]) == {"party_list", "single_member", "unassigned"}


def test_verification_requires_all_225_oiks_and_complete_official_references(session) -> None:
    election = Election(
        slug="duma-2021",
        name="State Duma election, eighth convocation",
        election_date=date(2021, 9, 19),
    )
    artifact = SourceArtifact(
        key="complete-two-ballot-fixture",
        url="https://example.test/snapshot",
        sha256="a" * 64,
        retrieved_at=datetime(2021, 9, 28, tzinfo=UTC),
        size_bytes=1,
        media_type="application/json",
        local_path="fixture",
        metadata_json={"snapshot_key": "complete-fixture"},
    )
    session.add_all([election, artifact])
    session.flush()

    party = Ballot(
        election_id=election.id,
        kind=BallotKind.PARTY_LIST,
        scope_key="federal",
        name="Federal party list",
    )
    session.add(party)
    session.flush()
    party_options = [
        BallotOption(ballot_id=party.id, position=position, name=f"Party {position}")
        for position in range(1, 15)
    ]
    session.add_all(party_options)
    party_result = ResultRecord(
        source_artifact_id=artifact.id,
        source_row_number=1,
        ballot_id=party.id,
        region_name="Russia",
        oik_name=None,
        tik_name="National",
        uik_number="1",
        source_url="https://example.test/party/1",
        special_type=SpecialType.NONE,
        is_deg=False,
        raw_json={},
        match_status=MatchStatus.MATCHED,
        validation_status=ValidationStatus.VALID,
    )
    session.add(party_result)
    session.flush()
    session.add(_accounting(party_result.id))
    for option in party_options:
        session.add(Vote(result_record_id=party_result.id, option_id=option.id, votes=0))
        session.add(
            PublishedTotal(
                ballot_id=party.id,
                level="national",
                scope_identifier="Russia",
                metric="votes",
                option_name=option.name,
                value=0,
                source_url="https://example.test/official/party",
            )
        )
    for metric in ACCOUNTING_METRICS:
        session.add(
            PublishedTotal(
                ballot_id=party.id,
                level="national",
                scope_identifier="Russia",
                metric=metric,
                option_name="",
                value=0,
                source_url="https://example.test/official/party",
            )
        )

    for number in range(1, 226):
        geography = Geography(
            type=GeographyType.OIK,
            name=f"OIK {number}",
            code=str(number),
            parent_id=None,
        )
        session.add(geography)
        session.flush()
        ballot = Ballot(
            election_id=election.id,
            kind=BallotKind.SINGLE_MEMBER,
            scope_key=str(number),
            oik_id=geography.id,
            name=f"OIK {number}",
        )
        session.add(ballot)
        session.flush()
        candidate = Candidate(
            ballot_id=ballot.id,
            position=1,
            full_name=f"Candidate {number}",
            source_artifact_id=artifact.id,
            source_record_id=f"candidate-{number}",
            raw_json={},
        )
        result = ResultRecord(
            source_artifact_id=artifact.id,
            source_row_number=number + 1,
            ballot_id=ballot.id,
            region_name="Test Region",
            oik_name=f"OIK {number}",
            tik_name="Central TIK",
            uik_number=str(number),
            source_url=f"https://example.test/oik/{number}",
            special_type=SpecialType.NONE,
            is_deg=False,
            raw_json={},
            match_status=MatchStatus.MATCHED,
            validation_status=ValidationStatus.VALID,
        )
        session.add_all([candidate, result])
        session.flush()
        session.add_all(
            [
                _accounting(result.id),
                Vote(result_record_id=result.id, candidate_id=candidate.id, votes=10),
                PublishedTotal(
                    ballot_id=ballot.id,
                    level="oik",
                    scope_identifier=str(number),
                    metric="candidate_votes",
                    option_name=candidate.full_name,
                    value=10,
                    source_url=f"https://example.test/official/{number}",
                ),
                PublishedTotal(
                    ballot_id=ballot.id,
                    level="oik",
                    scope_identifier=str(number),
                    metric="winner",
                    option_name=candidate.full_name,
                    value=1,
                    source_url=f"https://example.test/official/{number}",
                ),
            ]
        )
    session.flush()

    report = verify_complete_dataset(session)

    assert report["ready"] is True
    assert report["summary"]["ballot_kinds_ready"] == 2
    assert report["summary"]["single_member_oiks_found"] == 225
    assert report["summary"]["single_member_oiks_ready"] == 225
    assert report["ballot_kinds"]["single_member"]["oik_readiness"] == {
        "expected": 225,
        "found": 225,
        "ready": 225,
        "missing_scope_keys": [],
        "unexpected_scope_keys": [],
        "not_ready_scope_keys": [],
    }
    assert report["unresolved"] == {
        "validation_errors": 0,
        "unscoped_validation_errors": 0,
        "import_rejects": 0,
        "unassigned_import_rejects": 0,
        "match_records": 0,
        "source_gaps": 0,
        "blocking_source_gaps": 0,
    }


def test_verification_includes_acquisition_coverage_and_gaps(session, tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    oik = tmp_path / "oik.html"
    index.write_text('<a href="oik.html">ОИК №1</a>', encoding="utf-8")
    oik.write_text("УИК №1", encoding="utf-8")
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_key": "gap-fixture",
                "expected_oik_count": 2,
                "expected_oiks": [
                    {"number": 1, "region_name": "Region"},
                    {"number": 2, "region_name": "Region"},
                ],
                "seeds": [{"url": index.as_uri()}],
                "rate_limit_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    raw_dir = tmp_path / "raw"
    manifest = tmp_path / "snapshot.json"
    acquire_snapshot(plan, raw_dir, manifest)

    report = verify_complete_dataset(
        session,
        snapshot_manifest_path=manifest,
        raw_dir=raw_dir,
    )

    assert report["source_snapshot"]["coverage"]["covered_oiks"] == 1
    assert report["source_snapshot"]["coverage"]["missing_oiks"] == 1
    assert report["source_snapshot"]["blocking_gaps"] == 1
    assert report["unresolved"]["source_gaps"] == 1
    assert report["ready"] is False
