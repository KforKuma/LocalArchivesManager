from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import NetworkConfig
from ..exceptions import NetworkError
from .rate_limiter import RateLimiter
from .retry import RetryPolicy


LOGGER = logging.getLogger(__name__)
SECRET_PARAMETERS = {"api_key", "apikey", "token", "access_token", "key"}


@dataclass(slots=True)
class HttpResult:
    status_code: int
    content: bytes
    headers: dict[str, str]
    request_count: int
    retries: int
    rate_limit_wait_seconds: float

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


class HttpClient:
    def __init__(
        self,
        provider: str,
        config: NetworkConfig,
        rate_limiter: RateLimiter,
        retry_policy: RetryPolicy | None = None,
        *,
        client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.provider = provider
        self.config = config
        self.rate_limiter = rate_limiter
        self.retry_policy = retry_policy or RetryPolicy(config.max_retries)
        self._sleep = sleeper
        # httpx/httpcore may otherwise log query strings (including api_key)
        # at INFO/DEBUG. LAM emits its own redacted request diagnostics.
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        timeout = httpx.Timeout(
            config.timeout_seconds,
            connect=config.connect_timeout_seconds,
            read=config.read_timeout_seconds,
        )
        self._client = client or httpx.Client(
            timeout=timeout,
            headers={"User-Agent": config.user_agent},
            follow_redirects=True,
        )
        self._owns_client = client is None

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        safe_fingerprint = self._fingerprint(params or {})
        requests = 0
        retries = 0
        limiter_wait = 0.0
        for attempt in range(self.retry_policy.max_retries + 1):
            limiter_wait += self.rate_limiter.acquire()
            requests += 1
            started = time.monotonic()
            try:
                with self._client.stream(
                    "GET", url, params=params, headers=headers
                ) as response:
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > self.config.max_response_bytes:
                        raise NetworkError(
                            f"{self.provider} response exceeds configured size limit"
                        )
                    chunks: list[bytes] = []
                    size = 0
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > self.config.max_response_bytes:
                            raise NetworkError(
                                f"{self.provider} response exceeds configured size limit"
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    status = response.status_code
                    response_headers = {
                        key.casefold(): value for key, value in response.headers.items()
                    }
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt >= self.retry_policy.max_retries:
                    raise NetworkError(
                        f"{self.provider} request failed after {requests} attempt(s): "
                        f"{type(exc).__name__}"
                    ) from exc
                delay = self.retry_policy.delay(attempt)
                retries += 1
                self._sleep(delay)
                continue

            LOGGER.debug(
                "provider_request provider=%s endpoint=%s status=%s elapsed_ms=%d "
                "retry=%d query=%s",
                self.provider,
                httpx.URL(url).path,
                status,
                round((time.monotonic() - started) * 1000),
                retries,
                safe_fingerprint,
            )
            if self.retry_policy.retryable_status(status):
                if attempt >= self.retry_policy.max_retries:
                    raise NetworkError(
                        f"{self.provider} returned HTTP {status} after {requests} attempt(s)"
                    )
                delay = self.retry_policy.delay(
                    attempt, response_headers.get("retry-after")
                )
                retries += 1
                self._sleep(delay)
                continue
            return HttpResult(
                status,
                content,
                response_headers,
                requests,
                retries,
                limiter_wait,
            )
        raise NetworkError(f"{self.provider} retry loop ended unexpectedly")

    @staticmethod
    def _fingerprint(params: Mapping[str, Any]) -> str:
        safe = [
            (str(key), "<redacted>" if str(key).casefold() in SECRET_PARAMETERS else str(value))
            for key, value in sorted(params.items(), key=lambda item: str(item[0]))
        ]
        return hashlib.sha256(repr(safe).encode("utf-8")).hexdigest()[:12]
