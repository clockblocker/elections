from __future__ import annotations

import csv
import hashlib
import sqlite3
from pathlib import Path

import pytest

from elections.db import build_engine, session_factory
from elections.models import Base
from elections.sources import Artifact

PARTIES = [f"{index}. Party {index}" for index in range(1, 15)]
ACCOUNTING_HEADERS = [f"Accounting {index}" for index in range(1, 13)]


def artifact_for(path: Path, key: str, media_type: str) -> Artifact:
    return Artifact(
        key=key,
        url=path.as_uri(),
        filename=path.name,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        size_bytes=path.stat().st_size,
        media_type=media_type,
        metadata={},
    )


@pytest.fixture
def session(tmp_path: Path):
    engine = build_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    Base.metadata.create_all(engine)
    with session_factory(engine).begin() as database_session:
        yield database_session


@pytest.fixture
def results_source(tmp_path: Path) -> tuple[Path, Artifact]:
    path = tmp_path / "results.csv"
    headers = ["level", "region", "oik", "tik", "uik", *ACCOUNTING_HEADERS, *PARTIES, "url"]
    valid_accounting = [100, 100, 0, 80, 10, 10, 10, 80, 1, 89, 0, 0]
    valid_votes = [50, 20, 10, 9, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    deg_accounting = [10, 10, 0, 10, 0, 0, 0, 10, 0, 10, 0, 0]
    deg_votes = [10, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]
    rejected_accounting = [-1, 1, 0, 1, 0, 0, 0, 1, 0, 1, 0, 0]
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(headers)
        writer.writerow(
            [
                "CEC",
                "Test Region",
                "OIK 1",
                "Central TIK",
                "UIK 1",
                *valid_accounting,
                *valid_votes,
                "https://example.test/1",
            ]
        )
        writer.writerow(
            [
                "CEC",
                "Test Region",
                "OIK 1",
                "ДЭГ",
                "ДЭГ",
                *deg_accounting,
                *deg_votes,
                "https://example.test/deg",
            ]
        )
        writer.writerow(
            [
                "CEC",
                "Test Region",
                "OIK 1",
                "Central TIK",
                "UIK 2",
                *rejected_accounting,
                *valid_votes,
                "https://example.test/2",
            ]
        )
    return path, artifact_for(path, "test-results", "text/csv")


@pytest.fixture
def commission_source(tmp_path: Path) -> tuple[Path, Artifact]:
    path = tmp_path / "commissions.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE cik_uik (
                id INTEGER PRIMARY KEY, iz_id INTEGER, parent_id INTEGER, type_ik TEXT,
                region TEXT, name TEXT, address TEXT, phone TEXT, email TEXT,
                end_date TEXT, address_voteroom TEXT, lat_ik REAL, lon_ik REAL,
                lat_voteroom REAL, lon_voteroom REAL
            );
            CREATE TABLE cik_people (
                id INTEGER PRIMARY KEY, number INTEGER, fio TEXT, post TEXT,
                party TEXT, ik_id INTEGER
            );
            """
        )
        connection.executemany(
            "INSERT INTO cik_uik VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    10,
                    1000,
                    None,
                    "tik",
                    "test",
                    "Central TIK",
                    "Test Region",
                    "secret",
                    "secret@example.test",
                    "14.09.2026",
                    None,
                    None,
                    None,
                    None,
                    None,
                ),
                (
                    11,
                    1001,
                    10,
                    "uik",
                    "test",
                    "UIK 1",
                    "Test Region, One Street",
                    "secret",
                    "secret@example.test",
                    "14.09.2026",
                    None,
                    50.0,
                    10.0,
                    None,
                    None,
                ),
            ],
        )
        connection.execute(
            "INSERT INTO cik_people VALUES (?, ?, ?, ?, ?, ?)",
            (101, 1, "Example Chair", "Chairperson", "Example nominator", 11),
        )
    return path, artifact_for(path, "test-commissions", "application/vnd.sqlite3")
