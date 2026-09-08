"""Extract the authoritative 2026 State Duma UIK-to-TIK backbone.

The generated ``proper-data/2026/uiks`` shards contain assignments for every
election held on the same day.  This module deliberately selects one election
UUID and refuses ambiguous classifier paths, so downstream contact matching
cannot accidentally treat a municipal district as a territorial commission.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from .io import write_jsonl
from .models import BackboneRow, SourceEvidence, canonical_region_code

DEFAULT_DUMA_ELECTION_ID = "2b72bb97-c625-4a02-a76b-b5740c4d5f6a"
ABROAD_SUBJECT_NAME = "Территория за пределами РФ"


class BackboneDataError(ValueError):
    """Raised when the official classifier data violates a backbone invariant."""


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise BackboneDataError(f"cannot read JSON from {path}: {error}") from error


def _subject_entries(value: Any) -> Iterable[tuple[object, object]]:
    """Yield code/name pairs from common reference and coverage JSON shapes."""

    if isinstance(value, Mapping):
        nested = value.get("regions") or value.get("subjects")
        if isinstance(nested, list):
            yield from _subject_entries(nested)
            return

        # A plain {"77": "город Москва"} reference is convenient for
        # hand-maintained overrides.  Do not mistake an individual record for it.
        record_keys = {
            "regionCode",
            "region_code",
            "subjectCode",
            "subject_code",
            "regionName",
            "region_name",
            "subjectName",
            "subject_name",
            "code",
            "name",
        }
        if not record_keys.intersection(value):
            for code, name in value.items():
                if isinstance(name, str):
                    yield code, name
            return

        code = next(
            (
                value[key]
                for key in ("regionCode", "region_code", "subjectCode", "subject_code", "code")
                if value.get(key) not in (None, "")
            ),
            None,
        )
        name = next(
            (
                value[key]
                for key in (
                    "regionName",
                    "region_name",
                    "subjectName",
                    "subject_name",
                    "name",
                )
                if value.get(key) not in (None, "")
            ),
            None,
        )
        if code is not None and name is not None:
            yield code, name
        return

    if isinstance(value, list):
        for item in value:
            if isinstance(item, Mapping):
                yield from _subject_entries(item)


def _read_subject_reference(path: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    for raw_code, raw_name in _subject_entries(_read_json(path)):
        code = canonical_region_code(raw_code)
        name = str(raw_name).strip()
        if code and name:
            names[code] = name
    return names


def _read_election_subjects(elections_dir: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    if not elections_dir.is_dir():
        return names
    for path in sorted(elections_dir.glob("region-*.json"), key=_shard_sort_key):
        value = _read_json(path)
        if not isinstance(value, list):
            raise BackboneDataError(f"{path}: expected a JSON array")
        for election in value:
            if not isinstance(election, Mapping):
                continue
            code = canonical_region_code(election.get("regionCode"))
            name = str(election.get("region") or "").strip()
            if code and name and code != "0":
                names.setdefault(code, name)
    return names


def resolve_subject_names(
    uiks_dir: Path,
    *,
    reference_regions_path: Path | None = None,
    elections_dir: Path | None = None,
    baseline_coverage_path: Path | None = None,
) -> dict[str, str]:
    """Resolve subject labels, preferring an explicit reference when supplied.

    Current election shards are the first automatic fallback.  The 2024
    presidential coverage catalog fills regions with no current non-federal
    campaign.  Explicit reference values win over both sources.
    """

    uiks_dir = Path(uiks_dir)
    if elections_dir is None:
        elections_dir = uiks_dir.parent / "elections"
    if baseline_coverage_path is None:
        baseline_coverage_path = uiks_dir.parent.parent / "2024-president" / "coverage.json"

    names: dict[str, str] = {}
    if Path(baseline_coverage_path).is_file():
        names.update(_read_subject_reference(Path(baseline_coverage_path)))
    names.update(_read_election_subjects(Path(elections_dir)))
    if reference_regions_path is not None:
        names.update(_read_subject_reference(Path(reference_regions_path)))
    names["0"] = ABROAD_SUBJECT_NAME
    return names


def _shard_sort_key(path: Path) -> tuple[int, int | str]:
    suffix = path.stem.removeprefix("region-")
    if suffix.isdigit():
        return (0, int(suffix))
    return (1, suffix)


def _row_sort_key(row: BackboneRow) -> tuple[int, int | str, int, str]:
    code = row.subject_code
    region_key: tuple[int, int | str] = (0, int(code)) if code.isdigit() else (1, code)
    return (*region_key, row.uik_number, row.uik_classifier_id)


def _compact_territorial_status(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        return value.strip()
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _source_evidence(value: Any) -> SourceEvidence:
    source = value if isinstance(value, Mapping) else {}
    status = source.get("status")
    try:
        status_number = int(status or 0)
    except (TypeError, ValueError) as error:
        raise BackboneDataError(f"invalid source status {status!r}") from error
    return SourceEvidence(
        url=str(source.get("url") or ""),
        retrieved_at=str(source.get("retrievedAt") or source.get("retrieved_at") or ""),
        sha256=str(source.get("sha256") or ""),
        status=status_number,
        source_type=str(
            source.get("sourceType")
            or source.get("source_type")
            or source.get("provenance")
            or "unknown"
        ),
    )


def _positive_integer(value: Any, *, field: str, context: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        raise BackboneDataError(f"{context}: {field} must be an integer") from error
    if number <= 0:
        raise BackboneDataError(f"{context}: {field} must be positive")
    return number


def extract_backbone(
    uiks_dir: Path,
    *,
    reference_regions_path: Path | None = None,
    elections_dir: Path | None = None,
    baseline_coverage_path: Path | None = None,
    election_id: str = DEFAULT_DUMA_ELECTION_ID,
) -> list[BackboneRow]:
    """Load UIK shards and return one deterministic row per Duma UIK."""

    uiks_dir = Path(uiks_dir)
    subject_names = resolve_subject_names(
        uiks_dir,
        reference_regions_path=reference_regions_path,
        elections_dir=elections_dir,
        baseline_coverage_path=baseline_coverage_path,
    )
    rows: list[BackboneRow] = []
    identities: set[tuple[str, int]] = set()
    shard_paths = sorted(uiks_dir.glob("region-*.json"), key=_shard_sort_key)
    if not shard_paths:
        raise BackboneDataError(f"{uiks_dir}: no region-*.json shards found")

    for shard_path in shard_paths:
        shard = _read_json(shard_path)
        if not isinstance(shard, list):
            raise BackboneDataError(f"{shard_path}: expected a JSON array")
        for record_index, record in enumerate(shard):
            if not isinstance(record, Mapping):
                raise BackboneDataError(f"{shard_path}:{record_index}: expected an object")
            assignments = record.get("assignments")
            if not isinstance(assignments, list):
                raise BackboneDataError(
                    f"{shard_path}:{record_index}: assignments must be an array"
                )
            matching = [
                item
                for item in assignments
                if isinstance(item, Mapping) and str(item.get("electionId") or "") == election_id
            ]
            if not matching:
                continue
            context = f"{shard_path}:{record_index} ({record.get('uikKey', 'unknown UIK')})"
            if len(matching) != 1:
                raise BackboneDataError(
                    f"{context}: expected exactly one assignment for election {election_id}"
                )
            assignment = matching[0]
            paths = assignment.get("officialClassifierPaths")
            if not isinstance(paths, list) or len(paths) != 1:
                count = len(paths) if isinstance(paths, list) else "non-array"
                raise BackboneDataError(
                    f"{context}: expected exactly one official classifier path, got {count}"
                )
            path = paths[0]
            if not isinstance(path, Mapping):
                raise BackboneDataError(f"{context}: classifier path must be an object")
            territorial = path.get("territorial")
            if not isinstance(territorial, Mapping):
                raise BackboneDataError(f"{context}: classifier path has no territorial record")
            # Generated 2026 paths omit ``type`` because the generator already selected
            # a type-4 node.  If a future schema carries it, validate it explicitly.
            territorial_type = territorial.get("type", 4)
            try:
                territorial_type_number = int(territorial_type)
            except (TypeError, ValueError) as error:
                raise BackboneDataError(
                    f"{context}: territorial record has invalid type {territorial_type!r}"
                ) from error
            if territorial_type_number != 4:
                raise BackboneDataError(
                    f"{context}: classifier territorial record must have type 4"
                )

            subject_code = canonical_region_code(record.get("regionCode"))
            uik_number = _positive_integer(
                record.get("uikNumber"), field="uikNumber", context=context
            )
            identity = (subject_code, uik_number)
            if identity in identities:
                raise BackboneDataError(
                    f"{context}: duplicate UIK identity {subject_code}:{uik_number}"
                )
            identities.add(identity)

            tik_classifier_id = str(territorial.get("classifierId") or "").strip()
            uik_classifier_id = str(path.get("uikClassifierId") or "").strip()
            if not tik_classifier_id or not uik_classifier_id:
                raise BackboneDataError(f"{context}: classifier IDs must be present")
            rows.append(
                BackboneRow(
                    subject_code=subject_code,
                    subject_name=subject_names.get(subject_code, ""),
                    territorial_status=_compact_territorial_status(record.get("territorialStatus")),
                    tik_classifier_id=tik_classifier_id,
                    tik_number=_positive_integer(
                        territorial.get("number"), field="territorial.number", context=context
                    ),
                    tik_name=str(territorial.get("name") or "").strip(),
                    uik_classifier_id=uik_classifier_id,
                    uik_number=uik_number,
                    source=_source_evidence(path.get("source")),
                )
            )

    return sorted(rows, key=_row_sort_key)


def write_backbone_jsonl(path: Path, rows: Iterable[BackboneRow]) -> int:
    """Write backbone rows in canonical order as deterministic JSON Lines."""

    ordered = sorted(rows, key=_row_sort_key)
    return write_jsonl(Path(path), (row.to_dict() for row in ordered))


def coverage_summary(rows: Iterable[BackboneRow]) -> dict[str, Any]:
    """Return compact, JSON-serializable coverage counts for a backbone."""

    values = list(rows)
    subjects = {row.subject_code for row in values}
    missing_subject_names = sorted(
        {row.subject_code for row in values if not row.subject_name},
        key=lambda code: (0, int(code)) if code.isdigit() else (1, code),
    )
    territorial_rows = sum(bool(row.territorial_status) for row in values)
    return {
        "election_id": DEFAULT_DUMA_ELECTION_ID,
        "uik_count": len(values),
        "tik_count": len({row.tik_classifier_id for row in values}),
        "subject_count": len(subjects),
        "subject_names_resolved": len(subjects) - len(missing_subject_names),
        "missing_subject_name_codes": missing_subject_names,
        "rows_with_territorial_status": territorial_rows,
        "rows_with_source_url": sum(bool(row.source.url) for row in values),
        "rows_with_source_hash": sum(bool(row.source.sha256) for row in values),
    }
