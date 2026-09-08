"""Authenticated, resumable crawler for the public CEC commission directory.

The gateway uses a short JavaScript arithmetic challenge to mint an API key.
Authentication responses are intentionally never persisted: the raw store contains
only public directory/report responses and its manifest never contains headers.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import time
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import requests

from .io import write_jsonl
from .models import CommissionContact, SourceEvidence, canonical_region_code

DEFAULT_BASE_URL = "http://apps.cikrf.ru/service/ik-inp-service-pbcopy"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/140.0.0.0 Safari/537.36"
)
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
CHALLENGE_RE = re.compile(r"^\s*return\s+(-?\d+)\s*\+\s*(-?\d+)\s*;?\s*$")
NUMBER_RE = re.compile(r"(?:№|N)\s*(\d+)", re.IGNORECASE)
COMMISSION_TYPE_NAMES = {
    0: "organizing",
    1: "cik",
    2: "regional",
    3: "district",
    4: "tik",
    5: "uik",
}


class CecError(RuntimeError):
    """A safe-to-display CEC transport or response error."""


@dataclass(frozen=True, slots=True)
class CecConfig:
    base_url: str = DEFAULT_BASE_URL
    proxy_url: str | None = None
    timeout: float = 30.0
    retries: int = 5
    backoff_initial: float = 0.5
    backoff_max: float = 30.0
    page_size: int = 1_000
    user_agent: str = DEFAULT_USER_AGENT

    def __post_init__(self) -> None:
        if self.timeout <= 0:
            raise ValueError("timeout must be positive")
        if self.retries < 0:
            raise ValueError("retries cannot be negative")
        if self.page_size <= 0:
            raise ValueError("page_size must be positive")
        if self.backoff_initial < 0 or self.backoff_max < 0:
            raise ValueError("backoff values cannot be negative")


@dataclass(frozen=True, slots=True)
class StoredResponse:
    payload: bytes
    record: Mapping[str, Any]
    cache_hit: bool = False


@dataclass(frozen=True, slots=True)
class CrawlResult:
    contacts: tuple[CommissionContact, ...]
    summary: Mapping[str, Any]
    contacts_path: Path
    summary_path: Path
    manifest_path: Path


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        with temporary.open("wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _safe_error(error: BaseException, *secrets: str | None) -> str:
    message = f"{type(error).__name__}: {error}"
    for secret in secrets:
        if not secret:
            continue
        message = message.replace(secret, "<redacted>")
        parsed = urllib.parse.urlsplit(secret)
        for credential in (parsed.username, parsed.password):
            if credential:
                message = message.replace(credential, "***")
                message = message.replace(urllib.parse.quote(credential, safe=""), "***")
    return message


class RawResponseStore:
    """Content-addressed response bodies with an atomically rewritten index."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bodies = root / "sha256"
        self.manifest_path = root / "manifest.json"
        self._records = self._load()

    @staticmethod
    def identity(request_key: str) -> str:
        return _sha256(request_key.encode("utf-8"))

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.manifest_path.is_file():
            return {}
        value = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or not isinstance(value.get("requests", {}), dict):
            raise TypeError(f"invalid raw response manifest: {self.manifest_path}")
        return {str(key): dict(record) for key, record in value["requests"].items()}

    def _flush(self) -> None:
        value = {
            "schema_version": 1,
            "requests": dict(sorted(self._records.items())),
        }
        _atomic_write(
            self.manifest_path,
            json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
        )

    def verified(self, request_key: str) -> StoredResponse | None:
        record = self._records.get(self.identity(request_key))
        if not record or not record.get("sha256"):
            return None
        body_path = self.root / str(record.get("body_path") or "")
        if not body_path.is_file():
            return None
        payload = body_path.read_bytes()
        if _sha256(payload) != record["sha256"]:
            return None
        return StoredResponse(payload, record, cache_hit=True)

    def save(self, request_key: str, payload: bytes, metadata: Mapping[str, Any]) -> StoredResponse:
        digest = _sha256(payload)
        body_path = self.bodies / digest[:2] / digest
        if not body_path.is_file():
            _atomic_write(body_path, payload)
        record = {
            "request_id": self.identity(request_key),
            "request_key": request_key,
            **metadata,
            "byte_length": len(payload),
            "sha256": digest,
            "body_path": str(body_path.relative_to(self.root)),
        }
        self._records[record["request_id"]] = record
        self._flush()
        return StoredResponse(payload, record)

    def save_failure(self, request_key: str, metadata: Mapping[str, Any]) -> Mapping[str, Any]:
        record = {
            "request_id": self.identity(request_key),
            "request_key": request_key,
            **metadata,
        }
        self._records[record["request_id"]] = record
        self._flush()
        return record


