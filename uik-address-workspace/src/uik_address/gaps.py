"""Produce a deterministic TIK-level queue for contact-enrichment work."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path

from .assemble import _write_dict_csv

GAP_COLUMNS = (
    "subject_code",
    "subject",
    "tik_number",
    "tik_name",
    "tik_classifier_id",
    "uik_count",
    "uik_voting_address_count",
    "missing_uik_voting_address_count",
    "tik_address_present",
    "tik_phone_present",
    "fallback_present",
    "priority_missing_uiks",
)


def gap_rows(rows: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["tik_classifier_id"])].append(row)

    result: list[dict[str, object]] = []
    for tik_rows in grouped.values():
        first = tik_rows[0]
        addressed = sum(bool(row["uik_voting_address"]) for row in tik_rows)
        missing = len(tik_rows) - addressed
        has_address = bool(first["tik_address"])
        has_phone = bool(first["tik_phone"])
        result.append(
            {
                "subject_code": first["subject_code"],
                "subject": first["subject_name"],
                "tik_number": first["tik_number"],
                "tik_name": first["tik_name"],
                "tik_classifier_id": first["tik_classifier_id"],
                "uik_count": len(tik_rows),
                "uik_voting_address_count": addressed,
                "missing_uik_voting_address_count": missing,
                "tik_address_present": int(has_address),
                "tik_phone_present": int(has_phone),
                "fallback_present": int(has_address or has_phone),
                "priority_missing_uiks": missing if not (has_address or has_phone) else 0,
            }
        )
    return sorted(
        result,
        key=lambda row: (
            -int(row["priority_missing_uiks"]),
            -int(row["missing_uik_voting_address_count"]),
            int(row["subject_code"]) if str(row["subject_code"]).isdigit() else 10_000,
            int(row["tik_number"]),
            str(row["tik_classifier_id"]),
        ),
    )


def write_gaps(path: Path, rows: Iterable[dict[str, object]]) -> int:
    return _write_dict_csv(path, gap_rows(rows), GAP_COLUMNS)
