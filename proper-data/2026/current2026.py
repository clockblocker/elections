from __future__ import annotations

import argparse
import concurrent.futures
import io
import json
import random
import re
import sys
import threading
import time
import urllib.parse
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

try:
    from .common import (
        DEFAULT_REQUEST_HEADERS,
        _redact_proxy_error,
        atomic_write,
        configured_proxy_url,
        json_write,
        sha256_bytes,
    )
    from .shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from .transport import RETRYABLE_STATUS, ResponseStore, retry_after_seconds
except ImportError:
    SHARED_CRAWLER_DIR = Path(__file__).resolve().parents[1] / "crawler"
    sys.path.insert(0, str(SHARED_CRAWLER_DIR))
    from common import (
        DEFAULT_REQUEST_HEADERS,
        _redact_proxy_error,
        atomic_write,
        configured_proxy_url,
        json_write,
        sha256_bytes,
    )
    from shared_rate import DEFAULT_COORDINATION_DIR, SharedRateLimiter
    from transport import RETRYABLE_STATUS, ResponseStore, retry_after_seconds


API_ROOT = "http://apps.cikrf.ru/service/ik-inp-service-pbcopy"
DEFAULT_VOTING_DATE = "2026-09-20"
DEFAULT_RAW = Path("data/raw/cik-current-2026-09-20")
DEFAULT_STAGING = Path("reports/generated/cik-current-2026-09-20/elections")
DEFAULT_OUTPUT = Path("proper-data/2026")
DEFAULT_DECLARATIONS_RAW = Path("data/raw/cik-current-2026-declarations")
DEFAULT_DECLARATIONS_OUTPUT = DEFAULT_OUTPUT / "declarations"
DEFAULT_DUMA_ELECTION_EXTERNAL_ID = "2b72bb97-c625-4a02-a76b-b5740c4d5f6a"
DEFAULT_BASELINE = Path("proper-data/2024-president/uik-to-tik")
DECLARATION_REPORT_TYPE = 77
CLASSIFIER_CONCURRENCY = 8
CLASSIFIER_BATCH_SIZE = 64
UIK_RE = re.compile(r"^УИК\s*№?\s*(\d+)$", re.IGNORECASE)
CHALLENGE_RE = re.compile(r"^return\s+(\d+)\s*\+\s*(\d+)\s*;?$")

COMMISSION_TYPES = {
    0: "cik",
    1: "organizing",
    2: "regional",
    3: "district",
    4: "territorial",
    5: "uik",
    6: "other",
}

NOMINATION_STATUS = {
    "выбывший из заверенного списка": "removed_from_certified_list",
    "кандидат с отказом в заверении": "certification_refused",
    "выдвинут": "nominated",
    "утративший статус выдвинутого кандидата": "nomination_lost",
}
REGISTRATION_STATUS = {
    "исключенный из списка": "removed_from_list",
    "зарегистрирован": "registered",
    "отказ в регистрации": "registration_refused",
    "выбывший (после регистрации) кандидат": "withdrawn_after_registration",
}
ELECTION_STATUS = {
    "отказ от мандата": "mandate_refused",
    "сложивший полномочия": "mandate_resigned",
    "избран": "elected",
}

TERRITORIES_INTERNATIONALLY_RECOGNIZED_AS_UKRAINE = {
    "93": "Crimea",
    "94": "Sevastopol",
    "95": "Donetsk",
    "96": "Luhansk",
    "97": "Zaporizhzhia",
    "98": "Kherson",
}


