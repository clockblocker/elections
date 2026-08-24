from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import pytest

from elections.acquisition import acquire_snapshot, load_acquisition_plan, verify_snapshot
from elections.cli import main
from elections.sources import SourceError


class CountingOpener:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request: urllib.request.Request, *, timeout: float) -> Any:
        self.calls.append(request.full_url)
        return urllib.request.urlopen(request, timeout=timeout)


def _plan(tmp_path: Path, index: Path, *, oiks: int = 2) -> Path:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_key": "fixture-single-member",
                "expected_oik_count": oiks,
                "expected_oiks": [
                    {"number": number, "region_name": "Fixture Region"}
                    for number in range(1, oiks + 1)
                ],
                "source": {"publisher": "fixture"},
                "seeds": [{"url": index.as_uri(), "relation": "index"}],
                "rate_limit_seconds": 0,
            }
        ),
        encoding="utf-8",
    )
    return plan


def _complete_fixture(tmp_path: Path) -> Path:
    index = tmp_path / "index.html"
    oik1 = tmp_path / "oik1.html"
    oik2 = tmp_path / "oik2.html"
    tik1 = tmp_path / "tik1.html"
    index.write_text(
        '<a href="oik1.html">ОИК №1 Адыгейская</a><a href="oik2.html">ОИК №2 Башкортостанская</a>',
        encoding="utf-8",
    )
    oik1.write_text('<a href="tik1.html">ТИК Центральная</a>', encoding="utf-8")
    oik2.write_text("УИК №21", encoding="utf-8")
    tik1.write_text("УИК №11 УИК №12", encoding="utf-8")
    return index


def test_acquisition_preserves_bytes_and_reports_hierarchy_coverage(tmp_path: Path) -> None:
    index = _complete_fixture(tmp_path)
    raw_dir = tmp_path / "raw"
    manifest = tmp_path / "snapshot.json"
    sleeps: list[float] = []

    report = acquire_snapshot(
        _plan(tmp_path, index),
        raw_dir,
        manifest,
        rate_limit_seconds=0.01,
        sleeper=sleeps.append,
    )

    assert report["coverage"] == {
        "expected_oiks": 2,
        "covered_oiks": 2,
        "missing_oiks": 0,
        "tiks": 1,
        "uiks": 3,
        "by_oik": [
            {
                "oik_number": 1,
                "region_name": "Fixture Region",
                "oik_name": "ОИК №1 Адыгейская",
                "status": "covered",
                "payloads": 2,
                "tiks": 1,
                "uiks": 2,
            },
            {
                "oik_number": 2,
                "region_name": "Fixture Region",
                "oik_name": "ОИК №2 Башкортостанская",
                "status": "covered",
                "payloads": 1,
                "tiks": 0,
                "uiks": 1,
            },
        ],
    }
    assert report["gaps"] == []
    assert len(report["payloads"]) == 4
    assert sleeps == [0.01, 0.01, 0.01]
    for record in report["payloads"]:
        preserved = raw_dir / record["path"]
        source_path = Path(
            urllib.request.url2pathname(urllib.parse.urlparse(record["source_url"]).path)
        )
        assert preserved.read_bytes() == source_path.read_bytes()
    assert verify_snapshot(manifest, raw_dir)["verified_payloads"] == 4


def test_acquisition_rerun_reuses_verified_payloads(tmp_path: Path) -> None:
    index = _complete_fixture(tmp_path)
    plan = _plan(tmp_path, index)
    raw_dir = tmp_path / "raw"
    manifest = tmp_path / "snapshot.json"
    first_opener = CountingOpener()
    acquire_snapshot(plan, raw_dir, manifest, opener=first_opener)
    assert len(first_opener.calls) == 4

    second_opener = CountingOpener()
    report = acquire_snapshot(plan, raw_dir, manifest, opener=second_opener)

    assert second_opener.calls == []
    assert report["run"] == {"network_requests": 0, "reused_payloads": 4}


