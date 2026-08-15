"""Read-only HTTP API for exploring election data."""

from elections.api.app import create_app

__all__ = ["create_app"]
