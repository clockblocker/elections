from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, BinaryIO

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from elections.models import (
    Ballot,
    Commission,
    GasIdResolution,
    MatchStatus,
    ResultRecord,
    SourceArtifact,
)

UIK_RE = re.compile(
    r"(?:\bУИК\b|участков(?:ая|ой)\s+избирательн(?:ая|ой)\s+комисси[яи])"
    r"\s*(?:№|N)?\s*(\d+)",
    re.IGNORECASE,
)
RESOLVED_STATUSES = {"resolved", "missing_commission"}
INTEGRITY_REASONS = {
    "conflicting_uik_links",
    "conflicting_commission_ids",
    "protocol_conflict",
    "resolved_id_changed",
}


class GasResolutionError(ValueError):
    pass


class _AnchorParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        values = {key.casefold(): value for key, value in attrs}
        self._href = values.get("href")
        self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None
            self._text = []


def _links(html: str) -> list[tuple[str, str]]:
    parser = _AnchorParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:
        raise GasResolutionError(f"malformed HTML: {exc}") from exc
    return parser.links


def _absolute_url(base_url: str, href: str) -> str:
    if href.startswith("region/"):
        parts = urllib.parse.urlsplit(base_url)
        return urllib.parse.urljoin(
            urllib.parse.urlunsplit((parts.scheme, parts.netloc, "/", "", "")), href
        )
    return urllib.parse.urljoin(base_url, href)


def _result_tree_links(html: str) -> list[tuple[str, str]]:
    marker = "tvdTreeJson ="
    document: Any | None = None
    for line in html.splitlines():
        if marker not in line:
            continue
        payload = line.split(marker, 1)[1].strip().removesuffix(";")
        if not payload or payload == '""':
            continue
        try:
            document = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise GasResolutionError(f"malformed tvdTreeJson: {exc}") from exc
    if document is None:
        return []

    found: list[tuple[str, str]] = []

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                visit(child)
            return
        if not isinstance(node, dict):
            return
        if node.get("isUik") is True and isinstance(node.get("href"), str):
            found.append((node["href"], str(node.get("text", ""))))
        visit(node.get("children", []))

    visit(document)
    return found


def parse_parent_results_page(html: str, base_url: str) -> dict[str, list[str]]:
    """Return exact UIK-number-to-detail-link candidates from a GAS result table."""

    found: dict[str, set[str]] = defaultdict(set)
    for href, text in [*_links(html), *_result_tree_links(html)]:
        match = UIK_RE.search(text)
        if not match:
            continue
        url = _absolute_url(base_url, href)
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        if query.get("action") != ["show"]:
            continue
        found[str(int(match.group(1)))].add(url)
    return {number: sorted(urls) for number, urls in sorted(found.items(), key=lambda x: int(x[0]))}


def parse_commission_id_page(html: str, base_url: str) -> tuple[str, str]:
    """Extract the commission archive ID from an explicit ``action=ik&vrn=`` link."""

    candidates: dict[str, set[str]] = defaultdict(set)

    def add_candidate(url: str) -> None:
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        values = query.get("vrn", [])
        if query.get("action") != ["ik"] or len(values) != 1:
            return
        value = values[0].strip()
        if value.isdigit():
            candidates[value].add(url)

    add_candidate(base_url)
    for href, _text in _links(html):
        add_candidate(_absolute_url(base_url, href))
    if not candidates:
        raise GasResolutionError("commission_link_not_found")
    if len(candidates) != 1:
        raise GasResolutionError("conflicting_commission_ids")
    gas_id = next(iter(candidates))
    return gas_id, sorted(candidates[gas_id])[0]


