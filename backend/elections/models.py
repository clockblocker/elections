from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class BallotKind(StrEnum):
    PARTY_LIST = "party_list"
    SINGLE_MEMBER = "single_member"


class GeographyType(StrEnum):
    COUNTRY = "country"
    REGION = "region"
    OIK = "oik"
    TIK = "tik"
    UIK = "uik"


class CommissionType(StrEnum):
    TIK = "tik"
    UIK = "uik"


class SpecialType(StrEnum):
    NONE = "none"
    DEG = "deg"
    FOREIGN = "foreign"
    TEMPORARY = "temporary"
    OTHER = "other"


class MatchStatus(StrEnum):
    PENDING = "pending"
    MATCHED = "matched"
    RESULT_ONLY = "result_only"
    COMMISSION_ONLY = "commission_only"
    AMBIGUOUS = "ambiguous"
    SPECIAL = "special"


class ValidationStatus(StrEnum):
    NOT_VALIDATED = "not_validated"
    VALID = "valid"
    WARNING = "warning"
    INVALID = "invalid"


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SourceArtifact(Base):
    __tablename__ = "source_artifacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(120), unique=True)
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    size_bytes: Mapped[int | None]
    media_type: Mapped[str | None] = mapped_column(String(120))
    local_path: Mapped[str | None] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(80))
    artifact_id: Mapped[int | None] = mapped_column(ForeignKey("source_artifacts.id"))
    status: Mapped[RunStatus] = mapped_column(Enum(RunStatus, native_enum=False, length=20))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)

    artifact: Mapped[SourceArtifact | None] = relationship()


class ImportReject(Base):
    __tablename__ = "import_rejects"
    __table_args__ = (
        UniqueConstraint("run_id", "source_row_number", name="uq_reject_run_row"),
        Index("ix_import_rejects_artifact_row", "artifact_id", "source_row_number"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("ingestion_runs.id", ondelete="CASCADE"))
    artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"))
    source_row_number: Mapped[int]
    source_record_id: Mapped[str | None] = mapped_column(String(120))
    error: Mapped[str] = mapped_column(Text)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    run: Mapped[IngestionRun] = relationship()
    artifact: Mapped[SourceArtifact] = relationship()


class Election(Base):
    __tablename__ = "elections"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(255))
    election_date: Mapped[date] = mapped_column(Date)

    ballots: Mapped[list[Ballot]] = relationship(back_populates="election")


