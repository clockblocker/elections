"""FastAPI application factory."""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from elections.api.repository import ElectionRepository
from elections.api.router import router
from elections.api.schemas import HealthResponse

RepositoryFactory = Callable[[], ElectionRepository]


def _default_repository_factory() -> ElectionRepository:
    # Imports stay local so tooling can import the OpenAPI application before a
    # database connection is configured.
    from elections.api.sql_repository import create_repository

    return create_repository()


def create_app(repository_factory: RepositoryFactory | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        yield
        close = getattr(application.state.repository_factory, "close", None)
        if callable(close):
            close()

    application = FastAPI(
        title="Elections exploration API",
        version="0.1.0",
        description=(
            "Read-only access to 2021 State Duma UIK results, filters, "
            "validation state, and commission provenance."
        ),
        lifespan=lifespan,
    )
    application.state.repository_factory = repository_factory or _default_repository_factory
    cors_origins = [
        origin.strip()
        for origin in os.environ.get(
            "CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ]
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["*"],
    )

    @application.get(
        "/health",
        response_model=HealthResponse,
        tags=["system"],
        summary="Check API process health",
    )
    def health() -> HealthResponse:
        return HealthResponse()

    application.include_router(router)
    return application


app: Any = create_app()
