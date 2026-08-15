from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path

from conftest import artifact_for
from sqlalchemy import func, select

from elections.ingest.results import ACCOUNTING_FIELDS
from elections.ingest.single_member import (
    import_single_member_results,
    import_single_member_snapshot,
)
from elections.models import (
    Ballot,
    BallotKind,
    Candidate,
    ImportReject,
    ResultRecord,
    SourceArtifact,
    Vote,
)

RUSSIAN_LABELS = {
    "registered_voters": "Число избирателей, включенных в список избирателей",
    "ballots_received": "Число избирательных бюллетеней, полученных участковой комиссией",
    "ballots_issued_early": "Число бюллетеней, выданных проголосовавшим досрочно",
    "ballots_issued_at_station": "Число бюллетеней, выданных в помещении для голосования",
    "ballots_issued_outside": "Число бюллетеней, выданных вне помещения для голосования",
    "ballots_cancelled": "Число погашенных избирательных бюллетеней",
    "portable_boxes_ballots": "Число бюллетеней в переносных ящиках",
    "stationary_boxes_ballots": "Число бюллетеней в стационарных ящиках",
    "invalid_ballots": "Число недействительных избирательных бюллетеней",
    "valid_ballots": "Число действительных избирательных бюллетеней",
    "lost_ballots": "Число утраченных избирательных бюллетеней",
    "unaccounted_ballots": "Число бюллетеней, не учтенных при получении",
}


def _protocol(uik: str, votes: dict[str, int]) -> dict[str, object]:
    return {
        "tik_name": "Central TIK",
        "uik_number": uik,
        "source_url": f"https://example.test/{uik}",
        "accounting": {
            "registered_voters": 100,
            "ballots_received": 100,
            "ballots_issued_early": 0,
            "ballots_issued_at_station": 90,
            "ballots_issued_outside": 0,
            "ballots_cancelled": 10,
            "portable_boxes_ballots": 0,
            "stationary_boxes_ballots": 90,
            "invalid_ballots": 0,
            "valid_ballots": 90,
            "lost_ballots": 0,
            "unaccounted_ballots": 0,
        },
        "votes": votes,
    }


def _result_html(uik: str) -> str:
    rows = [f"<tr><th>Показатель</th><th>УИК № {uik}</th></tr>"]
    accounting = {
        "registered_voters": 100,
        "ballots_received": 100,
        "ballots_issued_early": 0,
        "ballots_issued_at_station": 90,
        "ballots_issued_outside": 0,
        "ballots_cancelled": 10,
        "portable_boxes_ballots": 0,
        "stationary_boxes_ballots": 90,
        "invalid_ballots": 0,
        "valid_ballots": 90,
        "lost_ballots": 0,
        "unaccounted_ballots": 0,
    }
    for field in ACCOUNTING_FIELDS:
        rows.append(
            f"<tr><td>{RUSSIAN_LABELS[field]}</td><td>{accounting[field]}</td></tr>"
        )
    rows.extend(
        [
            "<tr><td>1. Candidate A</td><td>60</td></tr>",
            "<tr><td>2. Candidate B</td><td>30</td></tr>",
        ]
    )
    return f"<html><table>{''.join(rows)}</table></html>"


def _payload(
    path: Path,
    raw_dir: Path,
    *,
    key: str,
    tik: str | None,
    status: str,
    oik_number: int | None = 1,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "key": key,
        "oik_number": oik_number,
        "oik_name": "OIK 1" if oik_number else None,
        "region_name": "Region One" if oik_number else None,
        "tik_name": tik,
        "relation": "uik" if oik_number else "index",
        "path": path.relative_to(raw_dir).as_posix(),
        "source_url": f"https://source.example/{key}",
        "final_url": f"https://final.example/{key}",
        "retrieved_at": "2021-09-20T00:00:00Z",
        "sha256": sha256(path.read_bytes()).hexdigest(),
        "size_bytes": path.stat().st_size,
        "media_type": "text/html",
        "status": status,
    }
    return payload


