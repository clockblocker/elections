"""Join the official 2026 UIK/TIK backbone to independently crawled contacts."""

from __future__ import annotations

import csv
import json
import os
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import BackboneRow, CommissionContact, canonical_region_code

CSV_COLUMNS = (
    "subject_code",
    "subject_name",
    "territorial_status",
    "tik_name",
    "tik_number",
    "tik_classifier_id",
    "tik_address",
    "tik_phone",
    "tik_match_method",
    "tik_contact_source",
    "tik_contact_source_type",
    "tik_contact_source_sha256",
    "tik_contact_status",
    "tik_contact_retrieved_at",
    "uik_number",
    "uik_classifier_id",
    "uik_commission_address",
    "uik_commission_phone",
    "uik_voting_address",
    "uik_voting_phone",
    "uik_match_method",
    "uik_contact_source",
    "uik_contact_source_type",
    "uik_contact_source_sha256",
    "uik_contact_status",
    "uik_contact_retrieved_at",
    "hierarchy_source",
    "hierarchy_source_type",
    "hierarchy_source_sha256",
    "hierarchy_retrieved_at",
)

PUBLIC_CSV_COLUMNS = (
    "subject",
    "tik_name",
    "tik_number",
    "tik_address",
    "tik_phone",
    "uik_number",
    "uik_voting_address",
    "uik_phone",
    "contact_source",
    "contact_retrieved_at",
    "contact_status",
)


def normalize_commission_type(value: str) -> str:
    text = value.strip().casefold()
    return {"4": "tik", "тик": "tik", "tik": "tik", "5": "uik", "уик": "uik", "uik": "uik"}.get(
        text, text
    )


def normalize_name(value: str) -> str:
    text = value.casefold().replace("ё", "е")
    text = re.sub(r"\b(?:территориальная|участковая)\s+избирательная\s+комиссия\b", "", text)
    text = re.sub(r"\b(?:тик|уик)\b", "", text)
    text = re.sub(r"[^0-9a-zа-я]+", " ", text)
    return " ".join(text.split())


def _contact_score(contact: CommissionContact) -> tuple[int, int, int, str, str]:
    source_type = contact.source.source_type.casefold().replace("_", "-")
    source_priority = {
        "cec-report-42": 50,
        "cec-commission-parents": 40,
        "cec-commission-org": 40,
        "cec-commission-search": 30,
        "regional-official-json": 25,
        "regional-official-csv": 20,
        "regional-official-html": 10,
        "regional-xlsx-2026": 45,
        "regional-docx-2026": 45,
        "regional-json": 25,
        "regional-csv": 20,
        "regional-html": 10,
        "regional-adapter-kemerovo-tik": 35,
    }.get(source_type, 35 if source_type.startswith("regional-adapter-") else 0)
    completeness = sum(
        bool(value.strip())
        for value in (
            contact.commission_address,
            contact.commission_phone,
            contact.voting_address,
            contact.voting_phone,
        )
    )
    # A directory hit with no usable fields must not mask a populated regional
    # record merely because its source class is more authoritative.
    return (
        bool(completeness),
        source_priority,
        completeness,
        contact.source.retrieved_at,
        contact.source.sha256,
    )


def _contact_values(contact: CommissionContact) -> tuple[str, str, str, str]:
    return (
        contact.commission_address.strip(),
        contact.commission_phone.strip(),
        contact.voting_address.strip(),
        contact.voting_phone.strip(),
    )


def _normalized_contact_values(contact: CommissionContact) -> tuple[str, str, str, str]:
    return tuple(" ".join(value.casefold().split()) for value in _contact_values(contact))


def _contact_conflict(candidates: Sequence[CommissionContact]) -> bool:
    """Report contradictory populated fields, not compatible partial records."""

    values = [_normalized_contact_values(candidate) for candidate in candidates]
    return any(len({row[index] for row in values if row[index]}) > 1 for index in range(4))


