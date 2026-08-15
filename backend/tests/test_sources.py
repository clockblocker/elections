from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from elections.sources import SourceError, download_all, load_manifest


def test_manifest_download_is_verified_and_restartable(tmp_path: Path) -> None:
    source = tmp_path / "source.csv"
    source.write_bytes(b"one,two\n1,2\n")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "artifacts": [
                    {
                        "key": "fixture",
                        "url": source.as_uri(),
                        "filename": "downloaded.csv",
                        "sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                        "size_bytes": source.stat().st_size,
                        "media_type": "text/csv",
                    }
                ],
            }
        )
    )
    artifact = load_manifest(manifest)["fixture"]
    assert artifact.filename == "downloaded.csv"
    destination = tmp_path / "raw"
    first = download_all(manifest, destination)
    second = download_all(manifest, destination)
    assert first == second
    assert first["fixture"].read_bytes() == source.read_bytes()

    first["fixture"].write_text("changed")
    with pytest.raises(SourceError, match="size mismatch|checksum mismatch"):
        download_all(manifest, destination)
