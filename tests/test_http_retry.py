"""Tests for HTTP retry behavior in backend.markets.http.

ALL INPUTS SYNTHETIC: every response below is a hand-built fake. No test
here touches the network, and no timing claim is real: ``time.sleep`` is
replaced with a recorder.

Covers: 429 retried with Retry-After honored, 429 retried with
exponential backoff when the header is absent, transient 5xx retried,
non-retryable 4xx raising immediately, transport errors retried, retry
exhaustion raising a clear final error, and success after retries.
"""

import httpx
import pytest

from backend.errors import RateLimitError, VenueError
from backend.markets import http


class FakeResponse:
    def __init__(self, status_code, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = str(payload)[:200]

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeClient:
    """Context manager standing in for httpx.Client."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        action = self._script.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


@pytest.fixture()
def harness(monkeypatch):
    """Patch the client factory and sleep; return (sleeps, install)."""
    sleeps = []
    monkeypatch.setattr(http, "_sleep", sleeps.append)

    def install(script):
        client = FakeClient(script)
        monkeypatch.setattr(http, "_client", lambda: client)
        return client

    return sleeps, install


def test_429_honors_retry_after(harness):
    sleeps, install = harness
    client = install(
        [
            FakeResponse(429, headers={"retry-after": "3"}),
            FakeResponse(200, {"ok": True}),
        ]
    )
    assert http.get_json("https://example.com/x") == {"ok": True}
    assert client.calls == 2
    assert sleeps == [3.0]


def test_429_without_header_uses_exponential_backoff(harness):
    sleeps, install = harness
    client = install([FakeResponse(429), FakeResponse(429), FakeResponse(200, [1, 2])])
    assert http.get_json("https://example.com/x", retries=2) == [1, 2]
    assert client.calls == 3
    assert sleeps == [1.0, 2.0]


def test_retry_after_capped(harness):
    sleeps, install = harness
    install([FakeResponse(429, headers={"retry-after": "600"}), FakeResponse(200, {})])
    http.get_json("https://example.com/x", retries=1)
    assert sleeps == [http.BACKOFF_CAP_S]


def test_transient_5xx_retried(harness):
    sleeps, install = harness
    client = install([FakeResponse(503), FakeResponse(500), FakeResponse(200, {"ok": True})])
    assert http.get_json("https://example.com/x", retries=2) == {"ok": True}
    assert client.calls == 3
    assert sleeps == [1.0, 2.0]


def test_429_exhaustion_raises_rate_limit(harness):
    sleeps, install = harness
    install([FakeResponse(429), FakeResponse(429), FakeResponse(429)])
    with pytest.raises(RateLimitError, match="after 3 attempts"):
        http.get_json("https://example.com/x", retries=2)
    assert sleeps == [1.0, 2.0]


def test_5xx_exhaustion_raises_venue_error(harness):
    _, install = harness
    install([FakeResponse(503), FakeResponse(503)])
    with pytest.raises(VenueError, match="HTTP 503.*after 2 attempts"):
        http.get_json("https://example.com/x", retries=1)


def test_client_error_raises_immediately(harness):
    sleeps, install = harness
    client = install([FakeResponse(400), FakeResponse(200, {})])
    with pytest.raises(VenueError, match="HTTP 400"):
        http.get_json("https://example.com/x", retries=2)
    assert client.calls == 1
    assert sleeps == []


def test_transport_errors_retried_then_fail(harness):
    _, install = harness
    install(
        [
            httpx.TimeoutException("boom"),
            httpx.TimeoutException("boom"),
            httpx.TimeoutException("boom"),
        ]
    )
    with pytest.raises(VenueError, match="failed after 3 attempts"):
        http.get_json("https://example.com/x", retries=2)


def test_non_json_raises_without_retry(harness):
    sleeps, install = harness
    client = install([FakeResponse(200, ValueError("bad json"))])
    with pytest.raises(VenueError, match="non-JSON"):
        http.get_json("https://example.com/x")
    assert client.calls == 1
    assert sleeps == []