def _contact_tie_breaker(contact: CommissionContact) -> tuple[str, ...]:
    """Make selection independent of crawler and input iteration order."""

    return (
        contact.external_id,
        normalize_name(contact.commission_name),
        *_normalized_contact_values(contact),
        contact.source.url,
    )


def _unverified_identity(contact: CommissionContact) -> tuple[str, ...]:
    """Identify a no-ID source record without merging distinct rows on one page."""

    if contact.external_id:
        return ("external_id", contact.external_id)
    return (
        "source_record",
        contact.source.url,
        contact.source.sha256,
        str(contact.commission_number),
        *_normalized_contact_values(contact),
    )


@dataclass(frozen=True, slots=True)
class ContactMatch:
    contact: CommissionContact | None
    method: str
    conflict: bool = False


class ContactIndex:
    def __init__(self, contacts: Iterable[CommissionContact]) -> None:
        self.contacts = tuple(contacts)
        self._number: dict[tuple[str, str, int], list[CommissionContact]] = defaultdict(list)
        self._name: dict[tuple[str, str, str], list[CommissionContact]] = defaultdict(list)
        self._number_matches: dict[tuple[str, str, int], ContactMatch] = {}
        self._name_matches: dict[tuple[str, str, str], ContactMatch] = {}
        for contact in self.contacts:
            region = canonical_region_code(contact.subject_code)
            kind = normalize_commission_type(contact.commission_type)
            if contact.commission_number is not None:
                self._number[(region, kind, contact.commission_number)].append(contact)
            name = normalize_name(contact.commission_name)
            if name:
                self._name[(region, kind, name)].append(contact)

    @staticmethod
    def _select(candidates: Sequence[CommissionContact], method: str) -> ContactMatch:
        if not candidates:
            return ContactMatch(None, "unmatched")
        # The same response hash applies to every record parsed from a source
        # page, so (external_id, sha256) is not a safe record key when a
        # regional source has no external IDs.  Dataclass equality removes only
        # genuinely identical normalized records.
        unique = tuple(dict.fromkeys(candidates))
        ranked = sorted(
            unique,
            key=lambda candidate: (_contact_score(candidate), _contact_tie_breaker(candidate)),
            reverse=True,
        )
        return ContactMatch(ranked[0], method, _contact_conflict(ranked))

    def match_uik(self, row: BackboneRow) -> ContactMatch:
        key = (canonical_region_code(row.subject_code), "uik", row.uik_number)
        if key not in self._number_matches:
            self._number_matches[key] = self._select(
                self._number.get(key, ()), "exact_region_number"
            )
        return self._number_matches[key]

    def match_tik(self, row: BackboneRow) -> ContactMatch:
        region = canonical_region_code(row.subject_code)
        number_key = (region, "tik", row.tik_number)
        by_number = self._number.get(number_key, ())
        if by_number:
            if number_key not in self._number_matches:
                self._number_matches[number_key] = self._select(by_number, "exact_region_number")
            return self._number_matches[number_key]
        name_key = (region, "tik", normalize_name(row.tik_name))
        by_name = self._name.get(name_key, ())
        if len({_unverified_identity(item) for item in by_name}) == 1:
            if name_key not in self._name_matches:
                self._name_matches[name_key] = self._select(by_name, "unique_normalized_name")
            return self._name_matches[name_key]
        # Official sites often publish the same TIK's contact card through both
        # a canonical page and a search-result URL.  Multiple URLs are safe to
        # merge only when the normalized contact fields are identical.
        if by_name and len({_normalized_contact_values(item) for item in by_name}) == 1:
            if name_key not in self._name_matches:
                self._name_matches[name_key] = self._select(
                    by_name, "unique_normalized_name_duplicate_sources"
                )
            return self._name_matches[name_key]
        return ContactMatch(None, "unmatched")


