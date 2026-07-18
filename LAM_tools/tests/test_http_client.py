from __future__ import annotations

import logging

import httpx
import pytest

from lam.config import NetworkConfig
from lam.exceptions import NetworkError
from lam.http.client import HttpClient
from lam.http.rate_limiter import RateLimiter
from lam.http.retry import RetryPolicy


class FakeClock:
    def __init__(self):
        self.value = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.value

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.value += seconds


def client_for(handler, clock, *, retries=0, max_bytes=1024):
    raw = httpx.Client(transport=httpx.MockTransport(handler))
    config = NetworkConfig(max_retries=retries, max_response_bytes=max_bytes)
    limiter = RateLimiter(3.2, clock=clock.monotonic, sleeper=clock.sleep)
    return HttpClient(
        "test",
        config,
        limiter,
        RetryPolicy(retries, (1.0, 2.0)),
        client=raw,
        sleeper=clock.sleep,
    )


def test_success_and_provider_rate_limit_are_serial():
    clock = FakeClock()
    client = client_for(lambda request: httpx.Response(200, content=b"ok"), clock)
    first = client.get("https://example.test/one")
    second = client.get("https://example.test/two")
    assert first.content == b"ok"
    assert second.rate_limit_wait_seconds == pytest.approx(3.2)
    assert clock.sleeps == [pytest.approx(3.2)]


def test_retry_after_is_obeyed_and_api_key_is_not_logged(caplog):
    clock = FakeClock()
    responses = iter(
        [
            httpx.Response(429, headers={"Retry-After": "7"}),
            httpx.Response(200, content=b"done"),
        ]
    )
    client = client_for(lambda request: next(responses), clock, retries=1)
    caplog.set_level(logging.DEBUG)
    result = client.get(
        "https://example.test/data",
        params={"api_key": "TOP-SECRET-KEY", "id": "1"},
    )
    assert result.retries == 1
    assert 7.0 in clock.sleeps
    assert "TOP-SECRET-KEY" not in caplog.text


def test_non_retryable_400_is_returned_once():
    clock = FakeClock()
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(400, content=b"bad query")

    result = client_for(handler, clock, retries=3).get("https://example.test")
    assert result.status_code == 400
    assert calls == 1


def test_retry_exhaustion_and_response_size_limit():
    clock = FakeClock()
    failing = client_for(
        lambda request: httpx.Response(503), clock, retries=1
    )
    with pytest.raises(NetworkError, match="HTTP 503"):
        failing.get("https://example.test")

    oversized = client_for(
        lambda request: httpx.Response(200, content=b"12345"),
        FakeClock(),
        max_bytes=4,
    )
    with pytest.raises(NetworkError, match="size limit"):
        oversized.get("https://example.test")
