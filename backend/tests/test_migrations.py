from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect

from alembic import command


def test_migrations_run_from_zero_and_can_rebuild(tmp_path: Path) -> None:
    backend = Path(__file__).parents[1]
    database = tmp_path / "migrated.sqlite3"
    config = Config(str(backend / "alembic.ini"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")

    command.upgrade(config, "head")
    tables = set(inspect(create_engine(f"sqlite:///{database}")).get_table_names())
    assert {"elections", "result_records", "commissions", "validation_findings"} <= tables

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert "result_records" in inspect(create_engine(f"sqlite:///{database}")).get_table_names()