def _row_sort_key(row: BackboneRow) -> tuple[int, int | str, int, str]:
    code = canonical_region_code(row.subject_code)
    region: tuple[int, int | str] = (0, int(code)) if code.isdigit() else (1, code)
    return (*region, row.uik_number, row.uik_classifier_id)


def assemble_rows(
    backbone: Iterable[BackboneRow], contacts: Iterable[CommissionContact]
) -> tuple[list[dict[str, object]], dict[str, object]]:
    rows = sorted(backbone, key=_row_sort_key)
    index = ContactIndex(contacts)
    output: list[dict[str, object]] = []
    counts: Counter[str] = Counter()
    used_contacts: set[int] = set()
    for row in rows:
        tik = index.match_tik(row)
        uik = index.match_uik(row)
        if tik.contact:
            used_contacts.add(id(tik.contact))
        if uik.contact:
            used_contacts.add(id(uik.contact))
        counts["rows"] += 1
        counts["tik_matched"] += bool(tik.contact)
        counts["tik_address"] += bool(tik.contact and tik.contact.commission_address.strip())
        counts["tik_phone"] += bool(tik.contact and tik.contact.commission_phone.strip())
        counts["uik_matched"] += bool(uik.contact)
        counts["uik_voting_address"] += bool(uik.contact and uik.contact.voting_address.strip())
        counts["uik_commission_address"] += bool(
            uik.contact and uik.contact.commission_address.strip()
        )
        counts["uik_phone"] += bool(
            uik.contact
            and (uik.contact.voting_phone.strip() or uik.contact.commission_phone.strip())
        )
        has_tik_address = bool(tik.contact and tik.contact.commission_address.strip())
        has_tik_phone = bool(tik.contact and tik.contact.commission_phone.strip())
        has_tik_contact = has_tik_address or has_tik_phone
        has_uik_contact = bool(uik.contact and any(_contact_values(uik.contact)))
        has_uik_voting_address = bool(uik.contact and uik.contact.voting_address.strip())
        counts["rows_with_any_contact"] += has_tik_contact or has_uik_contact
        counts["rows_without_uik_voting_address"] += not has_uik_voting_address
        counts["rows_without_uik_voting_address_with_tik_contact"] += (
            not has_uik_voting_address and has_tik_contact
        )
        counts["rows_without_uik_voting_address_with_tik_phone"] += (
            not has_uik_voting_address and has_tik_phone
        )
        counts["rows_without_uik_voting_address_with_tik_address_and_phone"] += (
            not has_uik_voting_address and has_tik_address and has_tik_phone
        )
        counts["rows_with_uik_voting_address_or_tik_contact"] += (
            has_uik_voting_address or has_tik_contact
        )
        counts["rows_without_uik_voting_address_or_tik_contact"] += not (
            has_uik_voting_address or has_tik_contact
        )
        counts["tik_match_conflicts"] += tik.conflict
        counts["uik_match_conflicts"] += uik.conflict
        counts["contact_conflicts"] += tik.conflict + uik.conflict
        output.append(_assembled_row(row, tik, uik))
    count_names = (
        "rows",
        "tik_matched",
        "tik_address",
        "tik_phone",
        "uik_matched",
        "uik_voting_address",
        "uik_commission_address",
        "uik_phone",
        "rows_with_any_contact",
        "rows_without_uik_voting_address",
        "rows_without_uik_voting_address_with_tik_contact",
        "rows_without_uik_voting_address_with_tik_phone",
        "rows_without_uik_voting_address_with_tik_address_and_phone",
        "rows_with_uik_voting_address_or_tik_contact",
        "rows_without_uik_voting_address_or_tik_contact",
        "tik_match_conflicts",
        "uik_match_conflicts",
        "contact_conflicts",
    )
    coverage = {
        **{name: counts[name] for name in count_names},
        "input_contacts": len(index.contacts),
        "used_contacts": len(used_contacts),
        "unused_contacts": len(index.contacts) - len(used_contacts),
        "csv_columns": list(CSV_COLUMNS),
    }
    return output, coverage


