from __future__ import annotations

import json
import urllib.error
from email.message import Message
from pathlib import Path

import pytest
from sqlalchemy import select

from elections.gas_resolution import (
    GasResolutionError,
    GasResponseCache,
    parse_commission_id_page,
    parse_parent_results_page,
    resolve_gas_ids,
)
from elections.ingest.commissions import import_commissions
from elections.ingest.results import import_results
from elections.matching import match_results
from elections.models import Commission, GasIdResolution, MatchEvidence, MatchStatus, ResultRecord

FIXTURES = Path(__file__).parent / "fixtures/gas"
PARENT_URL = "https://www.vybory.izbirkom.ru/region/izbirkom?action=show&tvd=500&vrn=900"
DETAIL_1 = "https://www.vybory.izbirkom.ru/region/izbirkom?action=show&root=1000002&tvd=2012000461501&vrn=100100225883172&region=1"
EXPLICIT_COMMISSION_LINK_HTML = b'<a href="/region/test?action=ik&amp;vrn=1001">commission</a>'
SYNTHETIC_PARENT_HTML = f"""
<a href="{DETAIL_1}">УИК № 1</a>
<a href="/region/izbirkom?action=show&amp;tvd=2012000461502&amp;vrn=100100225883172">УИК № 2</a>
""".encode()


class FakeResponse:
    def __init__(self, payload: bytes, url: str, status: int = 200) -> None:
        self.payload = payload
        self.url = url
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "text/html; charset=windows-1251"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return self.payload

    def geturl(self) -> str:
        return self.url


class MappingOpener:
    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.calls: list[str] = []

    def __call__(self, request, *, timeout: float):
        del timeout
        self.calls.append(request.full_url)
        if request.full_url not in self.payloads:
            raise urllib.error.URLError("not mapped")
        return FakeResponse(self.payloads[request.full_url], request.full_url)


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_genuine_parent_result_tree_finds_multiple_uiks() -> None:
    tree = fixture("altai-result-tik-node.json")
    base_url = (
        "http://www.vybory.izbirkom.ru/region/izbirkom?action=show&root=1000058"
        "&tvd=22220002523303&vrn=100100225883172&region=22&sub_region=22"
    )
    links = parse_parent_results_page(f"tvdTreeJson = {tree}\n", base_url)

    assert len(links) == 23
    assert "tvd=9229002199809" in links["592"][0]
    assert "tvd=9229002199810" in links["593"][0]


def test_explicit_action_ik_url_extracts_archive_id_not_result_identifiers() -> None:
    commission_tree = json.loads(fixture("altai-commission-tik-children.json"))
    source_url = (
        "http://www.altai-terr.vybory.izbirkom.ru/region/altai-terr"
        "?action=ik&vrn=9229002166846"
    )
    gas_id, source = parse_commission_id_page(fixture("malformed.html"), source_url)

    assert gas_id == commission_tree[0]["id"] == "9229002166846"
    assert source == source_url
    assert gas_id not in {"9229002199809", "100100225883172"}


def test_conflicting_and_changed_commission_pages_fail_closed() -> None:
    conflicting = EXPLICIT_COMMISSION_LINK_HTML.decode() + (
        '<a href="/region/test?action=ik&amp;vrn=9999">other commission</a>'
    )
    with pytest.raises(GasResolutionError, match="conflicting_commission_ids"):
        parse_commission_id_page(conflicting, DETAIL_1)
    with pytest.raises(GasResolutionError, match="commission_link_not_found"):
        parse_commission_id_page(fixture("malformed.html"), DETAIL_1)


def test_cache_offline_rerun_and_interrupted_resume(tmp_path: Path) -> None:
    opener = MappingOpener(
        {
            PARENT_URL: SYNTHETIC_PARENT_HTML,
            DETAIL_1: EXPLICIT_COMMISSION_LINK_HTML,
        }
    )
    first = GasResponseCache(
        tmp_path / "cache", opener=opener, rate_limit_seconds=0, sleeper=lambda _: None
    )
    assert first.fetch(PARENT_URL).status == "downloaded"  # interruption after parent

    resumed = GasResponseCache(
        tmp_path / "cache", opener=opener, rate_limit_seconds=0, sleeper=lambda _: None
    )
    results = resumed.fetch_many([PARENT_URL, DETAIL_1], concurrency=2)
    assert results[PARENT_URL].status == "cached"
    assert results[DETAIL_1].status == "downloaded"

    offline = GasResponseCache(tmp_path / "cache", offline=True)
    assert offline.fetch(PARENT_URL).status == "cached"
    assert offline.fetch(DETAIL_1).status == "cached"
    assert opener.calls.count(PARENT_URL) == 1


