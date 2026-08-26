from __future__ import annotations

import fcntl
import json
import os
import random
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_COORDINATION_DIR = Path("data/raw/.gas-rate-limit")


class SharedRateLimiter:
    """Evenly schedule requests across threads and crawler processes.

    ``flock`` releases the lock when a process exits, so a killed crawler cannot
    strand the scheduler. A reserved slot may be lost, which reduces traffic but
    never creates a burst. Linux monotonic timestamps are comparable between
    processes on the same host and are not affected by wall-clock adjustments.
    """

    def __init__(
        self,
        rate: float = 10.0,
        *,
        coordination_dir: Path = DEFAULT_COORDINATION_DIR,
        jitter: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_source: random.Random | None = None,
    ) -> None:
        if rate <= 0 or not 0 <= jitter <= 1:
            raise ValueError("rate must be positive and jitter must be between 0 and 1")
        self.interval = 1.0 / rate
        self.jitter = jitter
        self.coordination_dir = coordination_dir
        self.lock_path = coordination_dir / "schedule.lock"
        self.state_path = coordination_dir / "schedule.json"
        self.clock = clock
        self.sleep = sleep
        self.random = random_source or random.Random()
        self._thread_lock = threading.Lock()

    def _read_state(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, value: dict[str, Any]) -> None:
        temporary = self.state_path.with_name(
            f".{self.state_path.name}.{os.getpid()}.{threading.get_ident()}.part"
        )
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(self.state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def wait(self) -> float:
        self.coordination_dir.mkdir(parents=True, exist_ok=True)
        while True:
            with self._thread_lock, self.lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                now = self.clock()
                state = self._read_state()
                previous = float(state.get("next_monotonic", 0.0))
                cooldown = float(state.get("cooldown_until", 0.0))
                # A reboot resets monotonic time. Treat distant state as stale.
                if previous > now + 86_400 or cooldown > now + 86_400:
                    previous = cooldown = 0.0
                scheduled = max(now, previous, cooldown)
                # Delay-only jitter cannot raise the shared long-run request rate.
                scheduled += self.random.uniform(0, self.interval * self.jitter)
                self._write_state(
                    {
                        "schema_version": 1,
                        "next_monotonic": scheduled + self.interval,
                        "cooldown_until": cooldown,
                        "owner_pid": os.getpid(),
                    }
                )
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            remaining = scheduled - self.clock()
            if remaining > 0:
                self.sleep(remaining)
            # Recheck only cooldown: other processes may legitimately have reserved
            # later slots, but a newly imposed backoff invalidates this launch.
            with self._thread_lock, self.lock_path.open("a+b") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                cooldown = float(self._read_state().get("cooldown_until", 0.0))
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
            if cooldown <= self.clock():
                return scheduled

    def penalize(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self.coordination_dir.mkdir(parents=True, exist_ok=True)
        with self._thread_lock, self.lock_path.open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            state = self._read_state()
            state["schema_version"] = 1
            state["cooldown_until"] = max(
                float(state.get("cooldown_until", 0.0)), self.clock() + seconds
            )
            state["owner_pid"] = os.getpid()
            self._write_state(state)
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