class CecApi:
    """Current gateway client; the session argument makes it straightforward to fake."""

    def __init__(
        self,
        store: RawResponseStore,
        config: CecConfig | None = None,
        *,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        self.store = store
        self.config = config or CecConfig()
        self.session = session or requests.Session()
        self.sleep = sleep
        self.random = random_source or random.Random()
        self._api_key = ""

    @property
    def proxies(self) -> Mapping[str, str] | None:
        if not self.config.proxy_url:
            return None
        return {"http": self.config.proxy_url, "https": self.config.proxy_url}

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        base = self.config.base_url.rstrip("/")
        url = f"{base}/{path.lstrip('/')}"
        if params:
            query = urllib.parse.urlencode(
                sorted((str(key), str(value)) for key, value in params.items())
            )
            url = f"{url}?{query}"
        return url

    def _send(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        data: bytes | None = None,
    ) -> Any:
        request_headers = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": self.config.user_agent,
            **(headers or {}),
        }
        return self.session.request(
            method,
            url,
            headers=request_headers,
            data=data,
            timeout=self.config.timeout,
            proxies=self.proxies,
            allow_redirects=True,
        )

    @staticmethod
    def _status(response: Any) -> int:
        return int(getattr(response, "status_code", 0) or 0)

    @staticmethod
    def _payload(response: Any) -> bytes:
        payload = getattr(response, "content", b"")
        if isinstance(payload, str):
            return payload.encode("utf-8")
        return bytes(payload)

    def _authenticate(self, *, force: bool = False) -> str:
        if self._api_key and not force:
            return self._api_key
        challenge_url = self._url("/challenge/get")
        try:
            challenge_response = self._send("GET", challenge_url)
            status = self._status(challenge_response)
            if not 200 <= status < 300:
                raise CecError(f"challenge/get returned HTTP {status}")
            challenge = json.loads(self._payload(challenge_response))
            if not isinstance(challenge, dict):
                raise CecError("challenge/get returned a non-object")
            task = str(challenge.get("jsTask") or "")
            match = CHALLENGE_RE.fullmatch(task)
            if not match:
                raise CecError("challenge/get returned an unsupported arithmetic task")
            answer = str(int(match.group(1)) + int(match.group(2)))
            public_token = str(challenge.get("pubToken") or "")
            if not public_token:
                raise CecError("challenge/get returned no public token")
            solve_payload = _json_bytes(
                {
                    "pubToken": public_token,
                    "answer": answer,
                    "fingerprint": self.config.user_agent,
                }
            )
            solve_response = self._send(
                "POST",
                self._url("/challenge/solve"),
                headers={"Content-Type": "application/json"},
                data=solve_payload,
            )
            status = self._status(solve_response)
            if not 200 <= status < 300:
                raise CecError(f"challenge/solve returned HTTP {status}")
            solved = json.loads(self._payload(solve_response))
            self._api_key = str(solved.get("apiKey") or "") if isinstance(solved, dict) else ""
            if not self._api_key:
                raise CecError("challenge/solve returned no API key")
            return self._api_key
        except CecError:
            raise
        except (OSError, ValueError, TypeError, requests.RequestException) as error:
            raise CecError(_safe_error(error, self.config.proxy_url, self._api_key)) from error

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        refresh: bool = False,
    ) -> StoredResponse:
        method = method.upper()
        url = self._url(path, params)
        body_bytes = _json_bytes(body) if body is not None else None
        request_key = f"{method} {url}"
        if body_bytes is not None:
            request_key += f" body-sha256={_sha256(body_bytes)}"
        if not refresh and (cached := self.store.verified(request_key)):
            cached_status = int(cached.record.get("status") or 0)
            if cached_status not in RETRYABLE_STATUSES | {0, 401, 403}:
                return cached

        last_error = ""
        for attempt in range(self.config.retries + 1):
            started = time.monotonic()
            try:
                api_key = self._authenticate()
                response = self._send(
                    method,
                    url,
                    headers={
                        "X-Api-Key": api_key,
                        "X-Client-Fingerprint": self.config.user_agent,
                        **({"Content-Type": "application/json"} if body_bytes else {}),
                    },
                    data=body_bytes,
                )
                status = self._status(response)
                if status == 401 and attempt < self.config.retries:
                    self._authenticate(force=True)
                    continue
                payload = self._payload(response)
                result = self.store.save(
                    request_key,
                    payload,
                    {
                        "url": url,
                        "method": method,
                        "status": status,
                        "retrieved_at": _now(),
                        "content_type": str(
                            getattr(response, "headers", {}).get("Content-Type", "")
                        ),
                        "elapsed_seconds": round(time.monotonic() - started, 6),
                        "retry_count": attempt,
                    },
                )
                if status in RETRYABLE_STATUSES and attempt < self.config.retries:
                    self._backoff(attempt, getattr(response, "headers", {}))
                    continue
                return result
            except CecError as error:
                last_error = _safe_error(error, self.config.proxy_url, self._api_key)
            except (OSError, requests.RequestException) as error:
                last_error = _safe_error(error, self.config.proxy_url, self._api_key)
            if attempt < self.config.retries:
                self._backoff(attempt, {})
                continue
            self.store.save_failure(
                request_key,
                {
                    "url": url,
                    "method": method,
                    "retrieved_at": _now(),
                    "error": last_error,
                    "retry_count": attempt,
                },
            )
        raise CecError(last_error or f"request failed: {method} {url}")

    def _backoff(self, attempt: int, headers: Mapping[str, Any]) -> None:
        retry_after = headers.get("Retry-After") if headers else None
        try:
            delay = max(0.0, float(retry_after)) if retry_after is not None else None
        except (TypeError, ValueError):
            delay = None
        if delay is None:
            maximum = min(
                self.config.backoff_max,
                self.config.backoff_initial * (2**attempt),
            )
            delay = self.random.uniform(maximum / 2, maximum) if maximum else 0.0
        self.sleep(delay)

    def request_json(self, *args: Any, **kwargs: Any) -> tuple[Any, Mapping[str, Any]]:
        response = self.request_bytes(*args, **kwargs)
        try:
            return json.loads(response.payload), response.record
        except (json.JSONDecodeError, UnicodeDecodeError) as error:
            raise CecError(
                f"CEC returned non-JSON HTTP {response.record.get('status', 0)} "
                f"for {response.record.get('url', '')}"
            ) from error