def canonical_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.urlencode(
        sorted(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    )
    return urllib.parse.urlunsplit(
        (parts.scheme.casefold(), parts.netloc.casefold(), parts.path, query, "")
    )


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class FetchResult:
    source_url: str
    canonical_url: str
    status: str
    path: Path | None
    sha256: str | None
    size_bytes: int | None
    retrieved_at: str | None
    final_url: str | None
    media_type: str | None
    http_status: int | None
    attempts: int
    error: str | None = None

    def text(self) -> str:
        if self.path is None:
            raise GasResolutionError(self.error or "response has no cached body")
        payload = self.path.read_bytes()
        for encoding in ("utf-8", "windows-1251"):
            try:
                return payload.decode(encoding)
            except UnicodeDecodeError:
                pass
        return payload.decode("utf-8", errors="replace")


class GasResponseCache:
    """Checksum-verified, resumable response cache with bounded polite fetching."""

    def __init__(
        self,
        root: Path,
        *,
        timeout: float = 30,
        retries: int = 3,
        rate_limit_seconds: float = 0.5,
        offline: bool = False,
        opener: Callable[..., BinaryIO] = urllib.request.urlopen,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = root
        self.timeout = timeout
        self.retries = max(1, retries)
        self.rate_limit_seconds = max(0.0, rate_limit_seconds)
        self.offline = offline
        self.opener = opener
        self.sleeper = sleeper
        self.manifest_path = root / "manifest.json"
        self._manifest_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._last_request = 0.0
        self.root.mkdir(parents=True, exist_ok=True)
        self._manifest = self._load_manifest()

    def _load_manifest(self) -> dict[str, dict[str, Any]]:
        if not self.manifest_path.exists():
            return {}
        try:
            document = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GasResolutionError(f"cannot read cache manifest {self.manifest_path}: {exc}")
        if document.get("schema_version") != 1 or not isinstance(document.get("responses"), dict):
            raise GasResolutionError(f"unsupported cache manifest {self.manifest_path}")
        return document["responses"]

    def _save_manifest(self) -> None:
        document = {"schema_version": 1, "responses": dict(sorted(self._manifest.items()))}
        temporary = self.manifest_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.manifest_path)

    def _cached(self, url: str) -> FetchResult | None:
        key = canonical_url(url)
        with self._manifest_lock:
            record = self._manifest.get(key)
        if not record or record.get("status") not in {"downloaded", "cached"}:
            return None
        relative = Path(str(record.get("path", "")))
        if relative.is_absolute() or ".." in relative.parts:
            raise GasResolutionError(f"cached path escapes cache directory: {relative}")
        path = self.root / relative
        if not path.is_file():
            return None
        payload = path.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        if digest != record.get("sha256") or len(payload) != record.get("size_bytes"):
            return None
        return FetchResult(
            source_url=url,
            canonical_url=key,
            status="cached",
            path=path,
            sha256=digest,
            size_bytes=len(payload),
            retrieved_at=record.get("retrieved_at"),
            final_url=record.get("final_url"),
            media_type=record.get("media_type"),
            http_status=record.get("http_status"),
            attempts=0,
        )

    def _wait_for_rate_limit(self) -> None:
        with self._rate_lock:
            delay = self.rate_limit_seconds - (time.monotonic() - self._last_request)
            if delay > 0:
                self.sleeper(delay)
            self._last_request = time.monotonic()

    def _store(self, url: str, payload: bytes, metadata: dict[str, Any]) -> FetchResult:
        key = canonical_url(url)
        url_digest = hashlib.sha256(key.encode()).hexdigest()
        path = self.root / "responses" / url_digest[:2] / f"{url_digest}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".html.tmp")
        temporary.write_bytes(payload)
        temporary.replace(path)
        digest = hashlib.sha256(payload).hexdigest()
        record = {
            **metadata,
            "source_url": url,
            "path": path.relative_to(self.root).as_posix(),
            "sha256": digest,
            "size_bytes": len(payload),
        }
        with self._manifest_lock:
            self._manifest[key] = record
            self._save_manifest()
        return FetchResult(
            source_url=url,
            canonical_url=key,
            status=record["status"],
            path=path,
            sha256=digest,
            size_bytes=len(payload),
            retrieved_at=record["retrieved_at"],
            final_url=record.get("final_url"),
            media_type=record.get("media_type"),
            http_status=record.get("http_status"),
            attempts=int(record["attempts"]),
            error=record.get("error"),
        )

    def fetch(self, url: str) -> FetchResult:
        cached = self._cached(url)
        if cached is not None:
            return cached
        if self.offline:
            return FetchResult(
                source_url=url,
                canonical_url=canonical_url(url),
                status="failed",
                path=None,
                sha256=None,
                size_bytes=None,
                retrieved_at=None,
                final_url=None,
                media_type=None,
                http_status=None,
                attempts=0,
                error="offline_cache_miss",
            )

        last_error: str | None = None
        for attempt in range(1, self.retries + 1):
            self._wait_for_rate_limit()
            request = urllib.request.Request(
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml",
                    "User-Agent": "elections-workbench/0.1 (+exact GAS identity resolver)",
                },
            )
            try:
                response = self.opener(request, timeout=self.timeout)
                with response:
                    payload = response.read()
                    status = getattr(response, "status", None)
                    final_url = response.geturl()
                    media_type = (
                        response.headers.get_content_type()
                        if getattr(response, "headers", None)
                        else "application/octet-stream"
                    )
                if status is not None and status >= 400:
                    raise urllib.error.HTTPError(url, status, "HTTP error", {}, None)
                return self._store(
                    url,
                    payload,
                    {
                        "status": "downloaded",
                        "retrieved_at": _utc_now(),
                        "final_url": final_url,
                        "media_type": media_type,
                        "http_status": status,
                        "attempts": attempt,
                        "error": None,
                    },
                )
            except urllib.error.HTTPError as exc:
                last_error = f"http_{exc.code}"
                body = exc.read() if exc.fp is not None else b""
                retryable = exc.code in {408, 425, 429} or exc.code >= 500
                if not retryable or attempt == self.retries:
                    if body:
                        return self._store(
                            url,
                            body,
                            {
                                "status": "failed",
                                "retrieved_at": _utc_now(),
                                "final_url": exc.geturl(),
                                "media_type": "text/html",
                                "http_status": exc.code,
                                "attempts": attempt,
                                "error": last_error,
                            },
                        )
                    break
            except (OSError, TimeoutError, urllib.error.URLError) as exc:
                last_error = f"network_error:{type(exc).__name__}:{exc}"
            if attempt < self.retries:
                self.sleeper(min(2 ** (attempt - 1), 8))

        key = canonical_url(url)
        failed = {
            "status": "failed",
            "source_url": url,
            "retrieved_at": _utc_now(),
            "attempts": attempt,
            "error": last_error or "unknown_fetch_error",
        }
        with self._manifest_lock:
            self._manifest[key] = failed
            self._save_manifest()
        return FetchResult(
            source_url=url,
            canonical_url=key,
            status="failed",
            path=None,
            sha256=None,
            size_bytes=None,
            retrieved_at=failed["retrieved_at"],
            final_url=None,
            media_type=None,
            http_status=None,
            attempts=attempt,
            error=failed["error"],
        )

    def fetch_many(self, urls: Iterable[str], concurrency: int) -> dict[str, FetchResult]:
        unique = sorted(set(urls), key=canonical_url)
        with ThreadPoolExecutor(max_workers=max(1, concurrency)) as executor:
            results = executor.map(self.fetch, unique)
        return {result.source_url: result for result in results}