def _compact_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _dict_external(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("externalId") is not None:
        return str(value["externalId"])
    return None


def _dict_value(value: Any) -> str | None:
    if isinstance(value, dict) and value.get("value") is not None:
        return str(value["value"]).strip() or None
    return None


def _region_code(value: Any) -> str:
    raw = _dict_external(value) if isinstance(value, dict) else str(value or "")
    raw = raw or "0"
    return str(int(raw)) if raw.isdigit() else raw


def _territorial_status(region_code: str) -> dict[str, Any] | None:
    territory = TERRITORIES_INTERNATIONALLY_RECOGNIZED_AS_UKRAINE.get(region_code)
    if not territory:
        return None
    resolution = "A/RES/68/262" if region_code in {"93", "94"} else "A/RES/ES-11/4"
    return {
        "cecClassification": "subject_of_russian_federation",
        "internationalRecognition": "part_of_ukraine",
        "territory": territory,
        "unReference": f"https://docs.un.org/{resolution}",
    }


def _campaign_scope(level_code: str | None) -> str:
    if level_code == "1":
        return "federal"
    if level_code == "2":
        return "regional"
    if level_code in {"3", "4", "5", "6"}:
        return "municipal"
    return "other_or_unclassified"


def _source(record: dict[str, Any], *, created_at: str | None = None) -> dict[str, Any]:
    result = {
        "url": record.get("official_url", record.get("requested_url")),
        "requestKey": record.get("requested_url"),
        "retrievedAt": record.get("retrieved_at"),
        "sha256": record.get("sha256"),
        "status": record.get("status"),
        "provenance": record.get("provenance", "live-official"),
    }
    if created_at:
        result["createdAt"] = created_at
    return result


class OfficialApiError(RuntimeError):
    pass


class OfficialApi:
    """Authenticated, resumable client for the public current-election CEC API."""

    def __init__(
        self,
        store: ResponseStore,
        limiter: SharedRateLimiter,
        *,
        timeout: float = 30.0,
        retries: int = 5,
        backoff_initial: float = 0.5,
        backoff_max: float = 30.0,
        proxy_url: str | None = None,
        sleep: Any = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        self.store = store
        self.limiter = limiter
        self.timeout = timeout
        self.retries = retries
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.proxy_url = proxy_url if proxy_url is not None else configured_proxy_url()
        self.sleep = sleep
        self.random = random_source or random.Random()
        self.fingerprint = DEFAULT_REQUEST_HEADERS["User-Agent"]
        self._api_key = ""
        self._auth_lock = threading.Lock()

    @property
    def proxies(self) -> dict[str, str] | None:
        if not self.proxy_url:
            return None
        return {"http": self.proxy_url, "https": self.proxy_url}

    def _raw_request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        self.limiter.wait()
        kwargs["headers"] = {
            **DEFAULT_REQUEST_HEADERS,
            **(kwargs.get("headers") or {}),
        }
        try:
            return requests.request(
                method,
                url,
                timeout=self.timeout,
                proxies=self.proxies,
                allow_redirects=True,
                **kwargs,
            )
        except requests.RequestException as error:
            raise OfficialApiError(
                _redact_proxy_error(error, self.proxy_url)
            ) from error

    def _authenticate(self, *, force: bool = False) -> str:
        with self._auth_lock:
            if self._api_key and not force:
                return self._api_key
            challenge_response = self._raw_request("GET", f"{API_ROOT}/challenge/get")
            challenge_response.raise_for_status()
            challenge = challenge_response.json()
            task = str(challenge.get("jsTask") or "").strip()
            match = CHALLENGE_RE.fullmatch(task)
            if not match:
                raise OfficialApiError(f"unsupported official challenge: {task!r}")
            answer = str(int(match.group(1)) + int(match.group(2)))
            solve_response = self._raw_request(
                "POST",
                f"{API_ROOT}/challenge/solve",
                headers={"Content-Type": "application/json"},
                json={
                    "pubToken": challenge["pubToken"],
                    "answer": answer,
                    "fingerprint": self.fingerprint,
                },
            )
            solve_response.raise_for_status()
            self._api_key = str(solve_response.json().get("apiKey") or "")
            if not self._api_key:
                raise OfficialApiError("official challenge returned no API key")
            return self._api_key

    def _backoff(self, attempt: int, retry_after: float | None = None) -> None:
        base = min(self.backoff_max, self.backoff_initial * (2**attempt))
        delay = (
            retry_after
            if retry_after is not None
            else self.random.uniform(base / 2, base)
        )
        self.limiter.penalize(delay)

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        refresh: bool = False,
    ) -> tuple[bytes, dict[str, Any]]:
        method = method.upper()
        query = urllib.parse.urlencode(
            sorted((str(key), str(value)) for key, value in (params or {}).items())
        )
        official_url = f"{API_ROOT}{path}" + (f"?{query}" if query else "")
        request_payload = _compact_json(body) if body is not None else None
        cache_key = official_url
        if request_payload is not None:
            cache_key += f"#request-sha256={sha256_bytes(request_payload)}"
        if not refresh and (existing := self.store.verified(cache_key)):
            status = int(existing.get("status", 0))
            if 200 <= status < 300:
                payload = (self.store.root / existing["body_path"]).read_bytes()
                return payload, {**existing, "cache_hit": True}

        last_error = ""
        for attempt in range(self.retries + 1):
            started = time.monotonic()
            headers = {
                **DEFAULT_REQUEST_HEADERS,
                "X-Api-Key": self._authenticate(),
                "X-Client-Fingerprint": self.fingerprint,
            }
            if request_payload is not None:
                headers["Content-Type"] = "application/json"
            try:
                response = self._raw_request(
                    method,
                    official_url,
                    headers=headers,
                    data=request_payload,
                )
            except OfficialApiError as error:
                last_error = str(error)
                if attempt < self.retries:
                    self._backoff(attempt)
                    continue
                record = self.store.save_failure(
                    cache_key,
                    {
                        "official_url": official_url,
                        "request_method": method,
                        "retrieved_at": datetime.now(timezone.utc).isoformat(),
                        "error": last_error,
                        "error_class": type(error).__name__,
                        "elapsed_seconds": round(time.monotonic() - started, 6),
                        "retry_count": attempt,
                    },
                )
                raise OfficialApiError(last_error) from error

            if response.status_code == 401 and attempt < self.retries:
                self._authenticate(force=True)
                continue
            payload = response.content
            record = self.store.save(
                cache_key,
                payload,
                {
                    "official_url": official_url,
                    "request_method": method,
                    "request_body_sha256": sha256_bytes(request_payload)
                    if request_payload is not None
                    else None,
                    "final_url": response.url,
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "status": response.status_code,
                    "content_type": response.headers.get("Content-Type", ""),
                    "content_disposition": response.headers.get(
                        "Content-Disposition", ""
                    ),
                    "elapsed_seconds": round(time.monotonic() - started, 6),
                    "retry_count": attempt,
                    "source_host": urllib.parse.urlsplit(response.url).netloc,
                    "provenance": "live-official",
                },
            )
            if response.status_code in RETRYABLE_STATUS and attempt < self.retries:
                self._backoff(
                    attempt, retry_after_seconds(response.headers.get("Retry-After"))
                )
                continue
            return payload, record
        raise OfficialApiError(last_error or f"request failed: {official_url}")

    def request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        refresh: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        payload, record = self.request_bytes(
            method, path, params=params, body=body, refresh=refresh
        )
        try:
            value = json.loads(payload)
        except json.JSONDecodeError as error:
            status = int(record.get("status", 0))
            if status >= 400:
                return {
                    "message": "official API returned a non-JSON error body",
                    "bodySha256": record.get("sha256"),
                }, record
            raise OfficialApiError(
                f"official API returned non-JSON status {status}: "
                f"{record.get('official_url')}"
            ) from error
        return value, record


def _fetch_candidate_branch(
    api: OfficialApi,
    election: dict[str, Any],
    scope: str,
    *,
    refresh: bool,
) -> dict[str, Any]:
    external_id = str(election["externalId"])
    base: dict[str, Any] = {
        "page": 1,
        "perPage": 1000,
        "electionsId": external_id,
        "showSelfNominated": True,
    }
    if scope == "majoritarian":
        base["onlyMajoritarian"] = True
    elif scope == "proportional":
        base["onlyProportional"] = True
        base["showSelfNominated"] = False
    elif scope != "direct":
        raise ValueError(scope)

    pages: list[dict[str, Any]] = []
    page = 1
    total_pages = 1
    while page <= total_pages:
        request = {**base, "page": page}
        value, record = api.request_json(
            "POST", "/candidate/paging", body=request, refresh=refresh
        )
        status = int(record.get("status", 0))
        if status != 200 or not isinstance(value, dict):
            return {
                "scope": scope,
                "complete": False,
                "error": value,
                "sources": [_source(record)],
                "rows": [],
            }
        total_pages = max(1, int(value.get("totalPages") or 1))
        pages.append(
            {
                "page": page,
                "source": _source(record, created_at=value.get("createdAt")),
                "rows": value.get("content") or [],
            }
        )
        page += 1
    return {
        "scope": scope,
        "complete": True,
        "total": sum(len(item["rows"]) for item in pages),
        "pages": pages,
    }


def _ingest_classifier(
    value: Any,
    record: dict[str, Any],
    nodes: dict[str, dict[str, Any]],
    pending: list[str],
) -> None:
    if not isinstance(value, dict) or value.get("id") is None:
        return
    source = _source(record)

    def add(raw: dict[str, Any], parent_id: int | None = None) -> None:
        external_id = str(raw["externalId"])
        current = {key: val for key, val in raw.items() if key != "children"}
        if current.get("parentId") is None and parent_id is not None:
            current["parentId"] = parent_id
        previous = nodes.get(external_id)
        sources = list(previous.get("sources", [])) if previous else []
        if not any(item.get("sha256") == source.get("sha256") for item in sources):
            sources.append(source)
        current["sources"] = sources
        nodes[external_id] = {**(previous or {}), **current}
        # Type 9 is the portal's FAKE placeholder. It is deliberately unclickable
        # in the official frontend and its advertised child endpoint returns 500.
        if (
            raw.get("hasChildren")
            and int(raw.get("type", -1)) != 9
            and external_id not in pending
        ):
            pending.append(external_id)
        for child in raw.get("children") or []:
            if isinstance(child, dict) and child.get("externalId"):
                add(child, int(raw["id"]))

    add(value)


def _fetch_classifier(
    api: OfficialApi,
    election: dict[str, Any],
    *,
    refresh: bool,
) -> dict[str, Any]:
    election_id = int(election["id"])
    root, root_record = api.request_json(
        "GET",
        "/commissionClassifiers",
        params={"electionsId": election_id},
        refresh=refresh,
    )
    if int(root_record.get("status", 0)) != 200 or not isinstance(root, dict):
        return {
            "complete": False,
            "error": root,
            "sources": [_source(root_record)],
            "nodes": [],
        }
    nodes: dict[str, dict[str, Any]] = {}
    pending: list[str] = []
    _ingest_classifier(root, root_record, nodes, pending)
    expanded: set[str] = set()
    while pending:
        batch: list[str] = []
        while pending and len(batch) < CLASSIFIER_BATCH_SIZE:
            classifier_id = pending.pop(0)
            if classifier_id in expanded:
                continue
            expanded.add(classifier_id)
            batch.append(classifier_id)
        if not batch:
            continue

        def fetch(classifier_id: str) -> tuple[str, Any, dict[str, Any]]:
            value, record = api.request_json(
                "GET",
                "/commissionClassifiers",
                params={"classifierId": classifier_id, "electionsId": election_id},
                refresh=refresh,
            )
            return classifier_id, value, record

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(CLASSIFIER_CONCURRENCY, len(batch))
        ) as executor:
            responses = list(executor.map(fetch, batch))
        failures = [
            (classifier_id, value, record)
            for classifier_id, value, record in responses
            if int(record.get("status", 0)) != 200
        ]
        if failures:
            classifier_id, value, record = failures[0]
            return {
                "complete": False,
                "error": {"classifierId": classifier_id, "response": value},
                "sources": [_source(root_record), _source(record)],
                "nodes": sorted(nodes.values(), key=lambda item: int(item["id"])),
            }
        for _, value, record in responses:
            _ingest_classifier(value, record, nodes, pending)
    return {
        "complete": True,
        "rootExternalId": str(root["externalId"]),
        "nodes": sorted(nodes.values(), key=lambda item: int(item["id"])),
    }


