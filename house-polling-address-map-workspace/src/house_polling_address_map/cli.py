"""Operator commands for building and auditing the polling-address map."""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from .cec import CecLegacyTree, CecLookup
from .cec2026 import Cec2026Probe, Cec2026Protocol, probe_result_json
from .gar import GarBuilding, iter_gar_buildings
from .legacy import LegacyTreeCrawler
from .models import AddressIdentity, BuildingAddress
from .resolver import resolve_pending
from .store import PollingMapStore


def _dotenv_value(name: str) -> str | None:
    candidates = (
        Path.cwd() / ".env",
        Path.cwd().parent / ".env",
        Path(__file__).resolve().parents[3] / ".env",
    )
    for path in dict.fromkeys(candidates):
        if not path.is_file():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == name:
                return value.strip().strip("'\"") or None
    return None


def _proxy_url(explicit: str | None) -> str | None:
    return explicit or os.environ.get("PROPER_DATA_PROXY_URL") or _dotenv_value(
        "PROPER_DATA_PROXY_URL"
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="house-polling-map")
    parser.add_argument("--database", type=Path, default=Path("data/polling-map.sqlite3"))
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("init", help="create the mapping database and raw store")

    import_gar = commands.add_parser(
        "import-gar", help="stream canonical building addresses from a GAR XML export"
    )
    import_gar.add_argument("source", type=Path)
    import_gar.add_argument("--index-database", type=Path)
    import_gar.add_argument(
        "--hierarchy", choices=("administrative", "municipal"), default="administrative"
    )
    import_gar.add_argument("--region", action="append", dest="regions")
    import_gar.add_argument("--batch-size", type=int, default=10_000)

    coverage = commands.add_parser("coverage", help="report latest resolution coverage")
    coverage.add_argument("--region")

    probe = commands.add_parser(
        "probe-cec", help="run one captured CEC request and preserve its raw response"
    )
    probe.add_argument("protocol", type=Path)
    probe.add_argument("address")

    probe_2026 = commands.add_parser(
        "probe-cec-2026",
        help="probe the live 2026 CEC address and election-classifier gateway",
    )
    probe_2026.add_argument("protocol", type=Path)
    probe_2026.add_argument("address")
    probe_2026.add_argument("--address-id")
    probe_2026.add_argument("--proxy-url")
    probe_2026.add_argument("--timeout", type=float, default=30.0)

    resolve = commands.add_parser(
        "resolve-gar", help="resolve pending GAR buildings through a captured CEC protocol"
    )
    resolve.add_argument("protocol", type=Path)
    resolve.add_argument("--region")
    resolve.add_argument("--limit", type=int)
    resolve.add_argument("--delay", type=float, default=0.5)

    legacy = commands.add_parser(
        "crawl-legacy-tree", help="resume an exhaustive crawl of the 2015–2018 CEC tree"
    )
    legacy.add_argument("--base-url", default="http://www.cikrf.ru")
    legacy.add_argument("--crawl-id", required=True)
    legacy.add_argument("--state-database", type=Path)
    legacy.add_argument("--request-limit", type=int)
    legacy.add_argument("--delay", type=float, default=0.5)

    export = commands.add_parser("export-jsonl", help="stream latest resolved mappings as JSONL")
    export.add_argument("output", type=Path)
    export.add_argument("--region")

    return parser


def _building(value: GarBuilding) -> BuildingAddress:
    house_parts = [
        part
        for part in (
            f"{value.house_type or 'д'} {value.house_number}" if value.house_number else None,
            f"корп {value.corpus}" if value.corpus else None,
            f"стр {value.structure}" if value.structure else None,
        )
        if part
    ]
    return BuildingAddress(
        identity=AddressIdentity.from_key(value.gar_id),
        region_code=value.region_code or "unknown",
        formatted_address=value.address,
        house=", ".join(house_parts) or None,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    with PollingMapStore(args.database, args.raw_dir) as store:
        if args.command == "init":
            print(json.dumps({"database": str(args.database), "raw_dir": str(args.raw_dir)}))
            return 0
        if args.command == "import-gar":
            values = iter_gar_buildings(
                args.source,
                database=args.index_database,
                hierarchy=args.hierarchy,
                region_codes=args.regions,
            )
            count = store.upsert_buildings(
                (_building(value) for value in values), batch_size=args.batch_size
            )
            print(json.dumps({"imported_buildings": count}, ensure_ascii=False))
            return 0
        if args.command == "coverage":
            print(
                json.dumps(asdict(store.coverage(args.region)), ensure_ascii=False, sort_keys=True)
            )
            return 0
        if args.command == "probe-cec":
            response = CecLookup.from_protocol_file(str(args.protocol)).suggest(args.address)
            digest = store.put_raw_response(response.body, media_type="application/json")
            print(
                json.dumps(
                    {
                        "raw_sha256": digest,
                        "request_url": response.url,
                        "status": response.status,
                        "suggestions": [
                            {
                                "address_id": item.address_id,
                                "commission_id": item.commission_id,
                                "label": item.label,
                                "region_code": item.region_code,
                            }
                            for item in response.suggestions
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "probe-cec-2026":
            result = Cec2026Probe(
                Cec2026Protocol.from_file(args.protocol),
                store,
                proxy_url=_proxy_url(args.proxy_url),
                timeout=args.timeout,
            ).probe(args.address, address_id=args.address_id)
            print(json.dumps(probe_result_json(result), ensure_ascii=False, indent=2))
            return 0
        if args.command == "resolve-gar":
            counts = resolve_pending(
                store,
                CecLookup.from_protocol_file(str(args.protocol)),
                region_code=args.region,
                limit=args.limit,
                delay_seconds=args.delay,
            )
            print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "crawl-legacy-tree":
            summary = LegacyTreeCrawler(
                CecLegacyTree(args.base_url),
                store,
                crawl_id=args.crawl_id,
                delay_seconds=args.delay,
                state_database=args.state_database,
            ).run(request_limit=args.request_limit)
            print(json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "export-jsonl":
            args.output.parent.mkdir(parents=True, exist_ok=True)
            count = 0
            with args.output.open("w", encoding="utf-8") as destination:
                for mapping in store.iter_latest_mappings(region_code=args.region):
                    payload = asdict(mapping)
                    payload["retrieved_at"] = mapping.retrieved_at.isoformat()
                    destination.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                    destination.write("\n")
                    count += 1
            print(json.dumps({"exported_mappings": count, "output": str(args.output)}))
            return 0
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