def _assembled_row(row: BackboneRow, tik: ContactMatch, uik: ContactMatch) -> dict[str, object]:
    tc = tik.contact
    uc = uik.contact
    return {
        "subject_code": row.subject_code,
        "subject_name": row.subject_name,
        "territorial_status": row.territorial_status,
        "tik_name": row.tik_name,
        "tik_number": row.tik_number,
        "tik_classifier_id": row.tik_classifier_id,
        "tik_address": tc.commission_address if tc else "",
        "tik_phone": tc.commission_phone if tc else "",
        "tik_match_method": tik.method + ("_conflict_selected" if tik.conflict else ""),
        "tik_contact_source": tc.source.url if tc else "",
        "tik_contact_source_type": tc.source.source_type if tc else "",
        "tik_contact_source_sha256": tc.source.sha256 if tc else "",
        "tik_contact_status": tc.source.status if tc else "",
        "tik_contact_retrieved_at": tc.source.retrieved_at if tc else "",
        "uik_number": row.uik_number,
        "uik_classifier_id": row.uik_classifier_id,
        "uik_commission_address": uc.commission_address if uc else "",
        "uik_commission_phone": uc.commission_phone if uc else "",
        "uik_voting_address": uc.voting_address if uc else "",
        "uik_voting_phone": uc.voting_phone if uc else "",
        "uik_match_method": uik.method + ("_conflict_selected" if uik.conflict else ""),
        "uik_contact_source": uc.source.url if uc else "",
        "uik_contact_source_type": uc.source.source_type if uc else "",
        "uik_contact_source_sha256": uc.source.sha256 if uc else "",
        "uik_contact_status": uc.source.status if uc else "",
        "uik_contact_retrieved_at": uc.source.retrieved_at if uc else "",
        "hierarchy_source": row.source.url,
        "hierarchy_source_type": row.source.source_type,
        "hierarchy_source_sha256": row.source.sha256,
        "hierarchy_retrieved_at": row.source.retrieved_at,
    }


def write_csv(path: Path, rows: Iterable[dict[str, object]]) -> int:
    return _write_dict_csv(path, rows, CSV_COLUMNS)


def public_rows(rows: Iterable[dict[str, object]]) -> Iterable[dict[str, object]]:
    for row in rows:
        has_uik_contact = bool(
            row["uik_voting_address"] or row["uik_voting_phone"] or row["uik_commission_phone"]
        )
        prefix = "uik" if has_uik_contact else "tik"
        yield {
            "subject": row["subject_name"],
            "tik_name": row["tik_name"],
            "tik_number": row["tik_number"],
            "tik_address": row["tik_address"],
            "tik_phone": row["tik_phone"],
            "uik_number": row["uik_number"],
            "uik_voting_address": row["uik_voting_address"],
            "uik_phone": row["uik_voting_phone"] or row["uik_commission_phone"],
            "contact_source": row[f"{prefix}_contact_source"],
            "contact_retrieved_at": row[f"{prefix}_contact_retrieved_at"],
            "contact_status": row[f"{prefix}_contact_status"],
        }


def write_public_csv(path: Path, rows: Iterable[dict[str, object]]) -> int:
    """Write the exact consumer-facing 11-column handoff."""

    return _write_dict_csv(path, public_rows(rows), PUBLIC_CSV_COLUMNS)


def _write_dict_csv(path: Path, rows: Iterable[dict[str, object]], columns: Sequence[str]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as destination:
            writer = csv.DictWriter(destination, fieldnames=columns, extrasaction="raise")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
                count += 1
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return count


def write_coverage(path: Path, coverage: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    payload = json.dumps(coverage, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as destination:
            destination.write(payload)
            destination.flush()
            os.fsync(destination.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