def _crawl_election(
    api: OfficialApi,
    election: dict[str, Any],
    staging_dir: Path,
    *,
    refresh: bool,
) -> dict[str, Any]:
    output = staging_dir / f"election-{int(election['id'])}.json"
    if output.is_file() and not refresh:
        existing = json.loads(output.read_text(encoding="utf-8"))
        association_report = existing.get("associationReport")
        sources_complete = (
            bool(existing.get("classifier", {}).get("complete"))
            and all(
                bool(branch.get("complete"))
                for branch in existing.get("candidateBranches", [])
            )
            and (association_report is None or bool(association_report.get("complete")))
        )
        if existing.get("complete") and sources_complete:
            return {
                "id": election["id"],
                "name": election["name"],
                "cached": True,
                "complete": True,
            }

    kind = int(_dict_external(election.get("kind")) or -1)
    candidate_branches: list[dict[str, Any]] = []
    association_report: dict[str, Any] | None = None
    if kind == 1:
        candidate_branches.append(
            _fetch_candidate_branch(api, election, "direct", refresh=refresh)
        )
    elif kind == 2:
        candidate_branches.extend(
            [
                _fetch_candidate_branch(api, election, "majoritarian", refresh=refresh),
                _fetch_candidate_branch(api, election, "proportional", refresh=refresh),
            ]
        )
        value, record = api.request_json(
            "GET",
            "/reports/236",
            params={"electionsId": election["externalId"]},
            refresh=refresh,
        )
        association_report = {
            "complete": int(record.get("status", 0)) == 200,
            "source": _source(
                record,
                created_at=value.get("createdAt") if isinstance(value, dict) else None,
            ),
            "rows": value.get("body") or [] if isinstance(value, dict) else [],
            "error": None if int(record.get("status", 0)) == 200 else value,
        }

    classifier = _fetch_classifier(api, election, refresh=refresh)
    complete = (
        bool(classifier.get("complete"))
        and all(branch.get("complete") for branch in candidate_branches)
        and (association_report is None or bool(association_report.get("complete")))
    )
    result = {
        "schemaVersion": 1,
        "election": election,
        "candidateBranches": candidate_branches,
        "associationReport": association_report,
        "classifier": classifier,
        "complete": complete,
    }
    json_write(output, result)
    return {
        "id": election["id"],
        "name": election["name"],
        "cached": False,
        "complete": complete,
        "candidateCount": sum(branch.get("total", 0) for branch in candidate_branches),
        "classifierCount": len(classifier.get("nodes", [])),
    }


def crawl(args: argparse.Namespace) -> int:
    store = ResponseStore(args.raw_dir)
    limiter = SharedRateLimiter(
        args.rate, coordination_dir=args.coordination_dir, jitter=args.jitter
    )
    api = OfficialApi(
        store,
        limiter,
        timeout=args.timeout,
        retries=args.retries,
        backoff_initial=args.backoff_initial,
        backoff_max=args.backoff_max,
    )
    election_page, election_record = api.request_json(
        "GET",
        "/elections",
        params={
            "page": 1,
            "perPage": 5000,
            "votingDateFrom": args.voting_date,
            "votingDateTo": args.voting_date,
        },
        refresh=args.refresh,
    )
    elections = election_page.get("content") or []
    expected = int(election_page.get("totalSize") or 0)
    if expected != len(elections):
        raise OfficialApiError(
            f"election calendar truncated: expected {expected}, received {len(elections)}"
        )
    selected_regions = {_region_code(value) for value in args.region or []}
    if selected_regions:
        elections = [
            item
            for item in elections
            if _region_code(item.get("subjectRf")) in selected_regions
            or _region_code(item.get("subjectRf")) == "0"
        ]
    selected_elections = {int(value) for value in args.election_id or []}
    if selected_elections:
        elections = [
            item for item in elections if int(item["id"]) in selected_elections
        ]
    if args.limit:
        elections = elections[: args.limit]
    status_dictionaries: dict[str, Any] = {}
    for status_type in ("nomination", "enrollment", "election"):
        value, record = api.request_json(
            "GET",
            "/candidate/statuses",
            params={"statusType": status_type},
            refresh=args.refresh,
        )
        status_dictionaries[status_type] = {
            "values": value,
            "source": _source(record),
        }
    args.staging_dir.mkdir(parents=True, exist_ok=True)
    json_write(
        args.staging_dir.parent / "index.json",
        {
            "schemaVersion": 1,
            "votingDate": args.voting_date,
            "electionCount": len(elections),
            "electionsSource": _source(election_record),
            "statusDictionaries": status_dictionaries,
            "electionIds": [int(item["id"]) for item in elections],
        },
    )

    print(f"official campaigns: {len(elections):,}", flush=True)
    completed = 0
    failed: list[dict[str, Any]] = []
    counts: Counter[str] = Counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {
            pool.submit(
                _crawl_election,
                api,
                election,
                args.staging_dir,
                refresh=args.refresh,
            ): election
            for election in elections
        }
        for future in concurrent.futures.as_completed(futures):
            election = futures[future]
            completed += 1
            try:
                item = future.result()
            except Exception as error:  # noqa: BLE001 - preserve other campaigns
                failed.append(
                    {
                        "id": election.get("id"),
                        "name": election.get("name"),
                        "error": f"{type(error).__name__}: {error}",
                    }
                )
                counts["failed"] += 1
            else:
                counts["complete" if item.get("complete") else "partial"] += 1
                counts["cached"] += int(bool(item.get("cached")))
            if completed % args.progress_every == 0 or completed == len(elections):
                print(
                    f"campaigns {completed:,}/{len(elections):,}; "
                    f"complete={counts['complete']:,}; partial={counts['partial']:,}; "
                    f"failed={counts['failed']:,}; cached={counts['cached']:,}",
                    flush=True,
                )
    store.flush()
    report = {
        "schemaVersion": 1,
        "votingDate": args.voting_date,
        "electionCount": len(elections),
        "counts": dict(counts),
        "failed": failed,
        "complete": not failed and counts["partial"] == 0,
    }
    json_write(args.report, report)
    return 0 if report["complete"] else 1


