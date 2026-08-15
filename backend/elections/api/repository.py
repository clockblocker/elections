"""Repository contract used by the read-only API.

Keeping query construction here makes the HTTP layer independent of ingestion and
allows the SQLAlchemy adapter to be tested separately from route serialization.
"""

from __future__ import annotations

from typing import Protocol

from elections.api.schemas import (
    Affiliation,
    BallotSummary,
    CandidateSummary,
    DatasetStatus,
    District,
    Party,
    PointFilters,
    PointPage,
    Region,
    SpecialType,
    Tik,
    UikDetail,
)


class ElectionRepository(Protocol):
    def dataset_status(self) -> DatasetStatus: ...

    def list_parties(self) -> list[Party]: ...

    def list_ballots(self) -> list[BallotSummary]: ...

    def list_districts(self) -> list[District]: ...

    def list_candidates(
        self,
        *,
        ballot_id: int | None = None,
        oik_id: int | None = None,
        affiliation: str | None = None,
        winner: bool | None = None,
    ) -> list[CandidateSummary]: ...

    def list_affiliations(self, *, oik_id: int | None = None) -> list[Affiliation]: ...

    def list_regions(self) -> list[Region]: ...

    def list_tiks(self, region: str | None = None) -> list[Tik]: ...

    def list_special_types(self) -> list[SpecialType]: ...

    def list_points(self, filters: PointFilters) -> PointPage: ...

    def get_uik(self, result_record_id: int) -> UikDetail | None: ...
