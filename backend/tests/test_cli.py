from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import inspect

import elections.cli as cli
from elections.cli import main
from elections.db import build_engine, session_factory
from elections.models import Ballot, BallotKind, Base, Election


def test_cli_owns_and_closes_database_session(tmp_path: Path, capsys) -> None:
    database = tmp_path / "cli.sqlite3"
    url = f"sqlite:///{database}"
    engine = build_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    audit = tmp_path / "audit.json"

    assert main(["--database-url", url, "match", "--audit", str(audit)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["matched"] == 0
    assert audit.exists()


def test_default_database_is_independent_of_working_directory(
    tmp_path: Path, monkeypatch
) -> None:
    database = tmp_path / "persistent" / "elections.sqlite3"
    database.parent.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setattr(cli, "DEFAULT_LOCAL_DATABASE_URL", f"sqlite:///{database}")

    assert main(["migrate"]) == 0

    engine = build_engine(f"sqlite:///{database}")
    assert "result_records" in inspect(engine).get_table_names()
    engine.dispose()


def test_project_root_honors_container_configuration(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path / "mounted-project"
    monkeypatch.setenv("ELECTIONS_PROJECT_ROOT", str(project_root))

    assert cli.resolve_project_root() == project_root.resolve()


def test_verify_complete_writes_report_and_returns_not_ready(
    tmp_path: Path, capsys
) -> None:
    database = tmp_path / "verify.sqlite3"
    url = f"sqlite:///{database}"
    engine = build_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()
    report_path = tmp_path / "complete.json"

    assert (
        main(
            [
                "--database-url",
                url,
                "verify-complete",
                "--report",
                str(report_path),
                "--snapshot-manifest",
                str(tmp_path / "missing-snapshot.json"),
                "--raw-dir",
                str(tmp_path / "raw"),
            ]
        )
        == 2
    )
    output = json.loads(capsys.readouterr().out)
    assert output["ready"] is False
    assert output["source_snapshot"]["present"] is False
    assert output["summary"]["ballot_kinds_expected"] == 2
    assert json.loads(report_path.read_text(encoding="utf-8")) == output


def test_import_single_member_requires_acquisition_manifest(tmp_path: Path) -> None:
    database = tmp_path / "import.sqlite3"
    url = f"sqlite:///{database}"
    engine = build_engine(url)
    Base.metadata.create_all(engine)
    engine.dispose()

    with pytest.raises(SystemExit, match="acquire-single-member"):
        main(
            [
                "--database-url",
                url,
                "import-single-member",
                "--manifest",
                str(tmp_path / "missing.json"),
            ]
        )


def test_validate_requires_explicit_local_single_member_reference(tmp_path: Path) -> None:
    database = tmp_path / "validate.sqlite3"
    url = f"sqlite:///{database}"
    engine = build_engine(url)
    Base.metadata.create_all(engine)
    with session_factory(engine).begin() as session:
        election = Election(
            slug="duma-2021",
            name="State Duma election, eighth convocation",
            election_date=date(2021, 9, 19),
        )
        session.add(election)
        session.flush()
        session.add(
            Ballot(
                election_id=election.id,
                kind=BallotKind.PARTY_LIST,
                scope_key="federal",
                name="Federal party list",
            )
        )
    engine.dispose()

    with pytest.raises(SystemExit, match="single-member published totals file"):
        main(
            [
                "--database-url",
                url,
                "validate",
                "--published-totals",
                str(Path(__file__).parents[2] / "data/published-totals-2021.json"),
                "--single-member-published-totals",
                str(tmp_path / "official-single-member.json"),
            ]
        )