def _declaration_identity(row: dict[str, Any]) -> tuple[str, str]:
    body = row.get("body")
    if not isinstance(body, dict):
        raise OfficialApiError("declaration listing row has no body")
    financial_report_id = str(body.get("id") or "")
    file_name = str(body.get("fileName") or "")
    if not financial_report_id or not re.fullmatch(
        r"[A-Za-z0-9-]+", financial_report_id
    ):
        raise OfficialApiError("declaration listing row has an unsafe or missing id")
    if (
        not file_name
        or file_name in {".", ".."}
        or "/" in file_name
        or "\\" in file_name
        or "\x00" in file_name
    ):
        raise OfficialApiError(
            f"declaration {financial_report_id} has an unsafe or missing file name"
        )
    return financial_report_id, file_name


def _fetch_declaration_listing(
    api: OfficialApi,
    election_external_id: str,
    *,
    page_size: int,
    refresh: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    expected_total: int | None = None
    page = 1
    total_pages = 1
    while page <= total_pages:
        value, record = api.request_json(
            "POST",
            f"/reports/{DECLARATION_REPORT_TYPE}/search",
            body={
                "page": page,
                "perPage": page_size,
                "reportType": DECLARATION_REPORT_TYPE,
                "electionId": election_external_id,
            },
            refresh=refresh,
        )
        status = int(record.get("status", 0))
        if status != 200 or not isinstance(value, dict):
            raise OfficialApiError(
                f"declaration listing page {page} returned status {status}"
            )
        content = value.get("content")
        if not isinstance(content, list) or not all(
            isinstance(item, dict) for item in content
        ):
            raise OfficialApiError(
                f"declaration listing page {page} has invalid content"
            )
        page_total = int(value.get("totalSize") or 0)
        if expected_total is None:
            expected_total = page_total
        elif page_total != expected_total:
            raise OfficialApiError(
                "declaration listing total changed while pagination was in progress"
            )
        total_pages = max(1, int(value.get("totalPages") or 1))
        rows.extend(content)
        sources.append(_source(record, created_at=value.get("createdAt")))
        page += 1

    if len(rows) != expected_total:
        raise OfficialApiError(
            f"declaration listing truncated: expected {expected_total}, received {len(rows)}"
        )
    identifiers = [_declaration_identity(row)[0] for row in rows]
    if len(set(identifiers)) != len(identifiers):
        raise OfficialApiError("declaration listing contains duplicate file ids")
    return rows, sources


def _extract_declaration_archive(
    payload: bytes,
    *,
    expected_file_name: str,
    destination: Path,
) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            files = [item for item in archive.infolist() if not item.is_dir()]
            if len(files) != 1:
                raise OfficialApiError(
                    f"declaration archive contains {len(files)} files instead of one"
                )
            member = files[0]
            if member.filename != expected_file_name:
                raise OfficialApiError(
                    "declaration archive file name differs from its listing: "
                    f"{member.filename!r} != {expected_file_name!r}"
                )
            if member.flag_bits & 0x1:
                raise OfficialApiError("declaration archive member is encrypted")
            document = archive.read(member)
    except zipfile.BadZipFile as error:
        raise OfficialApiError(
            "declaration download is not a valid ZIP archive"
        ) from error

    atomic_write(destination, document)
    return {
        "path": destination.name,
        "byteLength": len(document),
        "sha256": sha256_bytes(document),
    }


def _download_declaration(
    api: OfficialApi,
    row: dict[str, Any],
    output_dir: Path,
    *,
    refresh: bool,
) -> dict[str, Any]:
    financial_report_id, file_name = _declaration_identity(row)
    payload, record = api.request_bytes(
        "GET",
        f"/reports/{DECLARATION_REPORT_TYPE}/files",
        params={"financialReportId": financial_report_id},
        refresh=refresh,
    )
    status = int(record.get("status", 0))
    if status != 200:
        raise OfficialApiError(
            f"declaration {financial_report_id} returned status {status}"
        )
    destination_dir = output_dir / "files" / financial_report_id
    document = _extract_declaration_archive(
        payload,
        expected_file_name=file_name,
        destination=destination_dir / file_name,
    )
    body = row["body"]
    return {
        "reportId": str(row.get("id") or ""),
        "financialReportId": financial_report_id,
        "reportType": str(row.get("reportType") or body.get("reportType") or ""),
        "electionId": str(body.get("electionId") or ""),
        "subjectRf": str(body.get("subjectRf") or ""),
        "fileName": file_name,
        "uploadTime": body.get("uploadTime"),
        "createdAt": row.get("createdAt"),
        "archive": {
            "byteLength": len(payload),
            "sha256": sha256_bytes(payload),
            "source": _source(record),
        },
        "document": {
            **document,
            "path": str(
                (Path("files") / financial_report_id / document["path"]).as_posix()
            ),
        },
    }


def crawl_declarations(args: argparse.Namespace) -> int:
    store = ResponseStore(args.raw_dir)
    limiter = SharedRateLimiter(
        args.rate, coordination_dir=args.coordination_dir, jitter=args.jitter
    )
    api = OfficialApi(
        store,
        limiter,
        timeout=args.timeout,
        retries=args.retries,
        backoff_initial=args.backoff_initial,
        backoff_max=args.backoff_max,
    )
    try:
        rows, listing_sources = _fetch_declaration_listing(
            api,
            args.election_external_id,
            page_size=args.page_size,
            refresh=args.refresh,
        )
        if args.expect_count is not None and len(rows) != args.expect_count:
            raise OfficialApiError(
                f"expected {args.expect_count} declarations, official listing returned "
                f"{len(rows)}"
            )
        print(f"official declarations: {len(rows):,}", flush=True)

        downloaded: dict[str, dict[str, Any]] = {}
        failures: list[dict[str, Any]] = []
        completed = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.concurrency
        ) as pool:
            futures = {
                pool.submit(
                    _download_declaration,
                    api,
                    row,
                    args.output,
                    refresh=args.refresh,
                ): row
                for row in rows
            }
            for future in concurrent.futures.as_completed(futures):
                row = futures[future]
                financial_report_id, file_name = _declaration_identity(row)
                completed += 1
                try:
                    downloaded[financial_report_id] = future.result()
                except Exception as error:  # noqa: BLE001 - preserve other downloads
                    failures.append(
                        {
                            "financialReportId": financial_report_id,
                            "fileName": file_name,
                            "error": f"{type(error).__name__}: {error}",
                        }
                    )
                if completed % args.progress_every == 0 or completed == len(rows):
                    print(
                        f"declarations {completed:,}/{len(rows):,}; "
                        f"downloaded={len(downloaded):,}; failed={len(failures):,}",
                        flush=True,
                    )

        declarations = [
            downloaded[financial_report_id]
            for row in rows
            if (financial_report_id := _declaration_identity(row)[0]) in downloaded
        ]
        complete = not failures and len(declarations) == len(rows)
        index = {
            "schemaVersion": 1,
            "reportType": DECLARATION_REPORT_TYPE,
            "electionId": args.election_external_id,
            "retrievedAt": datetime.now(timezone.utc).isoformat(),
            "listingSources": listing_sources,
            "officialCount": len(rows),
            "downloadedCount": len(declarations),
            "failedCount": len(failures),
            "complete": complete,
            "failures": failures,
            "declarations": declarations,
        }
        json_write(args.output / "index.json", index)
        return 0 if complete else 1
    finally:
        store.flush()