def test_cache_retries_transient_errors_and_stops_on_permanent_failure(tmp_path: Path) -> None:
    attempts = 0

    def transient(request, *, timeout: float):
        nonlocal attempts
        del timeout
        attempts += 1
        if attempts < 3:
            raise urllib.error.URLError("temporary")
        return FakeResponse(b"ok", request.full_url)

    cache = GasResponseCache(
        tmp_path / "retry",
        opener=transient,
        retries=3,
        rate_limit_seconds=0,
        sleeper=lambda _: None,
    )
    assert cache.fetch(PARENT_URL).status == "downloaded"
    assert attempts == 3

    permanent_attempts = 0

    def permanent(request, *, timeout: float):
        nonlocal permanent_attempts
        del timeout
        permanent_attempts += 1
        raise urllib.error.HTTPError(request.full_url, 404, "missing", {}, None)

    permanent_cache = GasResponseCache(
        tmp_path / "permanent",
        opener=permanent,
        retries=5,
        rate_limit_seconds=0,
        sleeper=lambda _: None,
    )
    failure = permanent_cache.fetch(PARENT_URL)
    assert failure.status == "failed"
    assert failure.error == "http_404"
    assert permanent_attempts == 1


def _prepared(session, results_source, commission_source) -> ResultRecord:
    results_path, results_artifact = results_source
    commissions_path, commissions_artifact = commission_source
    import_results(session, results_artifact, results_path)
    import_commissions(session, commissions_artifact, commissions_path)
    physical = session.scalar(select(ResultRecord).where(ResultRecord.uik_number == "1"))
    assert physical is not None
    physical.source_url = PARENT_URL
    special = session.scalar(select(ResultRecord).where(ResultRecord.uik_number.is_(None)))
    assert special is not None
    special.source_url = None
    session.flush()
    return physical


def test_resolver_persists_exact_id_provenance_and_deterministic_audit(
    session, results_source, commission_source, tmp_path: Path
) -> None:
    physical = _prepared(session, results_source, commission_source)
    opener = MappingOpener(
        {
            PARENT_URL: SYNTHETIC_PARENT_HTML,
            DETAIL_1: EXPLICIT_COMMISSION_LINK_HTML,
        }
    )
    cache = GasResponseCache(
        tmp_path / "raw/gas",
        opener=opener,
        rate_limit_seconds=0,
        sleeper=lambda _: None,
    )
    report_path = tmp_path / "report.json"
    human_path = tmp_path / "report.md"
    report = resolve_gas_ids(
        session,
        cache_dir=tmp_path / "raw/gas",
        report_path=report_path,
        human_report_path=human_path,
        commission_artifact_key="test-commissions",
        cache=cache,
    )

    session.refresh(physical)
    resolution = session.get(GasIdResolution, physical.id)
    assert physical.gas_vybory_id == "1001"
    assert resolution is not None and resolution.status == "resolved"
    assert resolution.parent_source_artifact_id is not None
    assert resolution.detail_source_artifact_id is not None
    assert resolution.evidence_json["commission_parameter"] == "action=ik&vrn"
    assert report["summary"]["exact_commission_links_established"] == 1
    assert report["summary"]["downloaded_requests"] == 2
    assert human_path.exists()

    offline_cache = GasResponseCache(tmp_path / "raw/gas", offline=True)
    resolve_gas_ids(
        session,
        cache_dir=tmp_path / "raw/gas",
        report_path=tmp_path / "offline-1.json",
        human_report_path=tmp_path / "offline-1.md",
        commission_artifact_key="test-commissions",
        cache=offline_cache,
    )
    resolve_gas_ids(
        session,
        cache_dir=tmp_path / "raw/gas",
        report_path=tmp_path / "offline-2.json",
        human_report_path=tmp_path / "offline-2.md",
        commission_artifact_key="test-commissions",
        cache=GasResponseCache(tmp_path / "raw/gas", offline=True),
    )
    assert (tmp_path / "offline-1.json").read_bytes() == (tmp_path / "offline-2.json").read_bytes()


def test_resolver_preserves_exact_id_missing_from_selected_snapshot(
    session, results_source, commission_source, tmp_path: Path
) -> None:
    physical = _prepared(session, results_source, commission_source)
    missing_page = EXPLICIT_COMMISSION_LINK_HTML.decode().replace("vrn=1001", "vrn=9999")
    cache = GasResponseCache(
        tmp_path / "cache",
        opener=MappingOpener(
            {PARENT_URL: SYNTHETIC_PARENT_HTML, DETAIL_1: missing_page.encode()}
        ),
        rate_limit_seconds=0,
        sleeper=lambda _: None,
    )
    report = resolve_gas_ids(
        session,
        cache_dir=tmp_path / "cache",
        report_path=tmp_path / "report.json",
        human_report_path=tmp_path / "report.md",
        commission_artifact_key="test-commissions",
        cache=cache,
    )

    session.refresh(physical)
    assert physical.gas_vybory_id == "9999"
    resolution = session.get(GasIdResolution, physical.id)
    assert resolution is not None
    assert resolution.reason_code == "id_absent_from_commission_snapshot"
    assert report["ids_missing_from_commission_snapshot"] == ["9999"]
    counts = match_results(session)
    assert counts[MatchStatus.DATA_INTEGRITY_ERROR.value] == 1
    assert physical.matched_commission_id is None


