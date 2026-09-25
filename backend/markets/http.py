"""Shared HTTP client for venue adapters.

All public market-data reads go through here: timeouts, retries with
backoff, a descriptive user agent, and typed errors. No credentials are
ever attached; every endpoint used by the adapters is public.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from backend.errors import RateLimitError, VenueError

# Re-exported so ``from backend.markets.http import VenueError`` keeps working.
__all__ = ["VenueError", "RateLimitError", "get_json", "USER_AGENT", "DEFAULT_TIMEOUT"]

USER_AGENT = "ODDS-research/0.1 (+https://github.com/RosarioM123/odds-prediction-mispricing)"
DEFAULT_TIMEOUT = 15.0

#: HTTP statuses worth one more attempt: rate limiting and transient
#: server-side failures. Anything else 4xx is a client error and raises.
RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})

#: Backoff bounds, seconds. Retry-After is honored but capped so one
#: hostile header cannot stall a scan loop indefinitely.
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0

# Module-level sleep so tests can monkeypatch it deterministically.
_sleep = time.sleep


def _retry_after_s(resp: httpx.Response) -> float | None:
    """Parse the Retry-After header as seconds, or None if absent/invalid."""
    raw = resp.headers.get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _backoff_s(attempt: int, resp: httpx.Response | None) -> float:
    """Seconds to wait before the next attempt (0-indexed ``attempt``)."""
    if resp is not None and resp.status_code == 429:
        hinted = _retry_after_s(resp)
        if hinted is not None:
            return float(min(hinted, BACKOFF_CAP_S))
    return float(min(BACKOFF_BASE_S * (2**attempt), BACKOFF_CAP_S))


def _client() -> httpx.Client:
    """Build a client that honors the egress proxy without tripping over
    bracketed IPv6 entries in NO_PROXY (which stock httpx mis-parses)."""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    verify: str | bool = os.environ.get("SSL_CERT_FILE", True)
    return httpx.Client(
        trust_env=False,
        proxy=proxy,
        verify=verify,
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
    )


def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = 2,
) -> dict[str, Any] | list[Any]:
    """GET a JSON document, retrying transient failures.

    Transport errors retry with linear backoff. HTTP 429 and transient
    5xx (500/502/503/504) retry with exponential backoff; a Retry-After
    header on a 429 is honored (capped). Other 4xx raise immediately.

    Raises:
        RateLimitError: on HTTP 429, immediately if ``retries`` is 0 or
            after the retry budget is exhausted.
        VenueError: on other HTTP errors, timeouts, bad JSON, or retry
            exhaustion.
    """
    last_exc: Exception | None = None
    last_status: int | None = None
    with _client() as client:
        for attempt in range(retries + 1):
            try:
                resp = client.get(url, params=params, timeout=timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                if attempt < retries:
                    _sleep(0.5 * (attempt + 1))
                continue
            if 200 <= resp.status_code < 300:
                try:
                    # resp.json() is untyped (Any); bind to the declared
                    # return type so no implicit Any leaks to callers.
                    payload: dict[str, Any] | list[Any] = resp.json()
                    return payload
                except ValueError as exc:
                    raise VenueError(f"non-JSON response from {url}") from exc
            last_status = resp.status_code
            if resp.status_code not in RETRYABLE_STATUS or attempt >= retries:
                break
            _sleep(_backoff_s(attempt, resp))
    if last_status == 429:
        raise RateLimitError(f"429 from {url} after {retries + 1} attempts")
    if last_status is not None:
        raise VenueError(f"HTTP {last_status} from {url} after {retries + 1} attempts")
    raise VenueError(f"request to {url} failed after {retries + 1} attempts: {last_exc}")