def _load_baseline(directory: Path) -> dict[tuple[str, int], dict[str, Any]]:
    result: dict[tuple[str, int], dict[str, Any]] = {}
    if not directory.is_dir():
        return result
    for path in sorted(directory.glob("region-*.ts")):
        source = path.read_text(encoding="utf-8")
        start = source.find("[")
        end = source.rfind("] satisfies")
        if start < 0 or end < 0:
            raise ValueError(f"cannot parse generated baseline {path}")
        for row in json.loads(source[start : end + 1]):
            key = (_region_code(row.get("regionCode")), int(row["uikNumber"]))
            if key in result:
                raise ValueError(f"duplicate baseline UIK {key}")
            result[key] = row
    return result


def _status(raw: Any, mapping: dict[str, str]) -> dict[str, Any] | None:
    if raw is None or not str(raw).strip():
        return None
    label = str(raw).strip()
    return {"code": mapping.get(label, "unmapped"), "label": label}


def _candidate(
    raw: dict[str, Any],
    election: dict[str, Any],
    scope: str,
    source: dict[str, Any],
) -> dict[str, Any]:
    registration = _status(raw.get("enrollment"), REGISTRATION_STATUS)
    return {
        "candidateId": str(raw["id"]),
        "electionId": str(election["externalId"]),
        "scope": scope,
        "districtNumber": int(raw.get("districtNum") or 0) or None,
        "fullName": raw.get("fullName"),
        "birthDate": raw.get("birthDate"),
        "associationName": raw.get("electionAssociation"),
        "nominationStatus": _status(raw.get("nomination"), NOMINATION_STATUS),
        "registrationStatus": registration,
        "electionStatus": _status(raw.get("election"), ELECTION_STATUS),
        "ballotEligible": bool(registration and registration["code"] == "registered"),
        "regionalGroup": raw.get("regionalGroup"),
        "regionalGroupNumber": raw.get("regionalGroupNum"),
        "numberInList": raw.get("numberInList"),
        "registrationDate": raw.get("regDate"),
        "registrationDecisionDate": raw.get("enrollmentDate"),
        "registrationDecisionNumber": raw.get("enrollmentNumber"),
        "source": source,
    }


def _association(
    raw: dict[str, Any], election: dict[str, Any], source: dict[str, Any]
) -> dict[str, Any]:
    marked_registered = bool(
        raw.get("registeredDecisionDate") or raw.get("registeredDecisionNumber")
    )
    return {
        "associationId": str(raw["associationId"]),
        "electionId": str(election["externalId"]),
        "name": raw.get("associationName"),
        "drawNumber": raw.get("drawNum"),
        "registrationMark": "registered"
        if marked_registered
        else "not_marked_registered",
        "ballotEligibility": True if marked_registered else None,
        "certifiedDecisionDate": raw.get("certifiedDecisionDate"),
        "certifiedDecisionNumber": raw.get("certifiedDecisionNumber"),
        "registeredDecisionDate": raw.get("registeredDecisionDate"),
        "registeredDecisionNumber": raw.get("registeredDecisionNumber"),
        "majoritarianCertifiedDecisionDate": raw.get("majCertifiedDecisionDate"),
        "majoritarianCertifiedDecisionNumber": raw.get("majCertifiedDecisionNumber"),
        "foreignAgentInList": raw.get("foreignAgentExist"),
        "source": source,
    }


