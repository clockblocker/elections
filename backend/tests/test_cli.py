from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import inspect

import elections.cli as cli
from elections.cli import main
from elections.db import build_engine
from elections.models import Base


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
