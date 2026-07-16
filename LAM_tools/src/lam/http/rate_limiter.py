from __future__ import annotations

import threading
import time
from collections.abc import Callable


class RateLimiter:
    """A synchronous, provider-local minimum-interval limiter."""

    def __init__(
        self,
        minimum_interval_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.minimum_interval_seconds = max(0.0, minimum_interval_seconds)
        self._clock = clock
        self._sleep = sleeper
        self._last_started: float | None = None
        self._lock = threading.Lock()

    def acquire(self) -> float:
        with self._lock:
            now = self._clock()
            waited = 0.0
            if self._last_started is not None:
                waited = max(
                    0.0,
                    self.minimum_interval_seconds - (now - self._last_started),
                )
                if waited:
                    self._sleep(waited)
                    now = self._clock()
            self._last_started = now
            return waited
