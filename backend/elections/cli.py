from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import select

from alembic import command
from elections.db import build_engine, session_scope
from elections.ingest.commissions import import_commissions
from elections.ingest.results import import_results
from elections.matching import match_results
from elections.models import Ballot, BallotKind
from elections.sources import download_all, load_manifest
from elections.validation import load_published_totals, validate_dataset

BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent if BACKEND_ROOT.name == "backend" else BACKEND_ROOT
DEFAULT_MANIFEST = PROJECT_ROOT / "data/source-manifest.json"
DEFAULT_RAW_DIR = PROJECT_ROOT / "data/raw"
DEFAULT_PUBLISHED_TOTALS = PROJECT_ROOT / "data/published-totals-2021.json"
DEFAULT_LOCAL_DATABASE_URL = f"sqlite:///{PROJECT_ROOT / 'data/elections.sqlite3'}"


def _artifact(args: argparse.Namespace) -> tuple[Any, Path]:
    artifacts = load_manifest(args.manifest)
    try:
        artifact = artifacts[args.artifact]
    except KeyError as exc:
        raise SystemExit(f"unknown artifact key {args.artifact!r}") from exc
    path = args.path or args.raw_dir / artifact.filename
    if not path.exists():
        raise SystemExit(f"source file does not exist: {path}; run `elections-data download`")
    return artifact, path


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="elections-data")
    parser.add_argument("--database-url", help="SQLAlchemy URL; defaults to DATABASE_URL")
    subparsers = parser.add_subparsers(dest="command", required=True)

    migrate = subparsers.add_parser("migrate", help="migrate the database to Alembic head")
    migrate.add_argument("--config", type=Path, default=BACKEND_ROOT / "alembic.ini")

    download = subparsers.add_parser("download", help="download and verify every source")
    download.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    download.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    download.add_argument("--force", action="store_true")

    for name, default_artifact in (
        ("import-results", "duma-2021-party-list-results"),
        ("import-commissions", "commissions-2021-09-14"),
    ):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        subparser.add_argument("--artifact", default=default_artifact)
        subparser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
        subparser.add_argument("--path", type=Path)

    match = subparsers.add_parser("match", help="match results to commissions")
    match.add_argument(
        "--audit", type=Path, default=PROJECT_ROOT / "reports/generated/matches.json"
    )

    validate = subparsers.add_parser("validate", help="validate and reconcile imported data")
    validate.add_argument(
        "--report", type=Path, default=PROJECT_ROOT / "reports/generated/validation.json"
    )
    validate.add_argument("--published-totals", type=Path, default=DEFAULT_PUBLISHED_TOTALS)
    validate.add_argument("--ballot-id", type=int, help="ballot for --published-totals")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "download":
        _json(
            {
                key: str(path)
                for key, path in download_all(args.manifest, args.raw_dir, force=args.force).items()
            }
        )
        return 0
    if args.command == "migrate":
        config = Config(str(args.config))
        config.set_main_option(
            "sqlalchemy.url",
            args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DATABASE_URL,
        )
        command.upgrade(config, "head")
        return 0

    engine = build_engine(
        args.database_url or os.environ.get("DATABASE_URL") or DEFAULT_LOCAL_DATABASE_URL
    )
    with session_scope(engine) as session:
        if args.command == "import-results":
            artifact, path = _artifact(args)
            _json(import_results(session, artifact, path))
        elif args.command == "import-commissions":
            artifact, path = _artifact(args)
            _json(import_commissions(session, artifact, path))
        elif args.command == "match":
            _json(match_results(session, args.audit))
        elif args.command == "validate":
            if args.published_totals:
                if args.ballot_id is None:
                    args.ballot_id = session.scalar(
                        select(Ballot.id).where(Ballot.kind == BallotKind.PARTY_LIST)
                    )
                if args.ballot_id is None:
                    raise SystemExit("no party-list ballot exists for published totals")
                if not args.published_totals.exists():
                    raise SystemExit(
                        f"published totals file does not exist: {args.published_totals}"
                    )
                load_published_totals(session, args.published_totals, args.ballot_id)
            report = validate_dataset(session, args.report)
            _json(report["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
