from __future__ import annotations

import email.utils
import json
import random
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .common import atomic_write, build_request, json_write, opener, sha256_bytes
except ImportError:
    from common import atomic_write, build_request, json_write, opener, sha256_bytes


RETRYABLE_STATUS = frozenset({429, 502, 503, 504})


class GlobalRateLimiter:
    """Thread-safe, process-wide evenly spaced request scheduler."""

    def __init__(
        self,
        rate: float = 10.0,
        *,
        jitter: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        if rate <= 0 or not 0 <= jitter <= 1:
            raise ValueError("rate must be positive and jitter must be between 0 and 1")
        self.interval = 1.0 / rate
        self.jitter = jitter
        self.clock = clock
        self.sleep = sleep
        self.random = random_source or random.Random()
        self._lock = threading.Lock()
        self._next = 0.0
        self._cooldown_until = 0.0

    def wait(self) -> float:
        while True:
            with self._lock:
                now = self.clock()
                scheduled = max(now, self._next, self._cooldown_until)
                # Jitter is delay-only so it cannot raise the long-run request rate.
                delay = self.random.uniform(0, self.interval * self.jitter)
                scheduled += delay
                self._next = scheduled + self.interval
            remaining = scheduled - self.clock()
            if remaining > 0:
                self.sleep(remaining)
            # A retry in another worker may have imposed a cooldown after this slot
            # was reserved. Discard the stale slot rather than launching through it.
            with self._lock:
                cooldown = self._cooldown_until
            if cooldown <= self.clock():
                return scheduled

    def penalize(self, seconds: float) -> None:
        if seconds <= 0:
            return
        with self._lock:
            self._cooldown_until = max(self._cooldown_until, self.clock() + seconds)


def retry_after_seconds(
    value: str | None, *, now: datetime | None = None
) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value.strip()))
    except ValueError:
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    current = now or datetime.now(timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - current).total_seconds())


@dataclass(frozen=True)
class FetchConfig:
    timeout: float = 20.0
    retries: int = 5
    backoff_initial: float = 0.5
    backoff_max: float = 30.0


class ResponseStore:
    """Content-addressed raw bodies plus an atomically replaced request index."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.bodies = root / "sha256"
        self.index_path = root / "manifest.json"
        self._lock = threading.Lock()
        self._index: dict[str, dict[str, Any]] = self._read_index()

    @staticmethod
    def identity(url: str) -> str:
        return sha256_bytes(url.encode("utf-8"))

    def _read_index(self) -> dict[str, dict[str, Any]]:
        if not self.index_path.exists():
            return {}
        value = json.loads(self.index_path.read_text(encoding="utf-8"))
        return value.get("requests", {}) if isinstance(value, dict) else {}

    def verified(self, url: str) -> dict[str, Any] | None:
        record = self._index.get(self.identity(url))
        if not record or not record.get("sha256"):
            return None
        body = self.root / record["body_path"]
        if not body.is_file() or sha256_bytes(body.read_bytes()) != record["sha256"]:
            return None
        return record

    def save(
        self, url: str, payload: bytes, metadata: dict[str, Any]
    ) -> dict[str, Any]:
        digest = sha256_bytes(payload)
        body = self.bodies / digest[:2] / digest
        if not body.exists():
            atomic_write(body, payload)
        record = {
            "request_id": self.identity(url),
            "requested_url": url,
            **metadata,
            "byte_length": len(payload),
            "sha256": digest,
            "body_path": str(body.relative_to(self.root)),
        }
        with self._lock:
            previous = self._index.get(record["request_id"], {})
            history = list(previous.get("observations", []))
            history.append(
                {key: value for key, value in record.items() if key != "observations"}
            )
            record["observations"] = history
            self._index[record["request_id"]] = record
            json_write(
                self.index_path,
                {"schema_version": 1, "requests": dict(sorted(self._index.items()))},
            )
        return record

    def save_failure(self, url: str, metadata: dict[str, Any]) -> dict[str, Any]:
        record = {"request_id": self.identity(url), "requested_url": url, **metadata}
        with self._lock:
            previous = self._index.get(record["request_id"], {})
            history = list(previous.get("observations", []))
            history.append(
                {key: value for key, value in record.items() if key != "observations"}
            )
            record["observations"] = history
            self._index[record["request_id"]] = record
            json_write(
                self.index_path,
                {"schema_version": 1, "requests": dict(sorted(self._index.items()))},
            )
        return record


class Fetcher:
    def __init__(
        self,
        store: ResponseStore,
        limiter: GlobalRateLimiter,
        config: FetchConfig | None = None,
        *,
        client: Any | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        self.store, self.limiter, self.config = store, limiter, config or FetchConfig()
        self.client, self.clock, self.sleep = client or opener(), clock, sleep
        self.random = random_source or random.Random()

    def fetch(self, url: str, *, refresh: bool = False) -> dict[str, Any]:
        if not refresh and (existing := self.store.verified(url)):
            status = int(existing.get("status", 0))
            if 200 <= status < 400:
                return {**existing, "cache_hit": True}
        last_error = ""
        for attempt in range(self.config.retries + 1):
            self.limiter.wait()
            started = self.clock()
            request = build_request(url)
            try:
                response = self.client.open(request, timeout=self.config.timeout)
            except urllib.error.HTTPError as error:
                response = error
            except (TimeoutError, OSError, urllib.error.URLError) as error:
                last_error = f"{type(error).__name__}: {error}"
                if attempt < self.config.retries:
                    self._backoff(attempt, None)
                    continue
                return self.store.save_failure(
                    url, self._failure(last_error, attempt, started)
                )
            with response:
                payload = response.read()
                status = int(
                    getattr(response, "status", 0) or getattr(response, "code", 0)
                )
                headers = response.headers
                metadata = {
                    "final_url": response.geturl(),
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "status": status,
                    "content_type": headers.get("Content-Type", ""),
                    "elapsed_seconds": round(self.clock() - started, 6),
                    "retry_count": attempt,
                    "source_host": urllib.parse.urlsplit(response.geturl()).netloc,
                    "provenance": "wayback"
                    if "web.archive.org" in response.geturl()
                    else "live-official",
                }
            record = self.store.save(url, payload, metadata)
            if status in RETRYABLE_STATUS and attempt < self.config.retries:
                self._backoff(attempt, retry_after_seconds(headers.get("Retry-After")))
                continue
            return record
        raise AssertionError(last_error)

    def _failure(self, error: str, attempt: int, started: float) -> dict[str, Any]:
        return {
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "error": error,
            "error_class": error.split(":", 1)[0],
            "elapsed_seconds": round(self.clock() - started, 6),
            "retry_count": attempt,
        }

    def _backoff(self, attempt: int, retry_after: float | None) -> None:
        base = min(self.config.backoff_max, self.config.backoff_initial * (2**attempt))
        delay = (
            retry_after
            if retry_after is not None
            else self.random.uniform(base / 2, base)
        )
        self.limiter.penalize(delay)
