"""Repository contract used by the read-only API.

Keeping query construction here makes the HTTP layer independent of ingestion and
allows the SQLAlchemy adapter to be tested separately from route serialization.
"""

from __future__ import annotations

from typing import Protocol

from elections.api.schemas import (
    DatasetStatus,
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

    def list_regions(self) -> list[Region]: ...

    def list_tiks(self, region: str | None = None) -> list[Tik]: ...

    def list_special_types(self) -> list[SpecialType]: ...

    def list_points(self, filters: PointFilters) -> PointPage: ...

    def get_uik(self, result_record_id: int) -> UikDetail | None: ...
