"""FastAPI routes for the exploration API."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import Field

from elections.api.repository import ElectionRepository
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

router = APIRouter(prefix="/api/v1", tags=["exploration"])


def get_repository(request: Request) -> ElectionRepository:
    return request.app.state.repository_factory()


Repository = Annotated[ElectionRepository, Depends(get_repository)]
PartyId = Annotated[int, Field(ge=1)]


@router.get(
    "/dataset/status",
    response_model=DatasetStatus,
    summary="Get the imported dataset status",
)
def dataset_status(repository: Repository) -> DatasetStatus:
    return repository.dataset_status()


@router.get("/parties", response_model=list[Party], summary="List party filters")
def parties(repository: Repository) -> list[Party]:
    return repository.list_parties()


@router.get("/ballots", response_model=list[BallotSummary], summary="List ballot filters")
def ballots(repository: Repository) -> list[BallotSummary]:
    return repository.list_ballots()


@router.get("/districts", response_model=list[District], summary="List OIK filters")
def districts(repository: Repository) -> list[District]:
    return repository.list_districts()


@router.get("/candidates", response_model=list[CandidateSummary], summary="List candidates")
def candidates(
    repository: Repository,
    ballot_id: Annotated[int | None, Query(ge=1)] = None,
    oik_id: Annotated[int | None, Query(ge=1)] = None,
    affiliation: str | None = None,
    winner: bool | None = None,
) -> list[CandidateSummary]:
    return repository.list_candidates(
        ballot_id=ballot_id,
        oik_id=oik_id,
        affiliation=affiliation,
        winner=winner,
    )


@router.get(
    "/affiliations",
    response_model=list[Affiliation],
    summary="List candidate affiliations",
)
def affiliations(
    repository: Repository,
    oik_id: Annotated[int | None, Query(ge=1)] = None,
) -> list[Affiliation]:
    return repository.list_affiliations(oik_id=oik_id)


@router.get("/regions", response_model=list[Region], summary="List region filters")
def regions(repository: Repository) -> list[Region]:
    return repository.list_regions()


@router.get("/tiks", response_model=list[Tik], summary="List TIK filters")
def tiks(
    repository: Repository,
    region: Annotated[str | None, Query(description="Exact region name")] = None,
) -> list[Tik]:
    return repository.list_tiks(region=region)


@router.get(
    "/special-types",
    response_model=list[SpecialType],
    summary="List special-record filters",
)
def special_types(repository: Repository) -> list[SpecialType]:
    return repository.list_special_types()


@router.get(
    "/points",
    response_model=PointPage,
    summary="List compact UIK-party scatterplot points",
)
def points(
    repository: Repository,
    ballot_kind: Annotated[
        list[Literal["party_list", "single_member"]] | None, Query()
    ] = None,
    ballot_id: Annotated[list[PartyId] | None, Query()] = None,
    oik_id: Annotated[list[PartyId] | None, Query()] = None,
    party_id: Annotated[list[PartyId] | None, Query()] = None,
    candidate_id: Annotated[list[PartyId] | None, Query()] = None,
    affiliation: Annotated[list[str] | None, Query()] = None,
    winner: bool | None = None,
    region: Annotated[list[str] | None, Query()] = None,
    tik: Annotated[list[str] | None, Query()] = None,
    special_type: Annotated[list[str] | None, Query()] = None,
    match_status: Annotated[list[str] | None, Query()] = None,
    validation_status: Annotated[list[str] | None, Query()] = None,
    is_deg: bool | None = None,
    turnout_min: Annotated[float | None, Query(ge=0, le=100)] = None,
    turnout_max: Annotated[float | None, Query(ge=0, le=100)] = None,
    result_min: Annotated[float | None, Query(ge=0, le=100)] = None,
    result_max: Annotated[float | None, Query(ge=0, le=100)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=20_000)] = 5_000,
) -> PointPage:
    if turnout_min is not None and turnout_max is not None and turnout_min > turnout_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="turnout_min must not exceed turnout_max",
        )
    if result_min is not None and result_max is not None and result_min > result_max:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="result_min must not exceed result_max",
        )
    return repository.list_points(
        PointFilters(
            ballot_kinds=ballot_kind or [],
            ballot_ids=ballot_id or [],
            oik_ids=oik_id or [],
            party_ids=party_id or [],
            candidate_ids=candidate_id or [],
            affiliations=affiliation or [],
            winner=winner,
            regions=region or [],
            tiks=tik or [],
            special_types=special_type or [],
            match_statuses=match_status or [],
            validation_statuses=validation_status or [],
            is_deg=is_deg,
            turnout_min=turnout_min,
            turnout_max=turnout_max,
            result_min=result_min,
            result_max=result_max,
            offset=offset,
            limit=limit,
        )
    )


@router.get(
    "/uiks/{result_record_id}",
    response_model=UikDetail,
    summary="Get complete UIK result and commission details",
    responses={404: {"description": "UIK result not found"}},
)
def uik_detail(result_record_id: int, repository: Repository) -> UikDetail:
    item = repository.get_uik(result_record_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="UIK result not found",
        )
    return item