class Ballot(Base):
    __tablename__ = "ballots"
    __table_args__ = (
        UniqueConstraint(
            "election_id", "kind", "scope_key", name="uq_ballot_election_kind_scope"
        ),
        UniqueConstraint(
            "election_id", "kind", "oik_id", name="uq_ballot_election_kind_oik"
        ),
        CheckConstraint(
            "(kind = 'PARTY_LIST' AND oik_id IS NULL AND scope_key = 'federal') OR "
            "(kind = 'SINGLE_MEMBER' AND oik_id IS NOT NULL "
            "AND scope_key <> 'federal' AND scope_key <> '')",
            name="ck_ballot_kind_scope",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    election_id: Mapped[int] = mapped_column(ForeignKey("elections.id", ondelete="CASCADE"))
    kind: Mapped[BallotKind] = mapped_column(Enum(BallotKind, native_enum=False, length=30))
    scope_key: Mapped[str] = mapped_column(String(120), default="federal", server_default="federal")
    oik_id: Mapped[int | None] = mapped_column(ForeignKey("geographies.id"))
    name: Mapped[str] = mapped_column(String(255))

    election: Mapped[Election] = relationship(back_populates="ballots")
    oik: Mapped[Geography | None] = relationship()
    options: Mapped[list[BallotOption]] = relationship(back_populates="ballot")
    candidates: Mapped[list[Candidate]] = relationship(back_populates="ballot")


class Geography(Base):
    __tablename__ = "geographies"
    __table_args__ = (
        UniqueConstraint("type", "code", name="uq_geography_type_code"),
        Index("ix_geographies_parent_type", "parent_id", "type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[GeographyType] = mapped_column(Enum(GeographyType, native_enum=False, length=20))
    name: Mapped[str] = mapped_column(String(255))
    code: Mapped[str] = mapped_column(String(120))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("geographies.id"))

    parent: Mapped[Geography | None] = relationship(remote_side="Geography.id")


class BallotOption(Base):
    __tablename__ = "ballot_options"
    __table_args__ = (
        UniqueConstraint("ballot_id", "position", name="uq_ballot_option_position"),
        Index("ix_ballot_options_ballot_name", "ballot_id", "name"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ballot_id: Mapped[int] = mapped_column(ForeignKey("ballots.id", ondelete="CASCADE"))
    position: Mapped[int]
    name: Mapped[str] = mapped_column(String(500))
    short_name: Mapped[str | None] = mapped_column(String(160))

    ballot: Mapped[Ballot] = relationship(back_populates="options")


class Candidate(Base):
    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("ballot_id", "position", name="uq_candidate_ballot_position"),
        UniqueConstraint(
            "ballot_id",
            "source_artifact_id",
            "source_record_id",
            name="uq_candidate_ballot_source_record",
        ),
        CheckConstraint("position > 0", name="ck_candidate_position_positive"),
        Index("ix_candidates_ballot_name", "ballot_id", "full_name"),
        Index("ix_candidates_gas_id", "gas_vybory_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ballot_id: Mapped[int] = mapped_column(ForeignKey("ballots.id", ondelete="CASCADE"))
    position: Mapped[int]
    full_name: Mapped[str] = mapped_column(String(500))
    party_affiliation: Mapped[str | None] = mapped_column(String(500))
    is_self_nominated: Mapped[bool] = mapped_column(Boolean, default=False)
    registration_status: Mapped[str | None] = mapped_column(String(120))
    source_artifact_id: Mapped[int | None] = mapped_column(ForeignKey("source_artifacts.id"))
    source_record_id: Mapped[str | None] = mapped_column(String(120))
    gas_vybory_id: Mapped[str | None] = mapped_column(String(120))
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    ballot: Mapped[Ballot] = relationship(back_populates="candidates")
    source_artifact: Mapped[SourceArtifact | None] = relationship()


class Commission(Base):
    __tablename__ = "commissions"
    __table_args__ = (
        UniqueConstraint(
            "source_artifact_id", "source_record_id", name="uq_commission_source_record"
        ),
        Index("ix_commissions_gas_id", "gas_vybory_id"),
        Index("ix_commissions_region_type_number", "region", "type", "number"),
        Index("ix_commissions_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"))
    source_record_id: Mapped[str] = mapped_column(String(120))
    gas_vybory_id: Mapped[str | None] = mapped_column(String(120))
    parent_id: Mapped[int | None] = mapped_column(ForeignKey("commissions.id"))
    parent_source_id: Mapped[str | None] = mapped_column(String(120))
    type: Mapped[CommissionType] = mapped_column(Enum(CommissionType, native_enum=False, length=20))
    region: Mapped[str] = mapped_column(String(255))
    name: Mapped[str] = mapped_column(String(500))
    number: Mapped[str | None] = mapped_column(String(80))
    address: Mapped[str | None] = mapped_column(Text)
    latitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    longitude: Mapped[Decimal | None] = mapped_column(Numeric(10, 7))
    term_start: Mapped[date | None] = mapped_column(Date)
    term_end: Mapped[date | None] = mapped_column(Date)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    source_artifact: Mapped[SourceArtifact] = relationship()
    parent: Mapped[Commission | None] = relationship(remote_side="Commission.id")
    memberships: Mapped[list[CommissionMembership]] = relationship(back_populates="commission")


class CommissionMembership(Base):
    __tablename__ = "commission_memberships"
    __table_args__ = (
        UniqueConstraint("commission_id", "source_record_id", name="uq_membership_source_record"),
        Index("ix_memberships_commission_role", "commission_id", "role"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    commission_id: Mapped[int] = mapped_column(ForeignKey("commissions.id", ondelete="CASCADE"))
    source_record_id: Mapped[str] = mapped_column(String(120))
    full_name: Mapped[str] = mapped_column(String(500))
    role: Mapped[str | None] = mapped_column(String(255))
    nominator: Mapped[str | None] = mapped_column(Text)
    term_start: Mapped[date | None] = mapped_column(Date)
    term_end: Mapped[date | None] = mapped_column(Date)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    commission: Mapped[Commission] = relationship(back_populates="memberships")


class ResultRecord(Base):
    __tablename__ = "result_records"
    __table_args__ = (
        UniqueConstraint("source_artifact_id", "source_row_number", name="uq_result_source_row"),
        Index("ix_results_region_tik_uik", "region_name", "tik_name", "uik_number"),
        Index("ix_results_ballot_special", "ballot_id", "special_type"),
        Index("ix_results_ballot_uik", "ballot_id", "uik_number"),
        Index("ix_results_match_status", "match_status"),
        Index("ix_results_validation_status", "validation_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_artifact_id: Mapped[int] = mapped_column(ForeignKey("source_artifacts.id"))
    source_row_number: Mapped[int]
    ballot_id: Mapped[int] = mapped_column(ForeignKey("ballots.id"))
    region_name: Mapped[str] = mapped_column(String(255))
    oik_name: Mapped[str | None] = mapped_column(String(255))
    tik_name: Mapped[str | None] = mapped_column(String(500))
    uik_number: Mapped[str | None] = mapped_column(String(80))
    gas_vybory_id: Mapped[str | None] = mapped_column(String(120))
    source_url: Mapped[str | None] = mapped_column(Text)
    special_type: Mapped[SpecialType] = mapped_column(
        Enum(SpecialType, native_enum=False, length=20), default=SpecialType.NONE
    )
    is_deg: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    match_status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, native_enum=False, length=20), default=MatchStatus.PENDING
    )
    matched_commission_id: Mapped[int | None] = mapped_column(ForeignKey("commissions.id"))
    validation_status: Mapped[ValidationStatus] = mapped_column(
        Enum(ValidationStatus, native_enum=False, length=30),
        default=ValidationStatus.NOT_VALIDATED,
    )

    source_artifact: Mapped[SourceArtifact] = relationship()
    ballot: Mapped[Ballot] = relationship()
    matched_commission: Mapped[Commission | None] = relationship()
    accounting: Mapped[BallotAccounting | None] = relationship(
        back_populates="result_record", uselist=False
    )
    votes: Mapped[list[Vote]] = relationship(back_populates="result_record")
    match_evidence: Mapped[list[MatchEvidence]] = relationship(back_populates="result_record")


class BallotAccounting(Base):
    __tablename__ = "ballot_accounting"
    __table_args__ = (
        CheckConstraint("registered_voters >= 0", name="ck_accounting_registered_nonnegative"),
    )

    result_record_id: Mapped[int] = mapped_column(
        ForeignKey("result_records.id", ondelete="CASCADE"), primary_key=True
    )
    registered_voters: Mapped[int]
    ballots_received: Mapped[int]
    ballots_issued_early: Mapped[int]
    ballots_issued_at_station: Mapped[int]
    ballots_issued_outside: Mapped[int]
    ballots_cancelled: Mapped[int]
    portable_boxes_ballots: Mapped[int]
    stationary_boxes_ballots: Mapped[int]
    invalid_ballots: Mapped[int]
    valid_ballots: Mapped[int]
    lost_ballots: Mapped[int]
    unaccounted_ballots: Mapped[int]

    result_record: Mapped[ResultRecord] = relationship(back_populates="accounting")


class Vote(Base):
    __tablename__ = "votes"
    __table_args__ = (
        UniqueConstraint("result_record_id", "option_id", name="uq_vote_result_option"),
        UniqueConstraint("result_record_id", "candidate_id", name="uq_vote_result_candidate"),
        CheckConstraint(
            "(option_id IS NOT NULL AND candidate_id IS NULL) OR "
            "(option_id IS NULL AND candidate_id IS NOT NULL)",
            name="ck_vote_exactly_one_target",
        ),
        CheckConstraint("votes >= 0", name="ck_votes_nonnegative"),
        Index("ix_votes_option_result", "option_id", "result_record_id"),
        Index("ix_votes_candidate_result", "candidate_id", "result_record_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    result_record_id: Mapped[int] = mapped_column(
        ForeignKey("result_records.id", ondelete="CASCADE")
    )
    option_id: Mapped[int | None] = mapped_column(ForeignKey("ballot_options.id"))
    candidate_id: Mapped[int | None] = mapped_column(ForeignKey("candidates.id"))
    votes: Mapped[int]

    result_record: Mapped[ResultRecord] = relationship(back_populates="votes")
    option: Mapped[BallotOption | None] = relationship()
    candidate: Mapped[Candidate | None] = relationship()


class MatchEvidence(Base):
    __tablename__ = "match_evidence"
    __table_args__ = (
        UniqueConstraint(
            "result_record_id", "commission_id", "method", name="uq_match_candidate_method"
        ),
        Index("ix_match_evidence_result_selected", "result_record_id", "selected"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    result_record_id: Mapped[int] = mapped_column(
        ForeignKey("result_records.id", ondelete="CASCADE")
    )
    commission_id: Mapped[int] = mapped_column(ForeignKey("commissions.id", ondelete="CASCADE"))
    method: Mapped[str] = mapped_column(String(80))
    score: Mapped[Decimal] = mapped_column(Numeric(6, 5))
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    selected: Mapped[bool] = mapped_column(Boolean, default=False)

    result_record: Mapped[ResultRecord] = relationship(back_populates="match_evidence")
    commission: Mapped[Commission] = relationship()


class ValidationFinding(Base):
    __tablename__ = "validation_findings"
    __table_args__ = (
        Index("ix_validation_scope_code", "scope", "code"),
        Index("ix_validation_result", "result_record_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    result_record_id: Mapped[int | None] = mapped_column(
        ForeignKey("result_records.id", ondelete="CASCADE")
    )
    scope: Mapped[str] = mapped_column(String(40))
    scope_identifier: Mapped[str] = mapped_column(String(500))
    code: Mapped[str] = mapped_column(String(100))
    severity: Mapped[Severity] = mapped_column(Enum(Severity, native_enum=False, length=20))
    expected_value: Mapped[int | None]
    actual_value: Mapped[int | None]
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class PublishedTotal(Base):
    __tablename__ = "published_totals"
    __table_args__ = (
        UniqueConstraint(
            "ballot_id",
            "level",
            "scope_identifier",
            "metric",
            "option_name",
            name="uq_published_total_dimension",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    ballot_id: Mapped[int] = mapped_column(ForeignKey("ballots.id", ondelete="CASCADE"))
    level: Mapped[str] = mapped_column(String(40))
    scope_identifier: Mapped[str] = mapped_column(String(500))
    metric: Mapped[str] = mapped_column(String(80))
    option_name: Mapped[str] = mapped_column(String(500), default="")
    value: Mapped[int]
    source_url: Mapped[str | None] = mapped_column(Text)
