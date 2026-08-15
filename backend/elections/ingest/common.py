from __future__ import annotations

import re
import unicodedata
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from elections.models import IngestionRun, RunStatus, SourceArtifact
from elections.sources import Artifact, sha256_file


def utc_now() -> datetime:
    return datetime.now(UTC)


def clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalized_name(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join(re.sub(r"[^0-9a-zа-я]+", " ", value).split())


def extract_number(value: str | None) -> str | None:
    if not value:
        return None
    matches = re.findall(r"\d+", value)
    return str(int(matches[-1])) if matches else None


def parse_int(value: Any, field: str) -> int:
    text = clean(value)
    if text is None:
        raise ValueError(f"{field} is empty")
    try:
        parsed = int(text.replace(" ", ""))
    except ValueError as exc:
        raise ValueError(f"{field} is not an integer: {text!r}") from exc
    if parsed < 0:
        raise ValueError(f"{field} is negative: {parsed}")
    return parsed


def parse_date(value: Any) -> date | None:
    text = clean(value)
    if text is None:
        return None
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    return None


def register_artifact(session: Session, artifact: Artifact, local_path: Path) -> SourceArtifact:
    actual_hash = sha256_file(local_path)
    if actual_hash != artifact.sha256:
        raise ValueError(
            f"checksum mismatch for {artifact.key}: expected {artifact.sha256}, got {actual_hash}"
        )
    row = session.scalar(select(SourceArtifact).where(SourceArtifact.key == artifact.key))
    if row is None:
        row = SourceArtifact(key=artifact.key, url=artifact.url, sha256=artifact.sha256)
        session.add(row)
    row.url = artifact.url
    row.sha256 = artifact.sha256
    row.size_bytes = local_path.stat().st_size
    row.media_type = artifact.media_type
    row.local_path = str(local_path)
    row.metadata_json = artifact.metadata
    retrieved = artifact.metadata.get("retrieved_at")
    row.retrieved_at = (
        datetime.fromisoformat(retrieved.replace("Z", "+00:00")) if retrieved else None
    )
    session.flush()
    return row


def start_run(session: Session, kind: str, artifact_id: int) -> IngestionRun:
    run = IngestionRun(
        kind=kind,
        artifact_id=artifact_id,
        status=RunStatus.RUNNING,
        started_at=utc_now(),
        stats_json={},
    )
    session.add(run)
    session.flush()
    return run


def finish_run(run: IngestionRun, stats: dict[str, Any]) -> None:
    run.status = RunStatus.SUCCEEDED
    run.finished_at = utc_now()
    run.stats_json = stats