@dataclass(frozen=True)
class _ResultInput:
    id: int
    source_artifact_id: int
    ballot_id: int
    ballot_kind: str
    region: str
    tik: str | None
    uik: str | None
    source_url: str | None
    previous_match_status: str
    existing_gas_id: str | None


@dataclass
class _Outcome:
    status: str
    reason: str | None
    gas_id: str | None
    parent_url: str | None
    detail_url: str | None
    commission_url: str | None = None
    parent_artifact_id: int | None = None
    detail_artifact_id: int | None = None
    evidence: dict[str, Any] | None = None


def _artifact_for_fetch(session: Session, result: FetchResult) -> SourceArtifact | None:
    if result.path is None or result.sha256 is None:
        return None
    url_digest = hashlib.sha256(result.canonical_url.encode()).hexdigest()[:20]
    key = f"gas-response-{url_digest}-{result.sha256[:20]}"
    artifact = session.scalar(select(SourceArtifact).where(SourceArtifact.key == key))
    retrieved_at = (
        datetime.fromisoformat(result.retrieved_at.replace("Z", "+00:00"))
        if result.retrieved_at
        else None
    )
    metadata = {
        "cache_status": result.status,
        "canonical_url": result.canonical_url,
        "final_url": result.final_url,
        "http_status": result.http_status,
        "attempts": result.attempts,
        "error": result.error,
    }
    if artifact is None:
        artifact = SourceArtifact(
            key=key,
            url=result.source_url,
            sha256=result.sha256,
            retrieved_at=retrieved_at,
            size_bytes=result.size_bytes,
            media_type=result.media_type,
            local_path=str(result.path),
            metadata_json=metadata,
        )
        session.add(artifact)
        session.flush()
    return artifact