def _path_for_node(
    node: dict[str, Any], by_internal_id: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[int] = set()
    current: dict[str, Any] | None = node
    while current is not None:
        internal_id = int(current["id"])
        if internal_id in seen:
            raise ValueError(f"classifier cycle at {internal_id}")
        seen.add(internal_id)
        result.append(current)
        parent_id = current.get("parentId")
        current = by_internal_id.get(int(parent_id)) if parent_id is not None else None
    return list(reversed(result))


def _contest_sets(
    election: dict[str, Any],
    candidates: list[dict[str, Any]],
    associations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    election_id = str(election["externalId"])
    major = [item for item in candidates if item["scope"] in ("majoritarian", "direct")]
    by_district: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for candidate in major:
        by_district[int(candidate.get("districtNumber") or 0)].append(candidate)
    for district, rows in sorted(by_district.items()):
        key = f"{election_id}:direct:{district or 'all'}"
        result.append(
            {
                "candidateSetKey": key,
                "electionId": election_id,
                "ballotType": "candidate",
                "scope": "district" if district else "election_wide",
                "districtNumber": district or None,
                "candidateIds": [item["candidateId"] for item in rows],
                "activeCandidateIds": [
                    item["candidateId"] for item in rows if item["ballotEligible"]
                ],
            }
        )
    proportional = [item for item in candidates if item["scope"] == "proportional"]
    system_code = _dict_external(election.get("systemType"))
    if proportional or (associations and system_code in {"2", "3", "7"}):
        association_by_name = {item["name"]: item for item in associations}
        unmatched = sorted(
            {
                str(item["associationName"])
                for item in proportional
                if item.get("associationName") not in association_by_name
            }
        )
        result.append(
            {
                "candidateSetKey": f"{election_id}:proportional",
                "electionId": election_id,
                "ballotType": "association",
                "scope": "election_wide",
                "associationIds": [item["associationId"] for item in associations],
                "registeredAssociationIds": [
                    item["associationId"]
                    for item in associations
                    if item["registrationMark"] == "registered"
                ],
                "unresolvedAssociationIds": [
                    item["associationId"]
                    for item in associations
                    if item["registrationMark"] != "registered"
                ],
                "listCandidateIds": [item["candidateId"] for item in proportional],
                "unmatchedCandidateAssociationNames": unmatched,
            }
        )
    return result


def _election_metadata(
    raw: dict[str, Any], source: dict[str, Any] | None = None
) -> dict[str, Any]:
    region_code = _region_code(raw.get("subjectRf"))
    level_code = _dict_external(raw.get("electionLevel"))
    return {
        "electionId": str(raw["externalId"]),
        "internalId": int(raw["id"]),
        "name": raw.get("name"),
        "votingDate": raw.get("votingDate"),
        "startVotingDay": raw.get("startVotingDay"),
        "finishVotingDay": raw.get("finishVotingDay"),
        "campaignScope": _campaign_scope(level_code),
        "levelCode": level_code,
        "level": _dict_value(raw.get("electionLevel")),
        "regionCode": region_code,
        "region": _dict_value(raw.get("subjectRf")),
        "territorialStatus": _territorial_status(region_code),
        "territory": _dict_value(raw.get("ate")),
        "kindCode": _dict_external(raw.get("kind")),
        "kind": _dict_value(raw.get("kind")),
        "systemCode": _dict_external(raw.get("systemType")),
        "system": _dict_value(raw.get("systemType")),
        "heldStatus": _dict_value(raw.get("heldStatus")),
        "validStatus": _dict_value(raw.get("validStatus")),
        "electionStatus": _dict_value(raw.get("electionStatus")),
        "revotingStatus": _dict_value(raw.get("revotingStatus")),
        "source": source,
    }


def generate(args: argparse.Namespace) -> int:
    index = json.loads(
        (args.staging_dir.parent / "index.json").read_text(encoding="utf-8")
    )
    expected_ids = set(map(int, index["electionIds"]))
    staging_files = sorted(args.staging_dir.glob("election-*.json"))
    datasets = [json.loads(path.read_text(encoding="utf-8")) for path in staging_files]
    actual_ids = {int(item["election"]["id"]) for item in datasets}
    if actual_ids != expected_ids:
        missing = sorted(expected_ids - actual_ids)
        extra = sorted(actual_ids - expected_ids)
        raise ValueError(
            f"staging election mismatch: missing={missing[:20]}, extra={extra[:20]}"
        )
    if not args.allow_partial and any(not item.get("complete") for item in datasets):
        partial = [
            item["election"]["id"] for item in datasets if not item.get("complete")
        ]
        raise ValueError(
            f"partial official campaigns cannot be generated: {partial[:20]}"
        )

    baseline = _load_baseline(args.baseline)
    elections_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    candidates_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    associations_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    sets_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    assignments: dict[tuple[str, int], dict[str, Any]] = {}
    coverage_regions: dict[str, Counter[str]] = defaultdict(Counter)
    source_hashes: set[str] = set()
    unresolved_assignments: list[dict[str, Any]] = []
    assumed_candidate_set_assignments: list[dict[str, Any]] = []
    official_source_gaps: list[dict[str, Any]] = []
    campaigns_without_uiks: list[dict[str, Any]] = []
    candidate_elections_without_candidates: list[dict[str, Any]] = []

    for dataset in sorted(datasets, key=lambda item: int(item["election"]["id"])):
        raw_election = dataset["election"]
        election = _election_metadata(raw_election, index.get("electionsSource"))
        election_region = election["regionCode"]
        elections_by_region[election_region].append(election)
        candidates: list[dict[str, Any]] = []
        for branch in dataset.get("candidateBranches") or []:
            for page in branch.get("pages") or []:
                source = page["source"]
                if source.get("sha256"):
                    source_hashes.add(source["sha256"])
                candidates.extend(
                    _candidate(row, raw_election, branch["scope"], source)
                    for row in page.get("rows") or []
                )
        associations: list[dict[str, Any]] = []
        association_report = dataset.get("associationReport")
        if association_report:
            source = association_report["source"]
            if source.get("sha256"):
                source_hashes.add(source["sha256"])
            associations = [
                _association(row, raw_election, source)
                for row in association_report.get("rows") or []
            ]
        candidates.sort(key=lambda item: (item["fullName"] or "", item["candidateId"]))
        associations.sort(
            key=lambda item: (
                item["drawNumber"] is None,
                item["drawNumber"] or 0,
                item["name"] or "",
            )
        )
        candidates_by_region[election_region].extend(candidates)
        associations_by_region[election_region].extend(associations)
        contest_sets = _contest_sets(raw_election, candidates, associations)
        sets_by_region[election_region].extend(contest_sets)
        sets_by_key = {item["candidateSetKey"]: item for item in contest_sets}
        direct_sets = [
            item for item in contest_sets if item["ballotType"] == "candidate"
        ]

        classifier_nodes = dataset.get("classifier", {}).get("nodes") or []
        gaps = []
        gap_details: list[dict[str, Any]] = []
        if not dataset.get("classifier", {}).get("complete"):
            gaps.append("classifier")
            gap_details.append(
                {
                    "component": "classifier",
                    "sources": dataset.get("classifier", {}).get("sources") or [],
                }
            )
        for branch in dataset.get("candidateBranches") or []:
            if branch.get("complete"):
                continue
            component = f"candidates:{branch.get('scope')}"
            gaps.append(component)
            gap_details.append(
                {"component": component, "sources": branch.get("sources") or []}
            )
        if association_report and not association_report.get("complete"):
            gaps.append("associations")
            gap_details.append(
                {
                    "component": "associations",
                    "sources": [association_report["source"]],
                }
            )
        for detail in gap_details:
            for source in detail["sources"]:
                if source.get("sha256"):
                    source_hashes.add(source["sha256"])
        if gaps:
            official_source_gaps.append(
                {
                    "electionId": election["electionId"],
                    "internalId": election["internalId"],
                    "name": election["name"],
                    "missing": gaps,
                    "details": gap_details,
                }
            )
        by_internal_id = {int(item["id"]): item for item in classifier_nodes}
        classifier_district_ids = {
            str(item["externalId"])
            for item in classifier_nodes
            if int(item.get("type", -1)) == 3
        }
        classifier_uik_count = sum(
            int(node.get("type", -1) == 5) for node in classifier_nodes
        )
        if classifier_uik_count == 0:
            campaigns_without_uiks.append(
                {
                    "electionId": election["electionId"],
                    "internalId": election["internalId"],
                    "name": election["name"],
                }
            )
        if election["kindCode"] in {"1", "2"} and not candidates:
            candidate_elections_without_candidates.append(
                {
                    "electionId": election["electionId"],
                    "internalId": election["internalId"],
                    "name": election["name"],
                }
            )
        for node in classifier_nodes:
            if int(node.get("type", -1)) != 5:
                continue
            match = UIK_RE.fullmatch(str(node.get("name") or "").strip())
            uik_number = int(node.get("number") or (match.group(1) if match else 0))
            if uik_number <= 0:
                continue
            path = _path_for_node(node, by_internal_id)
            regional = next(
                (item for item in reversed(path) if int(item.get("type", -1)) == 2),
                None,
            )
            district = next(
                (item for item in reversed(path) if int(item.get("type", -1)) == 3),
                None,
            )
            territorial = next(
                (item for item in reversed(path) if int(item.get("type", -1)) == 4),
                None,
            )
            region_code = (
                _region_code(regional.get("number")) if regional else election_region
            )
            key = (region_code, uik_number)
            record = assignments.setdefault(
                key,
                {
                    "uikKey": f"{region_code}:{uik_number}",
                    "regionCode": region_code,
                    "uikNumber": uik_number,
                    "uikNames": set(),
                    "assignments": {},
                },
            )
            record["uikNames"].add(str(node.get("name") or f"УИК №{uik_number}"))
            election_assignment = record["assignments"].setdefault(
                election["electionId"],
                {
                    "electionId": election["electionId"],
                    "electionName": election["name"],
                    "levelCode": election["levelCode"],
                    "kindCode": election["kindCode"],
                    "candidateSetRegionCode": election_region,
                    "officialClassifierPaths": [],
                    "candidateSetKeys": set(),
                    "mappingStatus": "official_classifier",
                },
            )
            path_source = (node.get("sources") or [{}])[0]
            if path_source.get("sha256"):
                source_hashes.add(path_source["sha256"])
            election_assignment["officialClassifierPaths"].append(
                {
                    "uikClassifierId": str(node["externalId"]),
                    "district": {
                        "classifierId": str(district["externalId"]),
                        "number": int(district.get("number") or 0),
                        "name": district.get("name"),
                    }
                    if district
                    else None,
                    "territorial": {
                        "classifierId": str(territorial["externalId"]),
                        "number": int(territorial.get("number") or 0),
                        "name": territorial.get("name"),
                    }
                    if territorial
                    else None,
                    "classifierBranch": next(
                        (
                            item.get("classifierBranch")
                            for item in reversed(path)
                            if item.get("classifierBranch")
                        ),
                        None,
                    ),
                    "source": path_source,
                }
            )
            district_number = int(district.get("number") or 0) if district else 0
            district_key = f"{election['electionId']}:direct:{district_number or 'all'}"
            election_wide_key = f"{election['electionId']}:direct:all"
            proportional_key = f"{election['electionId']}:proportional"
            if district_key in sets_by_key:
                election_assignment["candidateSetKeys"].add(district_key)
                election_assignment["candidateMapping"] = "official_district_number"
            elif election_wide_key in sets_by_key:
                election_assignment["candidateSetKeys"].add(election_wide_key)
                election_assignment["candidateMapping"] = "official_election_scope"
            elif (
                district and len(classifier_district_ids) == 1 and len(direct_sets) == 1
            ):
                assumed_set = direct_sets[0]
                election_assignment["candidateSetKeys"].add(
                    assumed_set["candidateSetKey"]
                )
                election_assignment["mappingStatus"] = (
                    "official_classifier_assumed_candidate_set"
                )
                election_assignment["candidateMapping"] = (
                    "assumed_unique_district_number_mismatch"
                )
                election_assignment["candidateMappingEvidence"] = {
                    "classifierDistrictNumber": district_number,
                    "candidateDistrictNumber": assumed_set.get("districtNumber"),
                    "reason": (
                        "The official classifier and candidate registry each expose "
                        "exactly one district, but their district numbers differ."
                    ),
                }
            if proportional_key in sets_by_key:
                election_assignment["candidateSetKeys"].add(proportional_key)
                election_assignment["proportionalMapping"] = "official_election_scope"
            if (
                candidates
                and not election_assignment["candidateSetKeys"]
                and election["kindCode"] in ("1", "2")
            ):
                election_assignment["mappingStatus"] = "unresolved_candidate_set"

        coverage_regions[election_region]["elections"] += 1
        coverage_regions[election_region]["candidates"] += len(candidates)
        coverage_regions[election_region]["associations"] += len(associations)
        coverage_regions[election_region]["classifierNodes"] += len(classifier_nodes)
        coverage_regions[election_region]["classifierUiks"] += classifier_uik_count

    for key, baseline_row in baseline.items():
        record = assignments.setdefault(
            key,
            {
                "uikKey": f"{key[0]}:{key[1]}",
                "regionCode": key[0],
                "uikNumber": key[1],
                "uikNames": {baseline_row.get("uikName") or f"УИК №{key[1]}"},
                "assignments": {},
            },
        )
        record["baseline2024"] = {
            "uikTvd": baseline_row.get("uikTvd"),
            "uikName": baseline_row.get("uikName"),
            "tikTvd": baseline_row.get("tikTvd"),
            "tikName": baseline_row.get("tikName"),
        }

    uiks_by_region: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for key in sorted(
        assignments,
        key=lambda item: (int(item[0]) if item[0].isdigit() else 999, item[1]),
    ):
        record = assignments[key]
        assignment_values = []
        for election_id, item in sorted(record["assignments"].items()):
            normalized = {**item, "candidateSetKeys": sorted(item["candidateSetKeys"])}
            assignment_values.append(normalized)
            if normalized["mappingStatus"] == "unresolved_candidate_set":
                unresolved_assignments.append(
                    {"uikKey": record["uikKey"], "electionId": election_id}
                )
            elif normalized["mappingStatus"] == (
                "official_classifier_assumed_candidate_set"
            ):
                assumed_candidate_set_assignments.append(
                    {
                        "uikKey": record["uikKey"],
                        "electionId": election_id,
                        "candidateSetKeys": normalized["candidateSetKeys"],
                        "evidence": normalized["candidateMappingEvidence"],
                    }
                )
        baseline_present = "baseline2024" in record
        current_present = bool(assignment_values)
        if baseline_present and current_present:
            existing_status = "matched_2024_region_number"
        elif baseline_present:
            existing_status = "not_assigned_on_voting_date"
        else:
            existing_status = "new_or_renumbered_since_2024"
        normalized_record = {
            "uikKey": record["uikKey"],
            "regionCode": record["regionCode"],
            "territorialStatus": _territorial_status(record["regionCode"]),
            "uikNumber": record["uikNumber"],
            "uikNames": sorted(record["uikNames"]),
            "identityAssumption": "same_region_and_uik_number",
            "existingStatus": existing_status,
            "baseline2024": record.get("baseline2024"),
            "assignments": assignment_values,
        }
        uiks_by_region[record["regionCode"]].append(normalized_record)

    def write_shards(folder: str, values: dict[str, list[dict[str, Any]]]) -> None:
        target = args.output / folder
        target.mkdir(parents=True, exist_ok=True)
        expected = set()
        for region, rows in sorted(
            values.items(),
            key=lambda item: (int(item[0]) if item[0].isdigit() else 999, item[0]),
        ):
            path = target / f"region-{region}.json"
            json_write(path, rows)
            expected.add(path.name)
        for path in target.glob("region-*.json"):
            if path.name not in expected:
                path.unlink()

    write_shards("elections", elections_by_region)
    write_shards("candidates", candidates_by_region)
    write_shards("associations", associations_by_region)
    write_shards("candidate-sets", sets_by_region)
    write_shards("uiks", uiks_by_region)

    status_counts = Counter(
        item["existingStatus"] for rows in uiks_by_region.values() for item in rows
    )
    coverage = {
        "schemaVersion": 1,
        "votingDate": index["votingDate"],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "campaigns": sum(len(rows) for rows in elections_by_region.values()),
        "candidates": sum(len(rows) for rows in candidates_by_region.values()),
        "associations": sum(len(rows) for rows in associations_by_region.values()),
        "candidateSets": sum(len(rows) for rows in sets_by_region.values()),
        "canonicalUiks": sum(len(rows) for rows in uiks_by_region.values()),
        "uikAssignments": sum(
            len(item["assignments"])
            for rows in uiks_by_region.values()
            for item in rows
        ),
        "existingStatusCounts": dict(sorted(status_counts.items())),
        "unresolvedCandidateSetAssignments": unresolved_assignments,
        "assumedCandidateSetAssignments": assumed_candidate_set_assignments,
        "officialSourceGaps": official_source_gaps,
        "campaignsWithoutUiks": campaigns_without_uiks,
        "candidateElectionsWithoutCandidates": candidate_elections_without_candidates,
        "sourceResponseCount": len(source_hashes),
        "sourceResponseSha256": sorted(source_hashes),
        "regions": [
            {"regionCode": region, **dict(counts)}
            for region, counts in sorted(
                coverage_regions.items(),
                key=lambda item: (int(item[0]) if item[0].isdigit() else 999, item[0]),
            )
        ],
        "complete": not unresolved_assignments and not official_source_gaps,
    }
    json_write(args.output / "coverage.json", coverage)
    json_write(args.output / "status-dictionaries.json", index["statusDictionaries"])
    json_write(
        args.output / "metadata.json",
        {
            "schemaVersion": 1,
            "votingDate": index["votingDate"],
            "scope": "all campaigns returned by the official CEC calendar for the voting date",
            "electionsSource": index.get("electionsSource"),
            "identityAssumption": (
                "Election-specific classifier UIKs are treated as the same existing UIK "
                "when region code and UIK number agree. Each election assignment itself is "
                "retained with its official classifier UUID and source hash."
            ),
            "associationRegistrationMark": (
                "not_marked_registered means the type-236 response has no registration "
                "decision fields. The official portal warns that those fields can also be "
                "absent after cancellation or annulment, so ballotEligibility remains null."
            ),
            "territorialStatus": {
                "codes": TERRITORIES_INTERNATIONALLY_RECOGNIZED_AS_UKRAINE,
                "cecClassification": "subject_of_russian_federation",
                "internationalRecognition": "part_of_ukraine",
                "unReferences": [
                    "https://docs.un.org/A/RES/68/262",
                    "https://docs.un.org/A/RES/ES-11/4",
                ],
            },
        },
    )
    print(json.dumps(coverage, ensure_ascii=False, indent=2), flush=True)
    return 0 if coverage["complete"] or args.allow_partial else 1


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Snapshot current official 2026 campaigns, UIKs, and candidate sets"
    )
    commands = result.add_subparsers(dest="command", required=True)
    network = argparse.ArgumentParser(add_help=False)
    network.add_argument("--rate", type=float, default=10.0)
    network.add_argument("--concurrency", type=int, default=8)
    network.add_argument("--timeout", type=float, default=30.0)
    network.add_argument("--retries", type=int, default=5)
    network.add_argument("--backoff-initial", type=float, default=0.5)
    network.add_argument("--backoff-max", type=float, default=30.0)
    network.add_argument("--jitter", type=float, default=0.1)
    network.add_argument(
        "--coordination-dir", type=Path, default=DEFAULT_COORDINATION_DIR
    )
    network.add_argument("--refresh", action="store_true")

    command = commands.add_parser("crawl", parents=[network])
    command.set_defaults(func=crawl)
    command.add_argument("--voting-date", default=DEFAULT_VOTING_DATE)
    command.add_argument("--raw-dir", type=Path, default=DEFAULT_RAW)
    command.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING)
    command.add_argument("--region", action="append")
    command.add_argument("--election-id", action="append")
    command.add_argument("--limit", type=int)
    command.add_argument("--progress-every", type=int, default=25)
    command.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_STAGING.parent / "crawl.json",
    )

    command = commands.add_parser("crawl-declarations", parents=[network])
    command.set_defaults(func=crawl_declarations)
    command.add_argument(
        "--election-external-id", default=DEFAULT_DUMA_ELECTION_EXTERNAL_ID
    )
    command.add_argument("--raw-dir", type=Path, default=DEFAULT_DECLARATIONS_RAW)
    command.add_argument("--output", type=Path, default=DEFAULT_DECLARATIONS_OUTPUT)
    command.add_argument("--page-size", type=int, default=1000)
    command.add_argument("--expect-count", type=int)
    command.add_argument("--progress-every", type=int, default=10)

    command = commands.add_parser("generate")
    command.set_defaults(func=generate)
    command.add_argument("--staging-dir", type=Path, default=DEFAULT_STAGING)
    command.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    command.add_argument("--allow-partial", action="store_true")
    return result


def main() -> int:
    args = parser().parse_args()
    if hasattr(args, "rate") and (
        args.rate <= 0 or args.concurrency < 1 or args.retries < 0
    ):
        raise SystemExit("rate/concurrency must be positive and retries non-negative")
    if hasattr(args, "page_size") and (
        args.page_size < 1
        or args.progress_every < 1
        or (args.expect_count is not None and args.expect_count < 0)
    ):
        raise SystemExit(
            "page size/progress must be positive and expected count non-negative"
        )
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