def _source(record: Mapping[str, Any], source_type: str) -> SourceEvidence:
    return SourceEvidence(
        url=str(record.get("url") or ""),
        retrieved_at=str(record.get("retrieved_at") or ""),
        sha256=str(record.get("sha256") or ""),
        status=int(record.get("status") or 0),
        source_type=source_type,
    )


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = " ".join(str(value).split()).strip()
    return "" if text.casefold() in {"null", "none"} else text


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _subject_code(mapping: Mapping[str, Any]) -> str:
    value = _first(
        mapping,
        "subjectRF",
        "subjectRf",
        "subjectRfCode",
        "subjectRFCode",
        "subject_code",
    )
    if isinstance(value, Mapping):
        value = _first(value, "externalId", "code", "id")
    return canonical_region_code(value)


def _commission_type(mapping: Mapping[str, Any]) -> str:
    value = _first(mapping, "commissionType", "commission_type", "type")
    if isinstance(value, Mapping):
        value = _first(value, "externalId", "code", "id")
    number = _integer(value)
    return COMMISSION_TYPE_NAMES.get(number, str(value or "unknown").casefold())


def _number(mapping: Mapping[str, Any], name: str) -> int | None:
    explicit = _integer(_first(mapping, "commissionNumber", "commission_number", "number"))
    if explicit is not None:
        return explicit
    match = NUMBER_RE.search(name)
    return int(match.group(1)) if match else None