def _upsert_resolution(
    session: Session,
    record: _ResultInput,
    outcome: _Outcome,
    commission_artifact_id: int,
    existing: GasIdResolution | None,
    result: ResultRecord,
) -> None:
    # A transient forced rerun must not destroy a previously established exact identity.
    if (
        existing is not None
        and existing.status in RESOLVED_STATUSES
        and outcome.status == "unresolved"
    ):
        return
    if existing is None:
        existing = GasIdResolution(result_record_id=record.id)
        session.add(existing)
    existing.status = outcome.status
    existing.reason_code = outcome.reason
    existing.gas_vybory_id = outcome.gas_id
    existing.parent_source_artifact_id = outcome.parent_artifact_id
    existing.detail_source_artifact_id = outcome.detail_artifact_id
    existing.commission_source_artifact_id = commission_artifact_id
    existing.evidence_json = outcome.evidence or {}
    result.gas_vybory_id = outcome.gas_id if outcome.status in RESOLVED_STATUSES else None


def _reason_from_parser(error: GasResolutionError, fallback: str) -> str:
    reason = str(error)
    if reason == "commission_link_not_found":
        return "no_explicit_commission_id"
    return reason if reason in INTEGRITY_REASONS else fallback


def resolve_gas_ids(
    session: Session,
    *,
    cache_dir: Path,
    report_path: Path,
    human_report_path: Path,
    commission_artifact_key: str = "commissions-2021-09-14",
    timeout: float = 30,
    retries: int = 3,
    rate_limit_seconds: float = 0.5,
    concurrency: int = 4,
    offline: bool = False,
    max_parent_urls: int | None = None,
    cache: GasResponseCache | None = None,
) -> dict[str, Any]:
    commission_artifact = session.scalar(
        select(SourceArtifact).where(SourceArtifact.key == commission_artifact_key)
    )
    if commission_artifact is None:
        raise GasResolutionError(
            f"commission source artifact {commission_artifact_key!r} is not imported"
        )

    rows = session.execute(
        select(ResultRecord, Ballot.kind).join(Ballot, Ballot.id == ResultRecord.ballot_id)
    ).all()
    result_models = {record.id: record for record, _kind in rows}
    existing_resolutions = {
        resolution.result_record_id: resolution
        for resolution in session.scalars(select(GasIdResolution))
    }
    records = [
        _ResultInput(
            id=record.id,
            source_artifact_id=record.source_artifact_id,
            ballot_id=record.ballot_id,
            ballot_kind=kind.value,
            region=record.region_name,
            tik=record.tik_name,
            uik=(
                str(int(record.uik_number))
                if record.uik_number and record.uik_number.isdigit()
                else record.uik_number
            ),
            source_url=record.source_url,
            previous_match_status=record.match_status.value,
            existing_gas_id=record.gas_vybory_id,
        )
        for record, kind in rows
    ]
    parent_urls = sorted(
        {item.source_url for item in records if item.source_url}, key=canonical_url
    )
    if max_parent_urls is not None:
        parent_urls = parent_urls[: max(0, max_parent_urls)]
    selected_parent_urls = set(parent_urls)
    considered = (
        records
        if max_parent_urls is None
        else [item for item in records if item.source_url in selected_parent_urls]
    )
    outcomes: dict[int, _Outcome] = {}
    for item in considered:
        if not item.source_url:
            outcomes[item.id] = _Outcome("unresolved", "missing_source_url", None, None, None)
        elif not item.uik:
            outcomes[item.id] = _Outcome(
                "unresolved", "missing_uik_number", None, item.source_url, None
            )

    response_cache = cache or GasResponseCache(
        cache_dir,
        timeout=timeout,
        retries=retries,
        rate_limit_seconds=rate_limit_seconds,
        offline=offline,
    )
    parent_fetches = response_cache.fetch_many(parent_urls, concurrency)
    artifacts: dict[tuple[str, str | None], int | None] = {}
    for fetch in parent_fetches.values():
        artifact = _artifact_for_fetch(session, fetch)
        artifacts[(fetch.source_url, fetch.sha256)] = artifact.id if artifact else None

    parsed_parents: dict[str, dict[str, list[str]]] = {}
    for url, fetch in parent_fetches.items():
        if fetch.status == "failed":
            continue
        try:
            parsed_parents[url] = parse_parent_results_page(fetch.text(), fetch.final_url or url)
        except GasResolutionError:
            parsed_parents[url] = {}

    detail_to_records: dict[str, list[_ResultInput]] = defaultdict(list)
    for item in considered:
        if item.id in outcomes:
            continue
        assert item.source_url is not None and item.uik is not None
        fetch = parent_fetches[item.source_url]
        parent_artifact_id = artifacts.get((fetch.source_url, fetch.sha256))
        if fetch.status == "failed":
            outcomes[item.id] = _Outcome(
                "unresolved",
                "parent_fetch_failed",
                None,
                item.source_url,
                None,
                parent_artifact_id=parent_artifact_id,
                evidence={"fetch_error": fetch.error, "attempts": fetch.attempts},
            )
            continue
        candidates = parsed_parents.get(item.source_url, {}).get(item.uik, [])
        if not candidates:
            reason = (
                "malformed_parent"
                if not parsed_parents.get(item.source_url)
                else "uik_link_not_found"
            )
            outcomes[item.id] = _Outcome(
                "unresolved",
                reason,
                None,
                item.source_url,
                None,
                parent_artifact_id=parent_artifact_id,
                evidence={"uik_number": item.uik},
            )
        elif len(candidates) > 1:
            outcomes[item.id] = _Outcome(
                "conflict",
                "conflicting_uik_links",
                None,
                item.source_url,
                None,
                parent_artifact_id=parent_artifact_id,
                evidence={"uik_number": item.uik, "detail_urls": candidates},
            )
        else:
            detail_to_records[candidates[0]].append(item)

    detail_fetches = response_cache.fetch_many(detail_to_records, concurrency)
    for fetch in detail_fetches.values():
        artifact = _artifact_for_fetch(session, fetch)
        artifacts[(fetch.source_url, fetch.sha256)] = artifact.id if artifact else None

    commission_ids = defaultdict(list)
    for commission_id, gas_id in session.execute(
        select(Commission.id, Commission.gas_vybory_id).where(
            Commission.source_artifact_id == commission_artifact.id,
            Commission.gas_vybory_id.is_not(None),
        )
    ):
        if gas_id is not None and gas_id.isdigit() and int(gas_id) > 0:
            commission_ids[gas_id].append(commission_id)

    for detail_url, linked_records in detail_to_records.items():
        fetch = detail_fetches[detail_url]
        detail_artifact_id = artifacts.get((fetch.source_url, fetch.sha256))
        for item in linked_records:
            assert item.source_url is not None
            parent = parent_fetches[item.source_url]
            parent_artifact_id = artifacts.get((parent.source_url, parent.sha256))
            if fetch.status == "failed":
                outcomes[item.id] = _Outcome(
                    "unresolved",
                    "detail_fetch_failed",
                    None,
                    item.source_url,
                    detail_url,
                    parent_artifact_id=parent_artifact_id,
                    detail_artifact_id=detail_artifact_id,
                    evidence={"fetch_error": fetch.error, "attempts": fetch.attempts},
                )
                continue
            try:
                gas_id, commission_url = parse_commission_id_page(
                    fetch.text(), fetch.final_url or detail_url
                )
            except GasResolutionError as exc:
                reason = _reason_from_parser(exc, "malformed_detail")
                outcomes[item.id] = _Outcome(
                    "conflict" if reason in INTEGRITY_REASONS else "unresolved",
                    reason,
                    None,
                    item.source_url,
                    detail_url,
                    parent_artifact_id=parent_artifact_id,
                    detail_artifact_id=detail_artifact_id,
                    evidence={"parser_error": str(exc)},
                )
                continue
            exact_candidates = commission_ids.get(gas_id, [])
            status = "resolved" if len(exact_candidates) == 1 else "missing_commission"
            reason = None if exact_candidates else "id_absent_from_commission_snapshot"
            if len(exact_candidates) > 1:
                status, reason = "conflict", "duplicate_id_in_commission_snapshot"
            outcomes[item.id] = _Outcome(
                status,
                reason,
                gas_id if status in RESOLVED_STATUSES else None,
                item.source_url,
                detail_url,
                commission_url=commission_url,
                parent_artifact_id=parent_artifact_id,
                detail_artifact_id=detail_artifact_id,
                evidence={
                    "commission_link": commission_url,
                    "commission_parameter": "action=ik&vrn",
                    "commission_candidate_ids": exact_candidates,
                    "selection": (
                        "exact_identifier_equality" if len(exact_candidates) == 1 else None
                    ),
                },
            )

    protocol_groups: dict[tuple[int, str, str | None, str | None], list[int]] = defaultdict(list)
    for item in considered:
        protocol_groups[(item.ballot_id, item.region, item.tik, item.uik)].append(item.id)
    for ids in protocol_groups.values():
        resolved_ids = {outcomes[item_id].gas_id for item_id in ids if outcomes[item_id].gas_id}
        if len(resolved_ids) > 1:
            for item_id in ids:
                outcome = outcomes[item_id]
                outcome.status = "conflict"
                outcome.reason = "protocol_conflict"
                outcome.gas_id = None
                outcome.evidence = {
                    **(outcome.evidence or {}),
                    "conflicting_ids": sorted(resolved_ids),
                }

    for item in considered:
        outcome = outcomes[item.id]
        if item.existing_gas_id and outcome.gas_id and item.existing_gas_id != outcome.gas_id:
            outcome.status = "conflict"
            outcome.reason = "resolved_id_changed"
            outcome.evidence = {
                **(outcome.evidence or {}),
                "existing_gas_id": item.existing_gas_id,
                "new_gas_id": outcome.gas_id,
            }
            outcome.gas_id = None
        _upsert_resolution(
            session,
            item,
            outcome,
            commission_artifact.id,
            existing_resolutions.get(item.id),
            result_models[item.id],
        )
    session.flush()

    request_results = list(parent_fetches.values()) + list(detail_fetches.values())
    request_counts = Counter(item.status for item in request_results)
    reason_counts = Counter(
        outcome.reason or outcome.status
        for outcome in outcomes.values()
        if outcome.status != "resolved"
    )
    representative_urls: dict[str, list[str]] = defaultdict(list)
    for outcome in outcomes.values():
        if outcome.status == "resolved":
            continue
        reason = outcome.reason or outcome.status
        candidate = outcome.detail_url or outcome.parent_url
        if candidate and candidate not in representative_urls[reason]:
            representative_urls[reason].append(candidate)

    exact_resolved = sum(outcome.status in RESOLVED_STATUSES for outcome in outcomes.values())
    exact_links = sum(outcome.status == "resolved" for outcome in outcomes.values())
    total_exact_resolved = int(
        session.scalar(
            select(func.count(ResultRecord.id)).where(ResultRecord.gas_vybory_id.is_not(None))
        )
        or 0
    )
    total_exact_links = int(
        session.scalar(
            select(func.count(GasIdResolution.result_record_id)).where(
                GasIdResolution.status == "resolved"
            )
        )
        or 0
    )
    ambiguous_before = sum(
        item.previous_match_status == MatchStatus.AMBIGUOUS.value for item in considered
    )
    newly_exact_ambiguous = sum(
        item.previous_match_status == MatchStatus.AMBIGUOUS.value
        and outcomes[item.id].status == "resolved"
        for item in considered
    )

    coverage_dimensions: dict[str, list[dict[str, Any]]] = {}
    for name, key in (
        ("region", lambda item: item.region),
        ("ballot_kind", lambda item: item.ballot_kind),
    ):
        buckets: dict[str, list[_Outcome]] = defaultdict(list)
        for item in considered:
            buckets[str(key(item))].append(outcomes[item.id])
        coverage_dimensions[name] = [
            {
                name: bucket,
                "total": len(values),
                "exact_ids": sum(value.status in RESOLVED_STATUSES for value in values),
            }
            for bucket, values in sorted(buckets.items())
        ]
    special_buckets: dict[str, list[_Outcome]] = defaultdict(list)
    special_by_id = {record.id: record.special_type.value for record, _kind in rows}
    for item in considered:
        special_buckets[special_by_id[item.id]].append(outcomes[item.id])
    coverage_dimensions["special_type"] = [
        {
            "special_type": bucket,
            "total": len(values),
            "exact_ids": sum(value.status in RESOLVED_STATUSES for value in values),
        }
        for bucket, values in sorted(special_buckets.items())
    ]

    report_timestamp = max(
        (item.retrieved_at for item in request_results if item.retrieved_at),
        default=commission_artifact.retrieved_at.isoformat()
        if commission_artifact.retrieved_at
        else None,
    )
    report = {
        "schema_version": 1,
        "generated_at": report_timestamp,
        "commission_source_artifact": {
            "id": commission_artifact.id,
            "key": commission_artifact.key,
            "sha256": commission_artifact.sha256,
        },
        "summary": {
            "total_result_records": len(records),
            "result_records_considered": len(considered),
            "distinct_parent_urls": len(parent_urls),
            "exact_ids_resolved": total_exact_resolved,
            "exact_commission_links_established": total_exact_links,
            "exact_ids_resolved_in_scope": exact_resolved,
            "exact_commission_links_established_in_scope": exact_links,
            "cached_requests": request_counts["cached"],
            "downloaded_requests": request_counts["downloaded"],
            "failed_requests": request_counts["failed"],
            "ambiguous_heuristic_matches_before": ambiguous_before,
            "ambiguous_heuristic_matches_after_resolution": max(
                0, ambiguous_before - newly_exact_ambiguous
            ),
            "confidence_exact_identity": 1.0 if total_exact_resolved else None,
            "coverage_exact_identity": (
                total_exact_resolved / len(records) if records else 0.0
            ),
        },
        "unresolved_by_reason": dict(sorted(reason_counts.items())),
        "representative_failure_urls": {
            reason: sorted(urls)[:3] for reason, urls in sorted(representative_urls.items())
        },
        "coverage": coverage_dimensions,
        "conflicts": [
            {"result_record_id": item.id, "reason": outcomes[item.id].reason}
            for item in sorted(considered, key=lambda value: value.id)
            if outcomes[item.id].status == "conflict"
        ],
        "ids_missing_from_commission_snapshot": sorted(
            {
                outcome.gas_id
                for outcome in outcomes.values()
                if outcome.reason == "id_absent_from_commission_snapshot" and outcome.gas_id
            }
        ),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    summary = report["summary"]
    human_report_path.parent.mkdir(parents=True, exist_ok=True)
    human_report_path.write_text(
        "\n".join(
            [
                "# Exact GAS commission-ID resolution",
                "",
                f"Generated: {report['generated_at']}",
                "",
                f"- Result records: {summary['total_result_records']:,}",
                f"- Records considered: {summary['result_records_considered']:,}",
                f"- Exact IDs resolved: {summary['exact_ids_resolved']:,}",
                f"- Exact commission links: {summary['exact_commission_links_established']:,}",
                f"- Coverage: {summary['coverage_exact_identity']:.2%}",
                f"- Exact-identity confidence: {summary['confidence_exact_identity']}",
                f"- Requests cached/downloaded/failed: {summary['cached_requests']:,} / "
                f"{summary['downloaded_requests']:,} / {summary['failed_requests']:,}",
                "",
                "## Unresolved reasons",
                "",
                *(
                    [f"- `{reason}`: {count:,}" for reason, count in sorted(reason_counts.items())]
                    or ["- None"]
                ),
                "",
                "Confidence describes identifiers explicitly supplied by GAS; coverage is the "
                "separate fraction of considered records for which GAS supplied one.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return report
