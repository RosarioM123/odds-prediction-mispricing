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

    Raises:
        RateLimitError: on HTTP 429.
        VenueError: on other HTTP errors, timeouts, or bad JSON.
    """
    last_exc: Exception | None = None
    with _client() as client:
        for attempt in range(retries + 1):
            try:
                resp = client.get(url, params=params, timeout=timeout)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_exc = exc
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code == 429:
                raise RateLimitError(f"429 from {url}")
            if resp.status_code >= 400:
                raise VenueError(f"HTTP {resp.status_code} from {url}: {resp.text[:200]}")
            try:
                # resp.json() is untyped (Any); bind to the declared return
                # type so no implicit Any leaks to callers.
                payload: dict[str, Any] | list[Any] = resp.json()
                return payload
            except ValueError as exc:
                raise VenueError(f"non-JSON response from {url}") from exc
    raise VenueError(f"request to {url} failed after {retries + 1} attempts: {last_exc}")