def _clean_direct_address(value: Any) -> str:
    text = _text(value)
    # The search endpoint currently emits concatenations such as
    # ``null...null`` when address components are absent. Do not publish those.
    return "" if "null" in text.casefold() else text


def _join_address(*parts: Any) -> str:
    result: list[str] = []
    for part in parts:
        text = _text(part)
        if not text or text in result:
            continue
        result.append(text.strip(" ,"))
    return ", ".join(result)


def _target_parent(value: Any, external_id: str) -> Mapping[str, Any]:
    rows = value if isinstance(value, list) else []
    for row in rows:
        if (
            isinstance(row, Mapping)
            and str(_first(row, "systemExternalId", "externalId") or "") == external_id
        ):
            return row
    for row in rows:
        if isinstance(row, Mapping) and _integer(row.get("level")) == 0:
            return row
    return {}


def _report_body(value: Any, external_id: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    body = value.get("body")
    if not isinstance(body, Mapping):
        return {}
    report_id = str(_first(body, "commissionOrgId", "externalId") or "")
    return body if not report_id or report_id == external_id else {}


def _as_contact(
    search_row: Mapping[str, Any],
    search_record: Mapping[str, Any],
    parent_value: Any,
    parent_record: Mapping[str, Any] | None,
    report_value: Any,
    report_record: Mapping[str, Any] | None,
) -> CommissionContact:
    external_id = _text(_first(search_row, "externalId", "systemExternalId"))
    parent = _target_parent(parent_value, external_id)
    report = _report_body(report_value, external_id)

    name = (
        _text(_first(report, "commissionName"))
        or _text(_first(parent, "commissionName", "name"))
        or _text(_first(search_row, "commissionName", "name"))
    )
    subject_code = _subject_code(search_row) or _subject_code(parent) or _subject_code(report)
    commission_type = (
        _commission_type(report)
        if report.get("commissionType") not in (None, "")
        else _commission_type(parent)
        if parent.get("commissionType") not in (None, "")
        else _commission_type(search_row)
    )
    number = _number(report, name) or _number(parent, name) or _number(search_row, name)

    commission_address = _join_address(
        report.get("commissionPostCode"),
        report.get("commissionLocality"),
        report.get("commissionAddress"),
    )
    if not commission_address:
        commission_address = _join_address(
            parent.get("postCode"), parent.get("locality"), parent.get("customAddress")
        )
    if not commission_address:
        commission_address = _clean_direct_address(
            _first(search_row, "address", "commissionAddress")
        )

    voting_address = _clean_direct_address(report.get("votingRoom"))
    if not voting_address:
        voting_address = _join_address(
            report.get("votingRoomPostCode"),
            report.get("votingRoomLocality"),
            report.get("votingRoomAddress"),
            report.get("votingRoomName"),
        )
    if not voting_address:
        voting_address = _join_address(
            parent.get("votingRoomPostCode"),
            parent.get("votingRoomLocality"),
            parent.get("customVotingRoomAddress"),
        )

    commission_phone = (
        _text(_first(report, "commissionPhone"))
        or _text(_first(parent, "phone", "commissionPhone"))
        or _text(_first(search_row, "phone", "commissionPhone"))
    )
    voting_phone = _text(_first(report, "votingRoomPhone")) or _text(
        _first(parent, "votingRoomPhone")
    )

    evidence_record = search_record
    source_type = "cec-commission-search"
    if parent and parent_record:
        evidence_record = parent_record
        source_type = "cec-commission-parents"
    if report and report_record:
        evidence_record = report_record
        source_type = "cec-report-42"
    return CommissionContact(
        subject_code=subject_code,
        commission_type=commission_type,
        commission_number=number,
        commission_name=name,
        external_id=external_id,
        commission_address=commission_address,
        commission_phone=commission_phone,
        voting_address=voting_address,
        voting_phone=voting_phone,
        source=_source(evidence_record, source_type),
    )


def _content(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Mapping):
        return []
    rows = value.get("content")
    if rows is None and isinstance(value.get("body"), Mapping):
        rows = value["body"].get("content")
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _total_pages(value: Any) -> int:
    if not isinstance(value, Mapping):
        return 1
    pages = _integer(_first(value, "totalPages", "total_pages"))
    if pages is None and isinstance(value.get("body"), Mapping):
        pages = _integer(_first(value["body"], "totalPages", "total_pages"))
    return max(1, pages or 1)


def _contact_sort_key(contact: CommissionContact) -> tuple[Any, ...]:
    subject = contact.subject_code
    subject_key: tuple[int, Any] = (0, int(subject)) if subject.isdigit() else (1, subject)
    type_order = {"tik": 4, "uik": 5}.get(contact.commission_type, 99)
    return (
        subject_key,
        type_order,
        contact.commission_number is None,
        contact.commission_number or 0,
        contact.commission_name.casefold(),
        contact.external_id,
    )


class CecCrawler:
    def __init__(self, api: CecApi) -> None:
        self.api = api

    def _search(
        self,
        commission_type: int | None,
        *,
        subject_code: str | None = None,
        refresh: bool,
    ) -> tuple[list[tuple[Mapping[str, Any], Mapping[str, Any]]], dict[str, Any]]:
        rows: list[tuple[Mapping[str, Any], Mapping[str, Any]]] = []
        pages: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            params: dict[str, Any] = {"page": page, "perPage": self.api.config.page_size}
            if commission_type is not None:
                params["commissionTypes"] = commission_type
            if subject_code is not None:
                params["subjectRfCodes"] = subject_code
            try:
                value, record = self.api.request_json(
                    "GET", "/commissionOrg/search", params=params, refresh=refresh
                )
            except CecError as error:
                pages.append({"page": page, "status": "error", "error": str(error)})
                return rows, {"complete": False, "pages": pages, "rows": len(rows)}
            status = int(record.get("status") or 0)
            found = _content(value)
            pages.append(
                {
                    "page": page,
                    "status": status,
                    "rows": len(found),
                    "sha256": str(record.get("sha256") or ""),
                }
            )
            if status != 200:
                return rows, {"complete": False, "pages": pages, "rows": len(rows)}
            for row in found:
                scoped_row = dict(row)
                if subject_code is not None and not _subject_code(scoped_row):
                    # The live endpoint omits subjectRF on some projections even
                    # when all rows belong to a requested subject. The filter is
                    # safe evidence for the missing value.
                    scoped_row["subjectRfCode"] = subject_code
                rows.append((scoped_row, record))
            total_pages = _total_pages(value)
            page += 1
        return rows, {"complete": True, "pages": pages, "rows": len(rows)}

    def crawl(
        self,
        *,
        commission_types: Sequence[int] = (4, 5),
        subject_codes: Sequence[str] | None = None,
        include_unfiltered: bool = False,
        include_report42: bool = True,
        refresh: bool = False,
    ) -> tuple[list[CommissionContact], dict[str, Any]]:
        search_status: dict[str, Any] = {}
        found_by_id: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
        search_scopes: list[tuple[int | None, str | None]] = [
            (commission_type, subject_code)
            for commission_type in commission_types
            for subject_code in (subject_codes or (None,))
        ]
        if include_unfiltered:
            search_scopes.append((None, None))
        requested_kinds = {
            COMMISSION_TYPE_NAMES.get(commission_type, str(commission_type))
            for commission_type in commission_types
        }
        for commission_type, subject_code in search_scopes:
            found, status = self._search(
                commission_type, subject_code=subject_code, refresh=refresh
            )
            label = "all" if commission_type is None else str(commission_type)
            if subject_code is not None:
                label = f"{label}@{canonical_region_code(subject_code)}"
            search_status[label] = status
            for row, record in found:
                if commission_type is None and _commission_type(row) not in requested_kinds:
                    continue
                external_id = _text(_first(row, "externalId", "systemExternalId"))
                if external_id:
                    found_by_id.setdefault(external_id, (row, record))

        contacts: list[CommissionContact] = []
        enrichment = Counter()
        errors: list[dict[str, str]] = []
        for external_id in sorted(found_by_id):
            search_row, search_record = found_by_id[external_id]
            parent_value: Any = None
            parent_record: Mapping[str, Any] | None = None
            report_value: Any = None
            report_record: Mapping[str, Any] | None = None
            try:
                parent_value, parent_record = self.api.request_json(
                    "GET",
                    f"/commissionOrg/parents/{urllib.parse.quote(external_id, safe='')}",
                    refresh=refresh,
                )
                if int(parent_record.get("status") or 0) == 200:
                    enrichment["parents_ok"] += 1
                else:
                    enrichment["parents_http_error"] += 1
            except CecError as error:
                enrichment["parents_error"] += 1
                errors.append({"external_id": external_id, "stage": "parents", "error": str(error)})
            if include_report42:
                try:
                    report_value, report_record = self.api.request_json(
                        "GET",
                        "/reports/42",
                        params={"commissionOrgId": external_id},
                        refresh=refresh,
                    )
                    if int(report_record.get("status") or 0) == 200:
                        enrichment["report42_ok"] += 1
                    else:
                        enrichment["report42_http_error"] += 1
                except CecError as error:
                    enrichment["report42_error"] += 1
                    errors.append(
                        {"external_id": external_id, "stage": "report42", "error": str(error)}
                    )
            contacts.append(
                _as_contact(
                    search_row,
                    search_record,
                    parent_value,
                    parent_record,
                    report_value,
                    report_record,
                )
            )

        contacts.sort(key=_contact_sort_key)
        by_type = Counter(contact.commission_type for contact in contacts)
        summary: dict[str, Any] = {
            "schema_version": 1,
            "complete": all(bool(item.get("complete")) for item in search_status.values()),
            "search": search_status,
            "contacts": {
                "total": len(contacts),
                "by_type": dict(sorted(by_type.items())),
                "with_commission_address": sum(bool(c.commission_address) for c in contacts),
                "with_commission_phone": sum(bool(c.commission_phone) for c in contacts),
                "with_voting_address": sum(bool(c.voting_address) for c in contacts),
                "with_voting_phone": sum(bool(c.voting_phone) for c in contacts),
            },
            "enrichment": dict(sorted(enrichment.items())),
            "errors": sorted(errors, key=lambda item: (item["external_id"], item["stage"])),
        }
        if errors:
            summary["complete"] = False
        return contacts, summary


def write_crawl_outputs(
    output_dir: Path,
    contacts: Iterable[CommissionContact],
    summary: Mapping[str, Any],
) -> tuple[Path, Path]:
    contacts_path = output_dir / "contacts.jsonl"
    summary_path = output_dir / "summary.json"
    sorted_contacts = sorted(contacts, key=_contact_sort_key)
    write_jsonl(contacts_path, (contact.to_dict() for contact in sorted_contacts))
    _atomic_write(
        summary_path,
        json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
    )
    return contacts_path, summary_path


def crawl_cec_contacts(
    output_dir: Path,
    *,
    base_url: str = DEFAULT_BASE_URL,
    proxy_url: str | None = None,
    timeout: float = 30.0,
    retries: int = 5,
    refresh: bool = False,
    include_report42: bool = True,
    include_unfiltered: bool = False,
    commission_types: Sequence[int] = (4, 5),
    subject_codes: Sequence[str] | None = None,
    page_size: int = 1_000,
    session: Any | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> CrawlResult:
    """Crawl current CEC contacts and write deterministic normalized outputs.

    ``session`` and ``sleep`` are public primarily so callers/tests can supply a
    network-free transport. The cache lives below ``output_dir/raw``.
    """

    output_dir = Path(output_dir)
    store = RawResponseStore(output_dir / "raw")
    config = CecConfig(
        base_url=base_url,
        proxy_url=proxy_url,
        timeout=timeout,
        retries=retries,
        page_size=page_size,
    )
    api = CecApi(store, config, session=session, sleep=sleep)
    contacts, summary = CecCrawler(api).crawl(
        commission_types=commission_types,
        subject_codes=subject_codes,
        include_unfiltered=include_unfiltered,
        include_report42=include_report42,
        refresh=refresh,
    )
    contacts_path, summary_path = write_crawl_outputs(output_dir, contacts, summary)
    return CrawlResult(
        contacts=tuple(contacts),
        summary=summary,
        contacts_path=contacts_path,
        summary_path=summary_path,
        manifest_path=store.manifest_path,
    )
