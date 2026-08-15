from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import func, select

from elections.ingest.commissions import import_commissions
from elections.ingest.results import import_results
from elections.matching import match_results
from elections.models import (
    Commission,
    CommissionMembership,
    ImportReject,
    MatchEvidence,
    MatchStatus,
    ResultRecord,
    ValidationFinding,
    ValidationStatus,
    Vote,
)
from elections.validation import load_published_totals, validate_dataset


def test_end_to_end_import_match_and_validate(
    session, results_source, commission_source, tmp_path: Path
) -> None:
    result_path, result_artifact = results_source
    stats = import_results(session, result_artifact, result_path)
    assert stats == {
        "source_rows": 3,
        "imported_records": 2,
        "rejected_records": 1,
        "vote_rows": 28,
        "options": 14,
    }
    assert session.scalar(select(func.count(ImportReject.id))) == 1
    assert session.scalar(select(func.count(Vote.id))) == 28

    # A rerun replaces, rather than duplicates, source-derived records.
    assert import_results(session, result_artifact, result_path) == stats
    assert session.scalar(select(func.count(ResultRecord.id))) == 2

    commission_path, commission_artifact = commission_source
    commission_stats = import_commissions(session, commission_artifact, commission_path)
    assert commission_stats["uik"] == 1
    assert commission_stats["tik"] == 1
    assert commission_stats["memberships"] == 1
    assert session.scalar(select(func.count(CommissionMembership.id))) == 1
    uik = session.scalar(select(Commission).where(Commission.number == "1"))
    assert uik is not None
    assert "phone" not in uik.raw_json
    assert "email" not in uik.raw_json

    audit = tmp_path / "matches.json"
    counts = match_results(session, audit)
    assert counts[MatchStatus.MATCHED.value] == 1
    assert counts[MatchStatus.SPECIAL.value] == 1
    physical = session.scalar(select(ResultRecord).where(ResultRecord.is_deg.is_(False)))
    assert physical is not None and physical.matched_commission_id == uik.id
    assert json.loads(audit.read_text())["counts"] == counts

    ballot_id = physical.ballot_id
    totals = tmp_path / "published.json"
    totals.write_text(
        json.dumps(
            {
                "totals": [
                    {
                        "level": "national",
                        "scope_identifier": "Russia",
                        "metric": "registered_voters",
                        "value": 110,
                        "source_url": "https://example.test/official",
                    }
                ]
            }
        )
    )
    assert load_published_totals(session, totals, ballot_id) == 1
    report = validate_dataset(session, tmp_path / "validation.json")
    assert report["summary"]["errors"] == 0
    assert report["summary"]["published_total_discrepancies"] == 0
    assert report["national_by_special_type"]["deg"]["result_records"] == 1
    assert physical.validation_status == ValidationStatus.VALID


def test_validation_enumerates_accounting_discrepancy(session, results_source) -> None:
    path, artifact = results_source
    import_results(session, artifact, path)
    physical = session.scalar(select(ResultRecord).where(ResultRecord.is_deg.is_(False)))
    assert physical is not None and physical.accounting is not None
    physical.accounting.valid_ballots = 88
    report = validate_dataset(session)
    assert report["summary"]["errors"] == 2
    codes = set(session.scalars(select(ValidationFinding.code)))
    assert {"ballot_box_identity", "party_vote_identity"} <= codes
    assert physical.validation_status == ValidationStatus.INVALID


def test_matching_bulk_path_preserves_explicit_ambiguity(
    session, results_source, commission_source, tmp_path: Path
) -> None:
    result_path, result_artifact = results_source
    commission_path, commission_artifact = commission_source
    import_results(session, result_artifact, result_path)
    import_commissions(session, commission_artifact, commission_path)
    original = session.scalar(select(Commission).where(Commission.number == "1"))
    assert original is not None
    session.add(
        Commission(
            source_artifact_id=original.source_artifact_id,
            source_record_id="duplicate-uik-1",
            gas_vybory_id="duplicate-gas-1",
            parent_id=original.parent_id,
            parent_source_id=original.parent_source_id,
            type=original.type,
            region=original.region,
            name=original.name,
            number=original.number,
            address=original.address,
            raw_json={},
        )
    )
    session.flush()

    audit = tmp_path / "ambiguous-matches.json"
    counts = match_results(session, audit)
    physical = session.scalar(select(ResultRecord).where(ResultRecord.is_deg.is_(False)))
    evidence = list(
        session.scalars(
            select(MatchEvidence)
            .where(MatchEvidence.result_record_id == physical.id)
            .order_by(MatchEvidence.commission_id)
        )
    )

    assert physical.match_status == MatchStatus.AMBIGUOUS
    assert physical.matched_commission_id is None
    assert len(evidence) == 2
    assert all(not item.selected for item in evidence)
    assert counts[MatchStatus.AMBIGUOUS.value] == 1
    report = json.loads(audit.read_text())
    assert report["unresolved"][0]["candidate_commission_ids"] == [
        item.commission_id for item in evidence
    ]
