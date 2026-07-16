from __future__ import annotations

import email.utils
from dataclasses import dataclass
from datetime import datetime, timezone


RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    delays: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0)

    def retryable_status(self, status_code: int) -> bool:
        return status_code in RETRYABLE_STATUS_CODES

    def delay(self, retry_number: int, retry_after: str | None = None) -> float:
        parsed = self._retry_after_seconds(retry_after)
        if parsed is not None:
            return parsed
        if not self.delays:
            return 0.0
        return self.delays[min(max(0, retry_number), len(self.delays) - 1)]

    @staticmethod
    def _retry_after_seconds(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value.strip()))
        except ValueError:
            try:
                parsed = email.utils.parsedate_to_datetime(value)
            except (TypeError, ValueError):
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())
