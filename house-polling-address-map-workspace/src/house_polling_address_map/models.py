"""Domain values for the address-to-polling-station map.

The values in this module are deliberately independent of SQLite.  Crawlers and
document parsers can construct and validate evidence before opening a store.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from hashlib import sha256

_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _required(value: str, field_name: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    return value


def _optional(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    return value or None


def _region_code(value: str) -> str:
    value = _required(value, "region_code")
    return str(int(value)) if value.isdigit() else value


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("retrieved_at must be timezone-aware")
    return value.astimezone(UTC)


class SourceKind(StrEnum):
    """Kind of official or derived source that made an assignment."""

    CEC_LOOKUP = "cec_lookup"
    REGIONAL_LOOKUP = "regional_lookup"
    ARCHIVED_LOOKUP = "archived_lookup"
    BOUNDARY_DOCUMENT = "boundary_document"
    MANUAL_REVIEW = "manual_review"


class AssignmentStatus(StrEnum):
    """Outcome of attempting to assign a building to a UIK."""

    RESOLVED = "resolved"
    NO_MATCH = "no_match"
    AMBIGUOUS = "ambiguous"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class AddressIdentity:
    """Stable identity allocated by an address source, normally GAR/FIAS."""

    namespace: str
    value: str

    def __post_init__(self) -> None:
        namespace = _required(self.namespace, "namespace").lower()
        value = _required(self.value, "value")
        if ":" in namespace:
            raise ValueError("namespace must not contain ':'")
        object.__setattr__(self, "namespace", namespace)
        object.__setattr__(self, "value", value)

    @property
    def key(self) -> str:
        return f"{self.namespace}:{self.value}"

    @classmethod
    def from_key(cls, key: str) -> AddressIdentity:
        namespace, separator, value = key.partition(":")
        if not separator:
            raise ValueError("address key must be '<namespace>:<value>'")
        return cls(namespace, value)


@dataclass(frozen=True, slots=True)
class BuildingAddress:
    """A canonical building-level address and its useful display components."""

    identity: AddressIdentity
    region_code: str
    formatted_address: str
    municipality: str | None = None
    locality: str | None = None
    street: str | None = None
    house: str | None = None
    postal_code: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "region_code", _region_code(self.region_code))
        object.__setattr__(
            self,
            "formatted_address",
            _required(self.formatted_address, "formatted_address"),
        )
        for field_name in ("municipality", "locality", "street", "house", "postal_code"):
            object.__setattr__(self, field_name, _optional(getattr(self, field_name)))


@dataclass(frozen=True, slots=True)
class PollingStation:
    """A UIK and the physical address at which voting takes place."""

    region_code: str
    uik_number: int
    polling_place_address: str
    commission_address: str | None = None
    telephone: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "region_code", _region_code(self.region_code))
        if (
            isinstance(self.uik_number, bool)
            or not isinstance(self.uik_number, int)
            or self.uik_number <= 0
        ):
            raise ValueError("uik_number must be a positive integer")
        object.__setattr__(
            self,
            "polling_place_address",
            _required(self.polling_place_address, "polling_place_address"),
        )
        object.__setattr__(self, "commission_address", _optional(self.commission_address))
        object.__setattr__(self, "telephone", _optional(self.telephone))

    @property
    def station_id(self) -> str:
        return f"{self.region_code}:{self.uik_number}"


@dataclass(frozen=True, slots=True)
class Provenance:
    """Enough source information to reproduce and audit a lookup result."""

    source: SourceKind
    source_url: str
    retrieved_at: datetime
    method: str
    raw_sha256: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source", SourceKind(self.source))
        object.__setattr__(self, "source_url", _required(self.source_url, "source_url"))
        object.__setattr__(self, "method", _required(self.method, "method"))
        object.__setattr__(self, "retrieved_at", _utc(self.retrieved_at))
        if self.raw_sha256 is not None:
            digest = self.raw_sha256.lower()
            if _SHA256_RE.fullmatch(digest) is None:
                raise ValueError("raw_sha256 must be a 64-character hexadecimal SHA-256")
            object.__setattr__(self, "raw_sha256", digest)


@dataclass(frozen=True, slots=True)
class AssignmentEvidence:
    """A time-versioned observation assigning one building to at most one UIK.

    ``valid_from`` and ``valid_to`` are inclusive.  They express when the
    assignment applies; ``provenance.retrieved_at`` expresses when it was seen.
    A resolved observation requires ``station_id``.  Every other outcome must
    omit it so that failed lookups cannot masquerade as mappings.
    """

    address: AddressIdentity
    status: AssignmentStatus
    provenance: Provenance
    station_id: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    note: str | None = None

    def __post_init__(self) -> None:
        status = AssignmentStatus(self.status)
        object.__setattr__(self, "status", status)
        station_id = _optional(self.station_id)
        object.__setattr__(self, "station_id", station_id)
        object.__setattr__(self, "note", _optional(self.note))
        if status is AssignmentStatus.RESOLVED and station_id is None:
            raise ValueError("resolved evidence requires station_id")
        if status is not AssignmentStatus.RESOLVED and station_id is not None:
            raise ValueError("non-resolved evidence must not specify station_id")
        if self.valid_from and self.valid_to and self.valid_from > self.valid_to:
            raise ValueError("valid_from must not be after valid_to")

    @property
    def evidence_id(self) -> str:
        """A stable identifier that makes recording the same evidence idempotent."""

        payload = {
            "address": self.address.key,
            "method": self.provenance.method,
            "note": self.note,
            "raw_sha256": self.provenance.raw_sha256,
            "retrieved_at": self.provenance.retrieved_at.isoformat(),
            "source": self.provenance.source.value,
            "source_url": self.provenance.source_url,
            "station_id": self.station_id,
            "status": self.status.value,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class Coverage:
    """Latest-observation coverage for a region or the whole address universe."""

    total: int
    resolved: int
    no_match: int
    ambiguous: int
    failed: int
    pending: int

    @property
    def attempted(self) -> int:
        return self.total - self.pending

    @property
    def resolved_fraction(self) -> float:
        return self.resolved / self.total if self.total else 0.0


@dataclass(frozen=True, slots=True)
class ResolvedMapping:
    """Latest resolved address→UIK observation suitable for export."""

    address_id: str
    region_code: str
    address: str
    station_id: str
    uik_number: int
    polling_place_address: str
    commission_address: str | None
    telephone: str | None
    source_url: str
    retrieved_at: datetime
    raw_sha256: str | None
