from __future__ import annotations

from datetime import date
from io import StringIO
from pathlib import Path

from alembic.config import Config
from sqlalchemy import UniqueConstraint, create_engine, inspect, select, text
from sqlalchemy.orm import Session

from alembic import command
from elections.models import (
    Ballot,
    BallotKind,
    Base,
    Candidate,
    Election,
    Geography,
    GeographyType,
)


def migration_config(database_url: str, *, output_buffer: StringIO | None = None) -> Config:
    backend = Path(__file__).parents[1]
    config = Config(str(backend / "alembic.ini"), output_buffer=output_buffer)
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_migrations_run_from_zero_and_can_rebuild(tmp_path: Path) -> None:
    database = tmp_path / "migrated.sqlite3"
    config = migration_config(f"sqlite:///{database}")

    command.upgrade(config, "head")
    tables = set(inspect(create_engine(f"sqlite:///{database}")).get_table_names())
    assert {
        "elections",
        "result_records",
        "commissions",
        "validation_findings",
        "candidates",
    } <= tables

    command.downgrade(config, "base")
    command.upgrade(config, "head")
    assert "result_records" in inspect(create_engine(f"sqlite:///{database}")).get_table_names()


def test_migrated_schema_allows_positions_to_restart_in_each_district(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'districts.sqlite3'}"
    command.upgrade(migration_config(database_url), "head")

    with Session(create_engine(database_url)) as session, session.begin():
        election = Election(
            slug="duma-2021", name="State Duma election", election_date=date(2021, 9, 19)
        )
        oik_1 = Geography(type=GeographyType.OIK, code="001", name="District 1")
        oik_2 = Geography(type=GeographyType.OIK, code="002", name="District 2")
        ballot_1 = Ballot(
            election=election,
            kind=BallotKind.SINGLE_MEMBER,
            scope_key=oik_1.code,
            oik=oik_1,
            name="District 1 ballot",
        )
        ballot_2 = Ballot(
            election=election,
            kind=BallotKind.SINGLE_MEMBER,
            scope_key=oik_2.code,
            oik=oik_2,
            name="District 2 ballot",
        )
        session.add_all(
            [
                Candidate(
                    ballot=ballot_1,
                    position=1,
                    full_name="Candidate One",
                    party_affiliation="Example Party",
                    registration_status="registered",
                ),
                Candidate(
                    ballot=ballot_2,
                    position=1,
                    full_name="Candidate Two",
                    is_self_nominated=True,
                    registration_status="registered",
                ),
            ]
        )

    with Session(create_engine(database_url)) as session:
        candidates = session.scalars(select(Candidate).order_by(Candidate.full_name)).all()
        assert [(candidate.ballot.scope_key, candidate.position) for candidate in candidates] == [
            ("001", 1),
            ("002", 1),
        ]


def test_migration_preserves_existing_party_list_ballots_and_votes(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'party-list.sqlite3'}"
    config = migration_config(database_url)
    command.upgrade(config, "6c212c0249e0")
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO elections (id, slug, name, election_date) "
                "VALUES (1, 'duma-2021', 'State Duma election', '2021-09-19')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ballots (id, election_id, kind, name) "
                "VALUES (1, 1, 'PARTY_LIST', 'Federal party-list ballot')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ballot_options (id, ballot_id, position, name) "
                "VALUES (1, 1, 1, 'Example Party')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO source_artifacts "
                "(id, key, url, sha256, metadata_json) "
                "VALUES (1, 'results', 'https://example.test/results', "
                "'0000000000000000000000000000000000000000000000000000000000000000', '{}')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO result_records "
                "(id, source_artifact_id, source_row_number, ballot_id, region_name, "
                "special_type, is_deg, raw_json, match_status, validation_status) "
                "VALUES (1, 1, 1, 1, 'Test Region', 'NONE', 0, '{}', "
                "'PENDING', 'NOT_VALIDATED')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO votes (id, result_record_id, option_id, votes) "
                "VALUES (1, 1, 1, 42)"
            )
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        ballot = connection.execute(
            text("SELECT kind, scope_key, oik_id FROM ballots WHERE id = 1")
        ).one()
        vote = connection.execute(
            text("SELECT option_id, candidate_id, votes FROM votes WHERE id = 1")
        ).one()
    assert ballot == ("PARTY_LIST", "federal", None)
    assert vote == (1, None, 42)


def test_mysql_migration_sql_contains_district_candidate_and_vote_ddl() -> None:
    output = StringIO()
    config = migration_config(
        "mysql+pymysql://user:password@localhost/elections", output_buffer=output
    )

    command.upgrade(config, "head", sql=True)

    sql = output.getvalue()
    assert "CREATE TABLE candidates" in sql
    assert "uq_ballot_election_kind_scope" in sql
    assert "uq_ballot_election_kind_oik" in sql
    assert sql.index("ADD CONSTRAINT uq_ballot_election_kind_scope") < sql.index(
        "DROP INDEX uq_ballot_election_kind"
    )
    assert "ADD COLUMN candidate_id INTEGER" in sql
    assert "ck_vote_exactly_one_target" in sql


def test_all_indexed_keys_fit_mysql_utf8mb4_limit() -> None:
    # MySQL InnoDB allows 3072 bytes per index. utf8mb4 VARCHAR columns can
    # consume four bytes per declared character. Eight bytes for fixed-size
    # columns is conservative for the integer and boolean keys in this schema.
    oversized: list[tuple[str, int]] = []
    for table in Base.metadata.tables.values():
        keys = list(table.indexes)
        keys.extend(
            item for item in table.constraints if isinstance(item, UniqueConstraint)
        )
        for key in keys:
            key_bytes = sum(
                (length * 4 if (length := getattr(column.type, "length", None)) else 8)
                for column in key.columns
            )
            if key_bytes > 3072:
                oversized.append((key.name or "<unnamed>", key_bytes))

    assert oversized == []
