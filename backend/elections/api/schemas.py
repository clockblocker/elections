"""Public response and query models for the exploration API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Percentage = Annotated[float, Field(ge=0, le=100)]


class ApiModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class HealthResponse(ApiModel):
    status: Literal["ok"] = "ok"


class DatasetStatus(ApiModel):
    election_slug: str | None = None
    election_name: str | None = None
    election_date: date | None = None
    ingestion_status: str
    ready: bool = False
    reconciled: bool = False
    imported_at: datetime | None = None
    result_records: int = 0
    matched_records: int = 0
    unresolved_records: int = 0
    validation_errors: int = 0
    source_artifacts: int = 0
    published_totals: int = 0
    latest_source_key: str | None = None
    latest_source_sha256: str | None = None


class Party(ApiModel):
    id: int
    ballot_id: int
    name: str
    short_name: str | None = None
    position: int | None = None


class BallotSummary(ApiModel):
    id: int
    election_id: int
    kind: str
    name: str
    scope_key: str
    oik_id: int | None = None


class District(ApiModel):
    id: int
    code: str
    name: str
    region_name: str | None = None
    ballot_id: int
    candidate_count: int = 0
    result_records: int = 0


class CandidateSummary(ApiModel):
    id: int
    ballot_id: int
    oik_id: int
    district_code: str
    position: int
    full_name: str
    party_affiliation: str | None = None
    is_self_nominated: bool = False
    registration_status: str | None = None
    is_winner: bool = False


class Affiliation(ApiModel):
    value: str
    candidates: int = 0


class Region(ApiModel):
    id: int | None = None
    name: str
    code: str | None = None
    result_records: int


class Tik(ApiModel):
    id: int | None = None
    name: str
    region_name: str
    number: str | None = None
    result_records: int


class SpecialType(ApiModel):
    value: str
    label: str
    result_records: int
    is_deg: bool = False


class PointFilters(ApiModel):
    ballot_kinds: list[str] = Field(default_factory=list)
    ballot_ids: list[int] = Field(default_factory=list)
    oik_ids: list[int] = Field(default_factory=list)
    party_ids: list[int] = Field(default_factory=list)
    candidate_ids: list[int] = Field(default_factory=list)
    affiliations: list[str] = Field(default_factory=list)
    winner: bool | None = None
    regions: list[str] = Field(default_factory=list)
    tiks: list[str] = Field(default_factory=list)
    special_types: list[str] = Field(default_factory=list)
    match_statuses: list[str] = Field(default_factory=list)
    validation_statuses: list[str] = Field(default_factory=list)
    is_deg: bool | None = None
    turnout_min: Percentage | None = None
    turnout_max: Percentage | None = None
    result_min: Percentage | None = None
    result_max: Percentage | None = None
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=5_000, ge=1, le=20_000)


class ScatterPoint(ApiModel):
    result_record_id: int
    ballot_id: int | None = None
    ballot_kind: str = "party_list"
    scope_key: str = "federal"
    oik_id: int | None = None
    party_id: int | None = None
    candidate_id: int | None = None
    candidate_name: str | None = None
    party_affiliation: str | None = None
    is_winner: bool = False
    uik_number: str
    tik_name: str | None = None
    region_name: str
    registered_voters: int = Field(ge=0)
    ballots_counted: int = Field(ge=0)
    party_votes: int = Field(ge=0)
    turnout_percent: Percentage | None = None
    party_percent: Percentage | None = None
    match_status: str
    validation_status: str | None = None
    special_type: str | None = None
    is_deg: bool = False
    flags: list[str] = Field(default_factory=list)


class PointPage(ApiModel):
    items: list[ScatterPoint]
    offset: int
    limit: int
    total: int
    has_more: bool


class Hierarchy(ApiModel):
    election: str
    ballot: str
    region: str
    district: str | None = None
    tik: str | None = None
    uik: str


class Accounting(ApiModel):
    registered_voters: int | None = None
    ballots_received: int | None = None
    ballots_issued_early: int | None = None
    ballots_issued_at_station: int | None = None
    ballots_issued_outside_station: int | None = None
    ballots_cancelled: int | None = None
    ballots_in_mobile_boxes: int | None = None
    ballots_in_stationary_boxes: int | None = None
    invalid_ballots: int | None = None
    valid_ballots: int | None = None
    absentee_certificates_received: int | None = None
    absentee_certificates_issued: int | None = None
    lost_absentee_certificates: int | None = None
    lost_ballots: int | None = None
    unaccounted_ballots: int | None = None


class PartyResult(ApiModel):
    party_id: int
    name: str
    short_name: str | None = None
    position: int | None = None
    votes: int = Field(ge=0)
    percent: Percentage | None = None


class CandidateResult(ApiModel):
    candidate_id: int
    full_name: str
    position: int
    party_affiliation: str | None = None
    is_self_nominated: bool = False
    registration_status: str | None = None
    votes: int = Field(ge=0)
    percent: Percentage | None = None
    is_winner: bool = False


class CommissionMember(ApiModel):
    full_name: str
    role: str | None = None
    nominator: str | None = None
    start_date: date | None = None
    end_date: date | None = None


class CommissionMetadata(ApiModel):
    id: int | None = None
    gas_vybory_id: str | None = None
    name: str | None = None
    number: str | None = None
    address: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    chairperson: CommissionMember | None = None
    members: list[CommissionMember] = Field(default_factory=list)


class ValidationFinding(ApiModel):
    code: str
    severity: str
    expected: str | None = None
    actual: str | None = None
    details: dict[str, object] = Field(default_factory=dict)


class SourceLink(ApiModel):
    label: str
    url: str
    artifact_id: int | None = None
    sha256: str | None = None
    retrieved_at: datetime | None = None


class UikProtocol(ApiModel):
    result_record_id: int
    ballot_id: int
    ballot_kind: str
    ballot_name: str
    scope_key: str
    accounting: Accounting
    turnout_percent: Percentage | None = None
    party_results: list[PartyResult] = Field(default_factory=list)
    candidate_results: list[CandidateResult] = Field(default_factory=list)
    match_status: str
    validation_status: str | None = None
    special_type: str | None = None
    is_deg: bool = False
    flags: list[str] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)


class UikDetail(ApiModel):
    result_record_id: int
    uik_number: str
    gas_vybory_id: str | None = None
    source_row_number: int | None = None
    hierarchy: Hierarchy
    accounting: Accounting
    turnout_percent: Percentage | None = None
    party_results: list[PartyResult] = Field(default_factory=list)
    candidate_results: list[CandidateResult] = Field(default_factory=list)
    protocols: list[UikProtocol] = Field(default_factory=list)
    commission: CommissionMetadata | None = None
    match_status: str
    validation_status: str | None = None
    special_type: str | None = None
    is_deg: bool = False
    flags: list[str] = Field(default_factory=list)
    validation_findings: list[ValidationFinding] = Field(default_factory=list)
    sources: list[SourceLink] = Field(default_factory=list)