def test_result_page_without_explicit_commission_id_stays_heuristic_fallback(
    session, results_source, commission_source, tmp_path: Path
) -> None:
    physical = _prepared(session, results_source, commission_source)
    cache = GasResponseCache(
        tmp_path / "cache",
        opener=MappingOpener(
            {
                PARENT_URL: SYNTHETIC_PARENT_HTML,
                DETAIL_1: fixture("malformed.html").encode(),
            }
        ),
        rate_limit_seconds=0,
        sleeper=lambda _: None,
    )

    resolve_gas_ids(
        session,
        cache_dir=tmp_path / "cache",
        report_path=tmp_path / "report.json",
        human_report_path=tmp_path / "report.md",
        commission_artifact_key="test-commissions",
        cache=cache,
    )

    resolution = session.get(GasIdResolution, physical.id)
    assert resolution is not None
    assert resolution.reason_code == "no_explicit_commission_id"
    assert physical.gas_vybory_id is None
    match_results(session)
    assert physical.match_status == MatchStatus.MATCHED
    evidence = session.scalars(
        select(MatchEvidence).where(MatchEvidence.result_record_id == physical.id)
    ).all()
    assert [item.method for item in evidence if item.selected] == ["uik_number_geography"]


def test_changed_parent_shape_has_explicit_failure_reason(
    session, results_source, commission_source, tmp_path: Path
) -> None:
    physical = _prepared(session, results_source, commission_source)
    cache = GasResponseCache(
        tmp_path / "cache",
        opener=MappingOpener({PARENT_URL: fixture("malformed.html").encode()}),
        rate_limit_seconds=0,
        sleeper=lambda _: None,
    )

    report = resolve_gas_ids(
        session,
        cache_dir=tmp_path / "cache",
        report_path=tmp_path / "report.json",
        human_report_path=tmp_path / "report.md",
        commission_artifact_key="test-commissions",
        cache=cache,
    )

    resolution = session.get(GasIdResolution, physical.id)
    assert resolution is not None and resolution.reason_code == "malformed_parent"
    assert report["unresolved_by_reason"]["malformed_parent"] == 1


def test_exact_match_takes_precedence_over_heuristic_candidates(
    session, results_source, commission_source
) -> None:
    physical = _prepared(session, results_source, commission_source)
    original = session.scalar(select(Commission).where(Commission.gas_vybory_id == "1001"))
    assert original is not None
    session.add(
        Commission(
            source_artifact_id=original.source_artifact_id,
            source_record_id="heuristic-duplicate-number",
            gas_vybory_id="2001",
            parent_id=original.parent_id,
            type=original.type,
            region=original.region,
            name="UIK 1 duplicate",
            number="1",
            raw_json={},
        )
    )
    session.add(
        GasIdResolution(
            result_record_id=physical.id,
            status="resolved",
            gas_vybory_id="1001",
            commission_source_artifact_id=original.source_artifact_id,
            evidence_json={"source_artifact_id": 123},
        )
    )
    physical.gas_vybory_id = "1001"
    session.flush()

    counts = match_results(session)
    evidence = list(
        session.scalars(select(MatchEvidence).where(MatchEvidence.result_record_id == physical.id))
    )
    assert counts[MatchStatus.MATCHED.value] == 1
    assert physical.matched_commission_id == original.id
    assert [(item.method, item.selected) for item in evidence] == [("gas_vybory_id", True)]
    assert evidence[0].evidence_json["selection_decision"] == "selected_unique_exact_candidate"


def test_resolution_conflict_is_not_resolved_heuristically(
    session, results_source, commission_source
) -> None:
    physical = _prepared(session, results_source, commission_source)
    session.add(
        GasIdResolution(
            result_record_id=physical.id,
            status="conflict",
            reason_code="conflicting_commission_ids",
            evidence_json={"conflicting_ids": ["1001", "1002"]},
        )
    )
    session.flush()

    counts = match_results(session)
    assert counts[MatchStatus.DATA_INTEGRITY_ERROR.value] == 1
    assert physical.matched_commission_id is None
    audit_evidence = session.scalars(
        select(MatchEvidence).where(MatchEvidence.result_record_id == physical.id)
    ).all()
    assert audit_evidence == []
