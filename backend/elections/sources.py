from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class SourceError(RuntimeError):
    """A source could not be located or verified."""


@dataclass(frozen=True)
class Artifact:
    key: str
    url: str
    filename: str
    sha256: str
    size_bytes: int
    media_type: str
    metadata: dict[str, Any]


def load_manifest(path: Path) -> dict[str, Artifact]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceError(f"cannot read source manifest {path}: {exc}") from exc
    if document.get("schema_version") != 1:
        raise SourceError(f"unsupported source manifest version in {path}")

    artifacts: dict[str, Artifact] = {}
    for raw in document.get("artifacts", []):
        required = {"key", "url", "filename", "sha256", "size_bytes", "media_type"}
        missing = sorted(required - raw.keys())
        if missing:
            raise SourceError(f"manifest artifact is missing {', '.join(missing)}")
        if raw["key"] in artifacts:
            raise SourceError(f"duplicate artifact key: {raw['key']}")
        metadata = {key: value for key, value in raw.items() if key not in required}
        artifacts[raw["key"]] = Artifact(
            key=raw["key"],
            url=raw["url"],
            filename=raw["filename"],
            sha256=raw["sha256"].lower(),
            size_bytes=int(raw["size_bytes"]),
            media_type=raw["media_type"],
            metadata=metadata,
        )
    return artifacts


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_artifact(artifact: Artifact, path: Path) -> None:
    actual_size = path.stat().st_size
    if actual_size != artifact.size_bytes:
        raise SourceError(
            f"size mismatch for {artifact.key}: expected {artifact.size_bytes}, got {actual_size}"
        )
    actual_hash = sha256_file(path)
    if actual_hash != artifact.sha256:
        raise SourceError(
            f"checksum mismatch for {artifact.key}: expected {artifact.sha256}, got {actual_hash}"
        )


def download_artifact(
    artifact: Artifact, destination: Path, *, force: bool = False, timeout: int = 120
) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    target = destination / artifact.filename
    if target.exists() and not force:
        verify_artifact(artifact, target)
        return target

    partial = target.with_name(f".{target.name}.part")
    try:
        request = urllib.request.Request(
            artifact.url, headers={"User-Agent": "elections-workbench/0.1"}
        )
        with (
            urllib.request.urlopen(request, timeout=timeout) as response,
            partial.open("wb") as out,
        ):
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
        verify_artifact(artifact, partial)
        os.replace(partial, target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return target


def download_all(manifest_path: Path, destination: Path, *, force: bool = False) -> dict[str, Path]:
    return {
        key: download_artifact(artifact, destination, force=force)
        for key, artifact in load_manifest(manifest_path).items()
    }
