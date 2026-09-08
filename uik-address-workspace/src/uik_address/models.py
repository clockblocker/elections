"""Stable interchange models shared by crawlers and the CSV assembler."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any


def canonical_region_code(value: object) -> str:
    """Normalize numeric CEC subject codes without losing the abroad bucket."""

    if value is None:
        return ""
    text = str(value).strip()
    # ``0`` is the CEC's real code for polling stations abroad.  Do not use a
    # truthiness fallback here: integer input is also accepted by ``from_dict``.
    return str(int(text)) if text.isascii() and text.isdecimal() else text


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    url: str
    retrieved_at: str
    sha256: str
    status: int
    source_type: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceEvidence:
        return cls(
            url=str(value.get("url") or ""),
            retrieved_at=str(value.get("retrievedAt") or value.get("retrieved_at") or ""),
            sha256=str(value.get("sha256") or ""),
            status=int(value.get("status") or 0),
            source_type=str(value.get("sourceType") or value.get("source_type") or "unknown"),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BackboneRow:
    subject_code: str
    subject_name: str
    territorial_status: str
    tik_classifier_id: str
    tik_number: int
    tik_name: str
    uik_classifier_id: str
    uik_number: int
    source: SourceEvidence

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source"] = self.source.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> BackboneRow:
        return cls(
            subject_code=canonical_region_code(value.get("subject_code")),
            subject_name=str(value.get("subject_name") or ""),
            territorial_status=str(value.get("territorial_status") or ""),
            tik_classifier_id=str(value.get("tik_classifier_id") or ""),
            tik_number=int(value.get("tik_number") or 0),
            tik_name=str(value.get("tik_name") or ""),
            uik_classifier_id=str(value.get("uik_classifier_id") or ""),
            uik_number=int(value.get("uik_number") or 0),
            source=SourceEvidence.from_dict(value.get("source") or {}),
        )


@dataclass(frozen=True, slots=True)
class CommissionContact:
    subject_code: str
    commission_type: str
    commission_number: int | None
    commission_name: str
    external_id: str
    commission_address: str
    commission_phone: str
    voting_address: str
    voting_phone: str
    source: SourceEvidence

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["source"] = self.source.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CommissionContact:
        number = value.get("commission_number")
        return cls(
            subject_code=canonical_region_code(value.get("subject_code")),
            commission_type=str(value.get("commission_type") or ""),
            commission_number=int(number) if number not in (None, "") else None,
            commission_name=str(value.get("commission_name") or ""),
            external_id=str(value.get("external_id") or ""),
            commission_address=str(value.get("commission_address") or ""),
            commission_phone=str(value.get("commission_phone") or ""),
            voting_address=str(value.get("voting_address") or ""),
            voting_phone=str(value.get("voting_phone") or ""),
            source=SourceEvidence.from_dict(value.get("source") or {}),
        )
