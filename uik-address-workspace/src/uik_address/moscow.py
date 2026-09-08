"""Evidence-preserving 2026 Moscow polling-place lookup by exact UIK number."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import requests

from .io import write_jsonl
from .models import BackboneRow, CommissionContact, SourceEvidence
from .regional import FetchResponse, RequestsFetcher

MOSCOW_SUBJECT_CODE = "77"
MOSCOW_ELECTION_DATE = "2026-09-20"
MOSCOW_ENDPOINT = "https://www.mosgorizbirkom.ru/pollingstation-search-service/api/findByNumber"


class Fetcher(Protocol):
    def fetch(self, url: str) -> FetchResponse: ...


def _clean(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def _lookup_url(number: int) -> str:
    return f"{MOSCOW_ENDPOINT}?{urllib.parse.urlencode({'number': number, 'config': 1})}"


def _coerce_response(value: object, requested_url: str) -> FetchResponse:
    if isinstance(value, FetchResponse):
        return value
    if isinstance(value, Mapping):
        body = value.get("body", value.get("content", b""))
        if isinstance(body, str):
            body = body.encode("utf-8")
        return FetchResponse(
            url=str(value.get("url") or value.get("final_url") or requested_url),
            status=int(value.get("status") or value.get("status_code") or 0),
            body=bytes(body),
            content_type=str(value.get("content_type") or ""),
            retrieved_at=str(value.get("retrieved_at") or ""),
        )
    raise TypeError("fetcher must return FetchResponse or a response mapping")


def _fetch(fetcher: Fetcher | Callable[[str], object], url: str) -> FetchResponse:
    method = getattr(fetcher, "fetch", None)
    value = method(url) if callable(method) else fetcher(url)  # type: ignore[operator]
    return _coerce_response(value, url)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _store_payload(output_dir: Path, payload: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(payload).hexdigest()
    relative = Path("raw") / "sha256" / digest[:2] / digest
    path = output_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise OSError(f"corrupt content-addressed artifact: {path}")
    else:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.part")
        try:
            temporary.write_bytes(payload)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return digest, relative.as_posix()


def _address(value: Mapping[str, object], *, voting: bool) -> str:
    if voting:
        fields = (
            "localityVotingInstitution",
            "votingInstitutionStreet",
            "votingInstitutionHouse",
            "votingInstitutionName",
        )
    else:
        fields = ("locality", "street", "house", "institutionName")
    return ", ".join(part for field in fields if (part := _clean(value.get(field))))


def parse_moscow_response(
    payload: bytes,
    *,
    requested_number: int,
    url: str,
    retrieved_at: str,
    status: int = 200,
) -> tuple[CommissionContact | None, str]:
    """Parse a Moscow response, rejecting records not proven current for Duma 2026."""

    if status != 200:
        return None, f"HTTP {status}"
    try:
        value = json.loads(payload.decode("utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError) as error:
        return None, f"invalid JSON: {error}"
    if not isinstance(value, Mapping):
        return None, "response is not an object"
    try:
        returned_number = int(value.get("number") or 0)
    except (TypeError, ValueError):
        returned_number = 0
    if returned_number != requested_number:
        return None, f"returned UIK {returned_number or 'missing'}"
    voting_date = _clean(value.get("votingDate"))[:10]
    if voting_date != MOSCOW_ELECTION_DATE:
        return None, f"votingDate is {voting_date or 'missing'}, not {MOSCOW_ELECTION_DATE}"
    voting_address = _address(value, voting=True)
    if not voting_address:
        return None, "polling-place address is missing"
    digest = hashlib.sha256(payload).hexdigest()
    source = SourceEvidence(url, retrieved_at, digest, status, "moscow_api_2026")
    return (
        CommissionContact(
            subject_code=MOSCOW_SUBJECT_CODE,
            commission_type="uik",
            commission_number=requested_number,
            commission_name=f"УИК №{requested_number}",
            external_id=_clean(value.get("id")),
            commission_address=_address(value, voting=False),
            commission_phone=_clean(value.get("phoneNumber")),
            voting_address=voting_address,
            voting_phone=_clean(value.get("votingInstitutionPhone")),
            source=source,
        ),
        "",
    )


def _load_verified_cache(output_dir: Path) -> dict[int, dict[str, Any]]:
    manifest_path = output_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}
    try:
        artifacts = json.loads(manifest_path.read_text(encoding="utf-8")).get("artifacts", [])
    except (json.JSONDecodeError, AttributeError):
        return {}
    result: dict[int, dict[str, Any]] = {}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            continue
        try:
            number = int(artifact.get("uikNumber") or 0)
        except (TypeError, ValueError):
            continue
        raw_path = str(artifact.get("rawPath") or "")
        path = output_dir / raw_path if raw_path else None
        status = int(artifact.get("status") or 0)
        retryable = status in {408, 425, 429} or status >= 500
        if not number or not path or not path.is_file() or status <= 0 or retryable:
            continue
        payload = path.read_bytes()
        if hashlib.sha256(payload).hexdigest() != artifact.get("sha256"):
            continue
        result[number] = artifact
    return result


def _manifest(artifacts: Iterable[dict[str, Any]]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "subjectCode": MOSCOW_SUBJECT_CODE,
        "electionDate": MOSCOW_ELECTION_DATE,
        "endpoint": MOSCOW_ENDPOINT,
        "artifacts": sorted(artifacts, key=lambda item: int(item["uikNumber"])),
    }


def crawl_moscow_contacts(
    backbone: Iterable[BackboneRow],
    output_dir: Path,
    *,
    proxy_url: str | None = None,
    timeout: float = 30.0,
    concurrency: int = 4,
    refresh: bool = False,
    request_limit: int | None = None,
    fetcher: Fetcher | Callable[[str], object] | None = None,
    max_attempts: int = 4,
    retry_backoff: float = 0.5,
) -> dict[str, Any]:
    """Look up all Moscow backbone UIKs with restart-safe, hashed response evidence."""

    if not 1 <= concurrency <= 32:
        raise ValueError("concurrency must be between 1 and 32")
    if request_limit is not None and request_limit < 0:
        raise ValueError("request_limit must be non-negative")
    if not 1 <= max_attempts <= 10 or retry_backoff < 0:
        raise ValueError("unsafe retry settings")
    numbers = sorted(
        {
            row.uik_number
            for row in backbone
            if row.subject_code == MOSCOW_SUBJECT_CODE and row.uik_number > 0
        }
    )
    cache = {} if refresh else _load_verified_cache(output_dir)
    artifacts = dict(cache)
    pending = [number for number in numbers if number not in cache]
    if request_limit is not None:
        pending = pending[:request_limit]
    transport = fetcher or RequestsFetcher(
        proxy_url=proxy_url,
        timeout=timeout,
        concurrency=concurrency,
    )

    def retrieve(number: int) -> tuple[int, FetchResponse | None, str]:
        url = _lookup_url(number)
        for attempt in range(max_attempts):
            try:
                response = _fetch(transport, url)
            except requests.RequestException as error:
                if attempt + 1 == max_attempts:
                    return number, None, f"{type(error).__name__}: {error}"
                time.sleep(retry_backoff * 2**attempt)
                continue
            if not response.retrieved_at:
                response = FetchResponse(
                    response.url,
                    response.status,
                    response.body,
                    response.content_type,
                    datetime.now(UTC).isoformat(),
                )
            retryable = response.status in {408, 425, 429} or response.status >= 500
            if not retryable or attempt + 1 == max_attempts:
                return number, response, ""
            time.sleep(retry_backoff * 2**attempt)
        raise AssertionError("retry loop must return")

    for offset in range(0, len(pending), concurrency):
        batch = pending[offset : offset + concurrency]
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(retrieve, batch))
        for number, response, error in results:
            requested_url = _lookup_url(number)
            if response is None:
                artifacts[number] = {
                    "uikNumber": number,
                    "requestedUrl": requested_url,
                    "url": requested_url,
                    "status": 0,
                    "error": error,
                    "parseStatus": "fetch_failed",
                    "parseReason": error,
                    "sha256": "",
                    "rawPath": "",
                }
                continue
            digest, raw_path = _store_payload(output_dir, response.body)
            contact, reason = parse_moscow_response(
                response.body,
                requested_number=number,
                url=response.url or requested_url,
                retrieved_at=response.retrieved_at,
                status=response.status,
            )
            artifacts[number] = {
                "uikNumber": number,
                "requestedUrl": requested_url,
                "url": response.url or requested_url,
                "status": response.status,
                "contentType": response.content_type,
                "retrievedAt": response.retrieved_at,
                "sha256": digest,
                "byteLength": len(response.body),
                "rawPath": raw_path,
                "parseStatus": "parsed" if contact else "unresolved",
                "parseReason": reason,
                "contactCount": int(contact is not None),
            }
        _write_json(output_dir / "manifest.json", _manifest(artifacts.values()))

    contacts: list[CommissionContact] = []
    for number in numbers:
        artifact = artifacts.get(number)
        if not artifact or artifact.get("parseStatus") != "parsed":
            continue
        path = output_dir / str(artifact["rawPath"])
        if not path.is_file():
            continue
        contact, _ = parse_moscow_response(
            path.read_bytes(),
            requested_number=number,
            url=str(artifact.get("url") or artifact.get("requestedUrl") or _lookup_url(number)),
            retrieved_at=str(artifact.get("retrievedAt") or ""),
            status=int(artifact.get("status") or 0),
        )
        if contact:
            contacts.append(contact)

    _write_json(output_dir / "manifest.json", _manifest(artifacts.values()))
    write_jsonl(output_dir / "contacts.jsonl", (contact.to_dict() for contact in contacts))
    counts = Counter(str(item.get("parseStatus") or "") for item in artifacts.values())
    completed_numbers = {
        number
        for number, artifact in artifacts.items()
        if number in numbers
        and int(artifact.get("status") or 0) > 0
        and int(artifact.get("status") or 0) not in {408, 425, 429}
        and int(artifact.get("status") or 0) < 500
    }
    summary = {
        "subjectCode": MOSCOW_SUBJECT_CODE,
        "electionDate": MOSCOW_ELECTION_DATE,
        "backboneUiks": len(numbers),
        "artifacts": len(artifacts),
        "networkRequests": len(pending),
        "cachedResponses": len(cache),
        "contacts": len(contacts),
        "parsed": counts["parsed"],
        "unresolved": counts["unresolved"],
        "fetchFailures": counts["fetch_failed"],
        "remaining": len(numbers) - len(completed_numbers),
        "complete": len(completed_numbers) == len(numbers),
    }
    _write_json(output_dir / "summary.json", summary)
    return summary