def test_missing_source_is_never_silently_omitted(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    oik1 = tmp_path / "oik1.html"
    index.write_text(
        '<a href="oik1.html">ОИК №1</a><a href="missing.html">ОИК №2</a>',
        encoding="utf-8",
    )
    oik1.write_text("УИК №1", encoding="utf-8")

    report = acquire_snapshot(_plan(tmp_path, index), tmp_path / "raw", tmp_path / "snapshot.json")

    assert report["coverage"]["missing_oiks"] == 1
    assert any(
        gap["status"] == "unavailable" and gap.get("oik_number") == 2 for gap in report["gaps"]
    )


def test_embedded_cec_tree_does_not_invent_oik_from_internal_root_id(tmp_path: Path) -> None:
    index = tmp_path / "index.html"
    region = tmp_path / "region.html"
    oik = tmp_path / "oik.html"
    index.write_text(
        '<base href="' + tmp_path.as_uri() + '/">'
        '<script>tvdTreeJson = {"text":"Fixture Region","href":"region.html",'
        '"children":[]};</script>',
        encoding="utf-8",
    )
    region.write_text(
        '<base href="' + tmp_path.as_uri() + '/">'
        '<script>tvdTreeJson = {"text":"Fixture OIK","href":'
        '"oik.html?root=1000001","children":[]};</script>',
        encoding="utf-8",
    )
    oik.write_text("УИК №11", encoding="utf-8")
    plan = _plan(tmp_path, index, oiks=1)
    document = json.loads(plan.read_text(encoding="utf-8"))
    document["report_type"] = 463
    plan.write_text(json.dumps(document), encoding="utf-8")

    report = acquire_snapshot(plan, tmp_path / "raw", tmp_path / "snapshot.json")

    assert report["coverage"]["covered_oiks"] == 0
    assert all(row["oik_number"] is None for row in report["payloads"])
    assert any(row["uik_numbers"] == [] for row in report["payloads"])


def test_max_pages_bounds_discovery_probe(tmp_path: Path) -> None:
    index = _complete_fixture(tmp_path)
    report = acquire_snapshot(
        _plan(tmp_path, index),
        tmp_path / "raw",
        tmp_path / "snapshot.json",
        max_pages=1,
    )

    assert report["run"]["network_requests"] == 1
    assert any("max_pages=1" in gap.get("detail", "") for gap in report["gaps"])


def test_changed_preserved_payload_requires_force(tmp_path: Path) -> None:
    index = _complete_fixture(tmp_path)
    raw_dir = tmp_path / "raw"
    manifest = tmp_path / "snapshot.json"
    report = acquire_snapshot(_plan(tmp_path, index), raw_dir, manifest)
    (raw_dir / report["payloads"][0]["path"]).write_text("changed", encoding="utf-8")

    with pytest.raises(SourceError, match="--force"):
        acquire_snapshot(_plan(tmp_path, index), raw_dir, manifest)


def test_cli_acquires_then_verifies_snapshot(tmp_path: Path, capsys: Any) -> None:
    index = _complete_fixture(tmp_path)
    raw_dir = tmp_path / "raw"
    manifest = tmp_path / "snapshot.json"
    plan = _plan(tmp_path, index)

    assert (
        main(
            [
                "acquire-single-member",
                "--plan",
                str(plan),
                "--raw-dir",
                str(raw_dir),
                "--manifest",
                str(manifest),
                "--rate-limit",
                "0",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["coverage"]["covered_oiks"] == 2

    assert (
        main(
            [
                "acquire-single-member",
                "--raw-dir",
                str(raw_dir),
                "--manifest",
                str(manifest),
                "--verify-only",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["verified_payloads"] == 4


def test_plan_requires_explicit_expected_oik_enumeration(tmp_path: Path) -> None:
    plan = tmp_path / "plan.json"
    plan.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "snapshot_key": "bad",
                "expected_oik_count": 225,
                "expected_oiks": [{"number": 1}],
                "seeds": [{"url": "file:///fixture"}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SourceError, match="expected 225 OIKs"):
        load_acquisition_plan(plan)