def _manifest(path: Path, payloads: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_key": "single-member-import-test",
                "expected_oiks": [{"number": 1, "region_name": "Region One"}],
                "payloads": payloads,
                "gaps": [
                    {
                        "oik_number": 1,
                        "status": "redirected",
                        "detail": "source redirected; response was preserved",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_imports_district_scoped_candidates_and_is_restartable(session, tmp_path: Path) -> None:
    path = tmp_path / "single-member.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "districts": [
                    {
                        "region_name": "Region One",
                        "oik_code": "1",
                        "oik_name": "OIK 1",
                        "candidates": [
                            {"position": 1, "full_name": "Candidate A", "party": "Party A"},
                            {"position": 2, "full_name": "Candidate B", "self_nominated": True},
                        ],
                        "protocols": [_protocol("1", {"1": 60, "2": 30})],
                    },
                    {
                        "region_name": "Region Two",
                        "oik_code": "2",
                        "oik_name": "OIK 2",
                        "candidates": [
                            {"position": 1, "full_name": "Candidate C", "party": "Party C"},
                            {"position": 2, "full_name": "Candidate D", "party": "Party D"},
                        ],
                        "protocols": [
                            _protocol("2", {"1": 50, "2": 40}),
                            _protocol("3", {"1": 90}),
                        ],
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    artifact = artifact_for(path, "single-member-test", "application/json")

    stats = import_single_member_results(session, artifact, path)

    assert stats == {
        "source_rows": 3,
        "imported_records": 2,
        "rejected_records": 1,
        "districts": 2,
        "tiks": 2,
        "uiks": 2,
        "candidates": 4,
        "vote_rows": 4,
    }
    ballots = list(
        session.scalars(
            select(Ballot).where(Ballot.kind == BallotKind.SINGLE_MEMBER).order_by(Ballot.scope_key)
        )
    )
    assert [ballot.scope_key for ballot in ballots] == ["1", "2"]
    assert [candidate.full_name for candidate in ballots[0].candidates] == [
        "Candidate A",
        "Candidate B",
    ]
    assert [candidate.full_name for candidate in ballots[1].candidates] == [
        "Candidate C",
        "Candidate D",
    ]
    assert session.scalar(select(func.count(Candidate.id))) == 4
    assert session.scalar(select(func.count(ResultRecord.id))) == 2
    assert session.scalar(select(func.count(Vote.id))) == 4
    assert session.scalar(select(func.count(ImportReject.id))) == 1

    assert import_single_member_results(session, artifact, path) == stats
    assert session.scalar(select(func.count(ResultRecord.id))) == 2
    assert session.scalar(select(func.count(Vote.id))) == 4
    assert session.scalar(select(func.count(ImportReject.id))) == 1


def test_snapshot_adapter_imports_redirects_and_reports_every_gap(
    session, tmp_path: Path
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    first = raw_dir / "first.html"
    second = raw_dir / "second.html"
    malformed = raw_dir / "malformed.html"
    index = raw_dir / "index.html"
    unavailable = raw_dir / "unavailable.html"
    first.write_text(_result_html("10"), encoding="utf-8")
    second.write_text(_result_html("11"), encoding="utf-8")
    malformed.write_text("<html>not a protocol table</html>", encoding="utf-8")
    index.write_text("<html>navigation</html>", encoding="utf-8")
    unavailable.write_text("not preserved", encoding="utf-8")
    manifest = tmp_path / "snapshot.json"
    payloads = [
        _payload(first, raw_dir, key="first", tik="TIK A", status="preserved"),
        _payload(
            second,
            raw_dir,
            key="second",
            tik="TIK B",
            status="redirected",
        ),
        _payload(malformed, raw_dir, key="bad", tik="TIK C", status="preserved"),
        _payload(index, raw_dir, key="index", tik=None, status="preserved", oik_number=None),
        _payload(unavailable, raw_dir, key="missing", tik="TIK D", status="unavailable"),
    ]
    _manifest(manifest, payloads)

    stats = import_single_member_snapshot(session, manifest, raw_dir)

    stat_fields = (
        "payloads",
        "preserved_payloads",
        "redirected_payloads",
        "skipped_payloads",
        "source_rows",
        "imported_records",
        "rejected_records",
        "districts",
        "tiks",
        "uiks",
        "candidates",
        "vote_rows",
    )
    assert {key: stats[key] for key in stat_fields} == {
        "payloads": 5,
        "preserved_payloads": 4,
        "redirected_payloads": 1,
        "skipped_payloads": 2,
        "source_rows": 2,
        "imported_records": 2,
        "rejected_records": 1,
        "districts": 1,
        "tiks": 2,
        "uiks": 2,
        "candidates": 2,
        "vote_rows": 4,
    }
    assert [item["key"] for item in stats["parse_errors"]] == ["bad"]
    assert any(item["status"] == "redirected" for item in stats["source_gaps"])
    assert any(item["status"] == "unavailable" for item in stats["source_gaps"])
    assert session.scalar(select(func.count(ResultRecord.id))) == 2
    assert session.scalar(select(func.count(Vote.id))) == 4
    assert session.scalar(select(func.count(ImportReject.id))) == 1
    assert session.scalar(select(func.count(SourceArtifact.id))) == 4
    assert {record.source_url for record in session.scalars(select(ResultRecord))} == {
        "https://final.example/first",
        "https://final.example/second",
    }

    assert import_single_member_snapshot(session, manifest, raw_dir) == stats
    assert session.scalar(select(func.count(ResultRecord.id))) == 2
    assert session.scalar(select(func.count(Vote.id))) == 4
    assert session.scalar(select(func.count(ImportReject.id))) == 1
    assert session.scalar(select(func.count(Candidate.id))) == 2


def test_snapshot_rerun_removes_rows_when_payload_becomes_unparseable(
    session, tmp_path: Path
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    result = raw_dir / "result.html"
    result.write_text(_result_html("10"), encoding="utf-8")
    manifest = tmp_path / "snapshot.json"
    _manifest(
        manifest,
        [_payload(result, raw_dir, key="result", tik="TIK A", status="preserved")],
    )
    assert import_single_member_snapshot(session, manifest, raw_dir)["imported_records"] == 1

    result.write_text("<html>the table disappeared</html>", encoding="utf-8")
    _manifest(
        manifest,
        [_payload(result, raw_dir, key="result", tik="TIK A", status="preserved")],
    )
    stats = import_single_member_snapshot(session, manifest, raw_dir)

    assert stats["imported_records"] == 0
    assert stats["rejected_records"] == 1
    assert session.scalar(select(func.count(ResultRecord.id))) == 0
    assert session.scalar(select(func.count(Vote.id))) == 0
    assert session.scalar(select(func.count(Candidate.id))) == 0
    assert session.scalar(select(func.count(ImportReject.id))) == 1


def test_snapshot_rejects_duplicate_protocol_identity_instead_of_double_counting(
    session, tmp_path: Path
) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    first = raw_dir / "first.html"
    duplicate = raw_dir / "duplicate.html"
    first.write_text(_result_html("10"), encoding="utf-8")
    duplicate.write_text(_result_html("10"), encoding="utf-8")
    manifest = tmp_path / "snapshot.json"
    _manifest(
        manifest,
        [
            _payload(first, raw_dir, key="first", tik="TIK A", status="preserved"),
            _payload(duplicate, raw_dir, key="second", tik="TIK A", status="preserved"),
        ],
    )

    stats = import_single_member_snapshot(session, manifest, raw_dir)

    assert stats["imported_records"] == 1
    assert stats["uiks"] == 1
    assert stats["vote_rows"] == 2
    assert stats["rejected_records"] == 1
    assert "duplicate another payload" in stats["parse_errors"][0]["error"]
    assert session.scalar(select(func.count(ResultRecord.id))) == 1
    assert session.scalar(select(func.count(ImportReject.id))) == 1
