"""Resumable GAR-building resolution through a captured CEC lookup."""

from __future__ import annotations

import re
import time
import unicodedata
from datetime import UTC, datetime

from .cec import CecError, CecLookup, CecSuggestion
from .models import (
    AssignmentEvidence,
    AssignmentStatus,
    PollingStation,
    Provenance,
    SourceKind,
)
from .store import PollingMapStore

_PUNCTUATION = re.compile(r"[^0-9a-zа-яё]+", re.IGNORECASE)


def _comparison_key(value: str) -> str:
    return _PUNCTUATION.sub(" ", unicodedata.normalize("NFKC", value).casefold()).strip()


def _choose(address: str, suggestions: tuple[CecSuggestion, ...]) -> CecSuggestion | None:
    leaves = tuple(item for item in suggestions if item.leaf is True and item.address_id)
    exact = tuple(
        item for item in leaves if _comparison_key(item.label) == _comparison_key(address)
    )
    if len(exact) == 1:
        return exact[0]
    if len(leaves) == 1:
        return leaves[0]
    return None


def resolve_pending(
    store: PollingMapStore,
    lookup: CecLookup,
    *,
    region_code: str | None = None,
    limit: int | None = None,
    delay_seconds: float = 0.5,
) -> dict[str, int]:
    """Resolve unattempted canonical buildings and persist every outcome.

    A single terminal suggestion is accepted; multiple terminal suggestions
    require one normalized exact-address match. Ambiguity is preserved for
    later review instead of selecting the first search result.
    """

    if delay_seconds < 0:
        raise ValueError("delay_seconds must not be negative")
    counts = {status.value: 0 for status in AssignmentStatus}
    processed = 0
    cursor: tuple[str, str] | None = None
    while limit is None or processed < limit:
        batch_limit = min(1_000, limit - processed) if limit is not None else 1_000
        buildings = store.pending_buildings(
            region_code=region_code,
            limit=batch_limit,
            include_failed=True,
            after=cursor,
        )
        if not buildings:
            break
        for building in buildings:
            retrieved_at = datetime.now(UTC)
            source_url = lookup.source_url
            try:
                response = lookup.suggest(building.formatted_address)
                suggestion_digest = store.put_raw_response(
                    response.body, media_type="application/json"
                )
                selected = _choose(building.formatted_address, response.suggestions)
                leaves = tuple(
                    item for item in response.suggestions if item.leaf is True and item.address_id
                )
                if selected is None:
                    status = AssignmentStatus.AMBIGUOUS if leaves else AssignmentStatus.NO_MATCH
                    evidence = AssignmentEvidence(
                        address=building.identity,
                        status=status,
                        provenance=Provenance(
                            SourceKind.CEC_LOOKUP,
                            response.url,
                            retrieved_at,
                            "cec-address-autocomplete",
                            suggestion_digest,
                        ),
                        note=f"terminal suggestion count: {len(leaves)}",
                    )
                else:
                    result_url, _, result_body, committee = lookup.resolve(
                        selected.address_id or ""
                    )
                    result_digest = store.put_raw_response(
                        result_body, media_type="application/json"
                    )
                    station = PollingStation(
                        region_code=committee.region_code,
                        uik_number=committee.uik_number,
                        polling_place_address=committee.polling_place_address,
                        commission_address=committee.commission_address,
                        telephone=committee.telephone,
                    )
                    store.upsert_station(station)
                    evidence = AssignmentEvidence(
                        address=building.identity,
                        station_id=station.station_id,
                        status=AssignmentStatus.RESOLVED,
                        provenance=Provenance(
                            SourceKind.CEC_LOOKUP,
                            result_url,
                            retrieved_at,
                            "cec-address-id-resolver",
                            result_digest,
                        ),
                        note=(
                            f"CEC address_id={selected.address_id}; "
                            f"suggestions_sha256={suggestion_digest}"
                        ),
                    )
            except CecError as error:
                evidence = AssignmentEvidence(
                    address=building.identity,
                    status=AssignmentStatus.FAILED,
                    provenance=Provenance(
                        SourceKind.CEC_LOOKUP,
                        source_url,
                        retrieved_at,
                        "cec-address-lookup",
                    ),
                    note=str(error),
                )
            store.record_assignment(evidence)
            counts[evidence.status.value] += 1
            processed += 1
            cursor = (building.region_code, building.identity.key)
            if delay_seconds and (limit is None or processed < limit):
                time.sleep(delay_seconds)
    return counts
