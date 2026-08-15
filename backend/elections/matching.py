from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import bindparam, delete, insert, select, update
from sqlalchemy.orm import Session

from elections.ingest.common import normalized_name
from elections.models import (
    Commission,
    CommissionType,
    MatchEvidence,
    MatchStatus,
    ResultRecord,
    SpecialType,
)

STOP_WORDS = {
    "избирательная",
    "комиссия",
    "участковая",
    "территориальная",
    "районная",
    "городская",
    "область",
    "области",
    "республика",
    "край",
    "города",
    "город",
}
BATCH_SIZE = 5_000


def _tokens(value: str | None) -> frozenset[str]:
    tokens = set(normalized_name(value).split()) - STOP_WORDS
    return frozenset(token[:5] if len(token) > 5 else token for token in tokens)


@lru_cache(maxsize=16_384)
def _result_tokens(value: str | None) -> frozenset[str]:
    # Result geography repeats heavily; commission addresses generally do not.
    return _tokens(value)


def _token_similarity(left: frozenset[str], right: frozenset[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _similarity(left: str | None, right: str | None) -> float:
    return _token_similarity(_tokens(left), _tokens(right))


@dataclass(frozen=True)
class Candidate:
    commission_id: int
    method: str
    score: float
    evidence: dict[str, Any]


@dataclass(frozen=True)
class CommissionProfile:
    commission_id: int
    region: str
    parent_name: str | None
    region_tokens: frozenset[str]
    tik_tokens: frozenset[str]


@dataclass(frozen=True)
class ResultProfile:
    result_id: int
    source_row_number: int
    region_name: str
    tik_name: str | None
    uik_number: str | None
    gas_vybory_id: str | None
    special_type: SpecialType


def _candidate_rows(
    session: Session,
) -> tuple[
    dict[str, list[CommissionProfile]],
    dict[str, list[CommissionProfile]],
    set[int],
]:
    rows = session.execute(
        select(
            Commission.id,
            Commission.gas_vybory_id,
            Commission.parent_id,
            Commission.type,
            Commission.number,
            Commission.region,
            Commission.address,
            Commission.name,
        )
    ).all()
    parent_names = {row.id: row.name for row in rows}
    by_gas: dict[str, list[CommissionProfile]] = defaultdict(list)
    by_number: dict[str, list[CommissionProfile]] = defaultdict(list)
    commission_ids: set[int] = set()
    for row in rows:
        commission_ids.add(row.id)
        parent_name = parent_names.get(row.parent_id)
        region_context = " ".join(value for value in (row.region, row.address, row.name) if value)
        profile = CommissionProfile(
            commission_id=row.id,
            region=row.region,
            parent_name=parent_name,
            region_tokens=_tokens(region_context),
            tik_tokens=_tokens(parent_name),
        )
        if row.gas_vybory_id:
            by_gas[row.gas_vybory_id].append(profile)
        if row.type == CommissionType.UIK and row.number:
            by_number[row.number].append(profile)
    return by_gas, by_number, commission_ids


def _candidates(
    result: ResultProfile,
    by_gas: dict[str, list[CommissionProfile]],
    by_number: dict[str, list[CommissionProfile]],
) -> list[Candidate]:
    if result.gas_vybory_id:
        stable = by_gas.get(result.gas_vybory_id, [])
        return [
            Candidate(
                commission.commission_id,
                "gas_vybory_id",
                1.0,
                {"gas_vybory_id": result.gas_vybory_id},
            )
            for commission in stable
        ]

    region_tokens = _result_tokens(result.region_name)
    tik_tokens = _result_tokens(result.tik_name)
    number_candidates = by_number.get(result.uik_number or "", [])
    candidates: list[Candidate] = []
    for commission in number_candidates:
        region_score = _token_similarity(region_tokens, commission.region_tokens)
        tik_score = _token_similarity(tik_tokens, commission.tik_tokens)
        score = 0.55 + (0.30 * region_score) + (0.15 * tik_score)
        # Keep candidates with geography evidence. A globally unique number is also auditable.
        if region_score or tik_score or len(number_candidates) == 1:
            candidates.append(
                Candidate(
                    commission.commission_id,
                    "uik_number_geography",
                    score,
                    {
                        "uik_number": result.uik_number,
                        "region_similarity": round(region_score, 5),
                        "tik_similarity": round(tik_score, 5),
                        "commission_region": commission.region,
                        "parent_name": commission.parent_name,
                    },
                )
            )
    return sorted(candidates, key=lambda item: (-item.score, item.commission_id))


def _selected_candidate(candidates: list[Candidate]) -> Candidate | None:
    if len(candidates) == 1 and (
        candidates[0].method == "gas_vybory_id" or candidates[0].score >= 0.55
    ):
        return candidates[0]
    if len(candidates) > 1:
        top, runner_up = candidates[0], candidates[1]
        if top.score >= 0.70 and top.score - runner_up.score > 0.05:
            return top
    return None


def _flush_rows(
    session: Session,
    evidence_rows: list[dict[str, Any]],
    result_rows: list[dict[str, Any]],
) -> None:
    connection = session.connection()
    if evidence_rows:
        connection.execute(insert(MatchEvidence.__table__), evidence_rows)
        evidence_rows.clear()
    if result_rows:
        statement = (
            update(ResultRecord.__table__)
            .where(ResultRecord.__table__.c.id == bindparam("record_id"))
            .values(
                matched_commission_id=bindparam("commission_id"),
                match_status=bindparam("status"),
            )
        )
        connection.execute(statement, result_rows)
        result_rows.clear()


def match_results(session: Session, audit_path: Path | None = None) -> dict[str, int]:
    session.execute(delete(MatchEvidence))
    session.execute(
        update(ResultRecord).values(matched_commission_id=None, match_status=MatchStatus.PENDING)
    )
    by_gas, by_number, commission_ids = _candidate_rows(session)
    counts = {status.value: 0 for status in MatchStatus}
    selected_commissions: set[int] = set()
    unresolved: list[dict[str, Any]] = []
    evidence_rows: list[dict[str, Any]] = []
    result_rows: list[dict[str, Any]] = []

    statement = select(
        ResultRecord.id,
        ResultRecord.source_row_number,
        ResultRecord.region_name,
        ResultRecord.tik_name,
        ResultRecord.uik_number,
        ResultRecord.gas_vybory_id,
        ResultRecord.special_type,
    ).order_by(ResultRecord.id)
    for row in session.execute(statement).yield_per(BATCH_SIZE):
        result = ResultProfile(*row)
        candidates: list[Candidate] = []
        selected: Candidate | None = None
        if result.special_type != SpecialType.NONE:
            status = MatchStatus.SPECIAL
        else:
            candidates = _candidates(result, by_gas, by_number)
            selected = _selected_candidate(candidates)
            if selected:
                status = MatchStatus.MATCHED
                selected_commissions.add(selected.commission_id)
            elif candidates:
                status = MatchStatus.AMBIGUOUS
            else:
                status = MatchStatus.RESULT_ONLY

        counts[status.value] += 1
        result_rows.append(
            {
                "record_id": result.result_id,
                "commission_id": selected.commission_id if selected else None,
                "status": status,
            }
        )
        for candidate in candidates:
            evidence_rows.append(
                {
                    "result_record_id": result.result_id,
                    "commission_id": candidate.commission_id,
                    "method": candidate.method,
                    "score": Decimal(f"{candidate.score:.5f}"),
                    "evidence_json": candidate.evidence,
                    "selected": selected is not None
                    and candidate.commission_id == selected.commission_id,
                }
            )
        if status in {MatchStatus.AMBIGUOUS, MatchStatus.RESULT_ONLY}:
            unresolved.append(
                {
                    "result_record_id": result.result_id,
                    "status": status.value,
                    "source_row_number": result.source_row_number,
                    "region": result.region_name,
                    "tik": result.tik_name,
                    "uik": result.uik_number,
                    "candidate_commission_ids": [item.commission_id for item in candidates],
                }
            )
        if len(result_rows) >= BATCH_SIZE or len(evidence_rows) >= BATCH_SIZE:
            _flush_rows(session, evidence_rows, result_rows)
    _flush_rows(session, evidence_rows, result_rows)

    counts[MatchStatus.COMMISSION_ONLY.value] = len(commission_ids - selected_commissions)
    if audit_path:
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        audit_path.write_text(
            json.dumps({"counts": counts, "unresolved": unresolved}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    # Core executemany bypasses the identity map; callers must not observe stale ORM objects.
    session.expire_all()
    return counts
