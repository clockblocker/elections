from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import select

from alembic import command
from elections.acquisition import BLOCKING_GAP_STATUSES, acquire_snapshot, verify_snapshot
from elections.db import build_engine, session_scope
from elections.gas_resolution import resolve_gas_ids
from elections.ingest.commissions import import_commissions
from elections.ingest.results import import_results
from elections.ingest.single_member import import_single_member_snapshot
from elections.matching import match_results
from elections.models import Ballot, BallotKind
from elections.sources import download_all, load_manifest
from elections.validation import (
    load_published_totals,
    load_single_member_published_totals,
    validate_dataset,
)
from elections.verification import verify_complete_dataset

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def resolve_project_root() -> Path:
    configured = os.environ.get("ELECTIONS_PROJECT_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    if BACKEND_ROOT.name == "backend":
        return BACKEND_ROOT.parent
    working_directory = Path.cwd()
    if (working_directory / "data" / "source-manifest.json").exists():
        return working_directory
    return BACKEND_ROOT


PROJECT_ROOT = resolve_project_root()
DEFAULT_ALEMBIC_CONFIG = (
    PROJECT_ROOT / "backend" / "alembic.ini"
    if (PROJECT_ROOT / "backend" / "alembic.ini").exists()
    else PROJECT_ROOT / "alembic.ini"
)
DEFAULT_MANIFEST = PROJECT_ROOT / "data/source-manifest.json"
DEFAULT_SINGLE_MEMBER_PLAN = PROJECT_ROOT / "data/single-member-sources-2021.json"
DEFAULT_RAW_DIR = PROJECT_ROOT / "data/raw"
DEFAULT_SINGLE_MEMBER_MANIFEST = PROJECT_ROOT / "reports/generated/single-member-snapshot.json"
DEFAULT_PUBLISHED_TOTALS = PROJECT_ROOT / "data/published-totals-2021.json"
DEFAULT_COMPLETE_REPORT = PROJECT_ROOT / "reports/generated/complete-dataset-2021.json"
DEFAULT_GAS_CACHE = PROJECT_ROOT / "data/raw/gas-id-resolution"
DEFAULT_GAS_REPORT = PROJECT_ROOT / "reports/generated/gas-id-resolution.json"
DEFAULT_GAS_HUMAN_REPORT = PROJECT_ROOT / "reports/generated/gas-id-resolution.md"
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
    migrate.add_argument("--config", type=Path, default=DEFAULT_ALEMBIC_CONFIG)

    download = subparsers.add_parser("download", help="download and verify every source")
    download.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    download.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    download.add_argument("--force", action="store_true")

    acquire = subparsers.add_parser(
        "acquire-single-member",
        help="acquire or verify the preserved 2021 single-member CEC snapshot",
    )
    acquire.add_argument("--plan", type=Path, default=DEFAULT_SINGLE_MEMBER_PLAN)
    acquire.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
    acquire.add_argument("--manifest", type=Path, default=DEFAULT_SINGLE_MEMBER_MANIFEST)
    acquire.add_argument("--force", action="store_true")
    acquire.add_argument("--rate-limit", type=float)
    acquire.add_argument("--timeout", type=float, default=120)
    acquire.add_argument("--verify-only", action="store_true")
    acquire.add_argument(
        "--allow-gaps", action="store_true", help="return success while retaining explicit gaps"
    )

    for name, default_artifact in (
        ("import-results", "duma-2021-party-list-results"),
        ("import-commissions", "commissions-2021-09-14"),
    ):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
        subparser.add_argument("--artifact", default=default_artifact)
        subparser.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
        subparser.add_argument("--path", type=Path)

    import_single = subparsers.add_parser(
        "import-single-member",
        help="verify and import candidate protocols from the preserved snapshot",
    )
    import_single.add_argument("--manifest", type=Path, default=DEFAULT_SINGLE_MEMBER_MANIFEST)
    import_single.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)

    match = subparsers.add_parser("match", help="match results to commissions")
    match.add_argument(
        "--audit", type=Path, default=PROJECT_ROOT / "reports/generated/matches.json"
    )

    resolve_gas = subparsers.add_parser(
        "resolve-gas-ids",
        help="resume exact GAS commission-ID resolution from cached official pages",
    )
    resolve_gas.add_argument("--cache-dir", type=Path, default=DEFAULT_GAS_CACHE)
    resolve_gas.add_argument("--report", type=Path, default=DEFAULT_GAS_REPORT)
    resolve_gas.add_argument("--human-report", type=Path, default=DEFAULT_GAS_HUMAN_REPORT)
    resolve_gas.add_argument("--commission-artifact-key", default="commissions-2021-09-14")
    resolve_gas.add_argument("--timeout", type=float, default=30)
    resolve_gas.add_argument("--retries", type=int, default=3)
    resolve_gas.add_argument("--rate-limit", type=float, default=0.5)
    resolve_gas.add_argument("--concurrency", type=int, default=4)
    resolve_gas.add_argument("--offline", action="store_true")
    resolve_gas.add_argument(
        "--max-parent-urls",
        type=int,
        help="bounded smoke-test mode; resolve only the first N unique parent URLs",
    )

    validate = subparsers.add_parser("validate", help="validate and reconcile imported data")
    validate.add_argument(
        "--report", type=Path, default=PROJECT_ROOT / "reports/generated/validation.json"
    )
    validate.add_argument("--published-totals", type=Path, default=DEFAULT_PUBLISHED_TOTALS)
    validate.add_argument("--single-member-published-totals", type=Path)
    validate.add_argument("--ballot-id", type=int, help="ballot for --published-totals")
    verify = subparsers.add_parser(
        "verify-complete", help="write final two-ballot counts and readiness report"
    )
    verify.add_argument("--report", type=Path, default=DEFAULT_COMPLETE_REPORT)
    verify.add_argument("--snapshot-manifest", type=Path, default=DEFAULT_SINGLE_MEMBER_MANIFEST)
    verify.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW_DIR)
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
    if args.command == "acquire-single-member":
        if args.verify_only:
            _json(
                verify_snapshot(args.manifest, args.raw_dir, require_complete=not args.allow_gaps)
            )
            return 0
        report = acquire_snapshot(
            args.plan,
            args.raw_dir,
            args.manifest,
            force=args.force,
            rate_limit_seconds=args.rate_limit,
            timeout=args.timeout,
        )
        _json(
            {
                "manifest": str(args.manifest),
                "coverage": report["coverage"],
                "gaps": len(report["gaps"]),
                "run": report["run"],
            }
        )
        blocking_gaps = any(gap["status"] in BLOCKING_GAP_STATUSES for gap in report["gaps"])
        return 2 if blocking_gaps and not args.allow_gaps else 0
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
        elif args.command == "import-single-member":
            if not args.manifest.exists():
                raise SystemExit(
                    f"snapshot manifest does not exist: {args.manifest}; "
                    "run `elections-data acquire-single-member`"
                )
            _json(import_single_member_snapshot(session, args.manifest, args.raw_dir))
        elif args.command == "import-commissions":
            artifact, path = _artifact(args)
            _json(import_commissions(session, artifact, path))
        elif args.command == "match":
            _json(match_results(session, args.audit))
        elif args.command == "resolve-gas-ids":
            report = resolve_gas_ids(
                session,
                cache_dir=args.cache_dir,
                report_path=args.report,
                human_report_path=args.human_report,
                commission_artifact_key=args.commission_artifact_key,
                timeout=args.timeout,
                retries=args.retries,
                rate_limit_seconds=args.rate_limit,
                concurrency=args.concurrency,
                offline=args.offline,
                max_parent_urls=args.max_parent_urls,
            )
            _json(report["summary"])
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
            if args.single_member_published_totals:
                if not args.single_member_published_totals.exists():
                    raise SystemExit(
                        "single-member published totals file does not exist: "
                        f"{args.single_member_published_totals}"
                    )
                load_single_member_published_totals(session, args.single_member_published_totals)
            report = validate_dataset(session, args.report)
            _json(report["summary"])
        elif args.command == "verify-complete":
            report = verify_complete_dataset(
                session,
                args.report,
                snapshot_manifest_path=args.snapshot_manifest,
                raw_dir=args.raw_dir,
            )
            _json(report)
            return 0 if report["ready"] else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
