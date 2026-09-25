"""Shared live-venue polling for the scan loop and snapshot collector.

All reads are public and unauthenticated. Fetching is threaded (books
are independent) with a small politeness gap; observed usage stays far
below the documented venue tiers (Polymarket ~4000 reads/10s, Kalshi
~200 reads/s). Failures are per-market: a bad book is logged and
skipped, never fatal to the round.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from backend.errors import VenueError
from backend.markets.base import MarketDataAdapter
from backend.markets.kalshi import KalshiAdapter
from backend.markets.polymarket import PolymarketAdapter
from backend.schemas import Market, OrderBook, Venue

FETCH_GAP_SECONDS = 0.2


def venue_adapters(kalshi_env: str = "auto") -> dict[str, MarketDataAdapter]:
    """Build one adapter per venue.

    ``kalshi_env``: "auto" tries production first and falls back to
    demo when this network is rejected (the documented datacenter
    403); "prod" and "demo" pin the host.

    Raises:
        DataValidationError: if ``kalshi_env`` is not auto/prod/demo.
    """
    if kalshi_env not in ("auto", "prod", "demo"):
        from backend.errors import DataValidationError

        raise DataValidationError("kalshi_env must be auto, prod, or demo")
    kalshi: MarketDataAdapter
    if kalshi_env == "demo":
        kalshi = KalshiAdapter(env="demo")
    else:
        try:
            candidate = KalshiAdapter(env="prod")
            candidate.fetch_markets(limit=1)
            kalshi = candidate
        except VenueError:
            if kalshi_env == "prod":
                raise
            kalshi = KalshiAdapter(env="demo")
    return {"polymarket": PolymarketAdapter(), "kalshi": kalshi}


def _fetch_one(
    adapter: MarketDataAdapter, market: Market
) -> tuple[OrderBook | None, float, str | None]:
    t0 = time.perf_counter()
    try:
        book = adapter.fetch_order_book(market)
        return book, time.perf_counter() - t0, None
    except VenueError as exc:
        return None, time.perf_counter() - t0, str(exc)


def fetch_books(
    adapter: MarketDataAdapter,
    markets: list[Market],
    *,
    max_workers: int = 4,
    gap: float = FETCH_GAP_SECONDS,
) -> tuple[list[OrderBook], dict[str, Any]]:
    """Fetch order books for ``markets`` with a thread pool.

    Returns (books, stats) where stats carries per-book latencies and
    the failure count. Raises: None (per-market failures are recorded
    in stats, not raised).
    """
    books: list[OrderBook] = []
    latencies: list[float] = []
    failures = 0
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_fetch_one, adapter, m) for m in markets]
        for i, fut in enumerate(futures):
            book, latency, error = fut.result()
            latencies.append(latency)
            if book is None:
                failures += 1
                print(f"  book fetch failed ({error})", flush=True)
            else:
                books.append(book)
            if gap and i < len(futures) - 1:
                time.sleep(gap)
    stats = {
        "n_requested": len(markets),
        "n_fetched": len(books),
        "n_failed": failures,
        "mean_latency_s": round(sum(latencies) / len(latencies), 3) if latencies else 0.0,
        "max_latency_s": round(max(latencies), 3) if latencies else 0.0,
    }
    return books, stats


def poll_venue(
    venue_name: str,
    adapter: MarketDataAdapter,
    limit: int,
    *,
    max_workers: int = 4,
    min_volume_24h: float = 0.0,
) -> tuple[list[Market], list[OrderBook], dict[str, Any]]:
    """Fetch markets then books for one venue. Raises: VenueError."""
    t0 = time.perf_counter()
    markets = adapter.fetch_markets(limit=limit)
    n_listed = len(markets)
    markets = select_liquid(markets, min_volume_24h)
    books, stats = fetch_books(adapter, markets, max_workers=max_workers)
    stats["venue"] = venue_name
    stats["kalshi_env"] = getattr(adapter, "env", None)
    stats["poll_s"] = round(time.perf_counter() - t0, 2)
    stats["n_listed"] = n_listed
    stats["n_liquid"] = len(markets)
    return markets, books, stats


def snapshot_payload(
    venue: Venue, markets: list[Market], books: list[OrderBook], *, label: str = "live"
) -> dict[str, Any]:
    """Build a version-1 snapshot dict from fetched data.

    Raises:
        None.
    """
    return {
        "odds_snapshot_version": 1,
        "label": label,
        "venue": venue.value,
        "captured_at": datetime.now(UTC).isoformat(),
        "markets": [m.model_dump(mode="json") for m in markets],
        "order_books": [b.model_dump(mode="json") for b in books],
    }


def volume_24h_usd(market: Market) -> float:
    """Best-effort 24h volume in USD from the venue-native raw payload.

    Polymarket Gamma exposes ``volume24hr``; Kalshi exposes
    ``volume_24h_fp`` (a decimal string). Missing or unparseable
    values read as 0.0, never raise.

    Raises:
        None.
    """
    raw = market.raw or {}
    for key in ("volume24hr", "volume_24h_fp", "volume24h"):
        if key in raw:
            try:
                return float(raw[key])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def select_liquid(markets: list[Market], min_volume_24h: float) -> list[Market]:
    """Keep markets with 24h volume >= ``min_volume_24h``, liquid first.

    A non-positive threshold disables filtering. Illiquid books cannot
    produce executable opportunities, so scans and collectors use this
    to spend their request budget where quotes exist.

    Raises:
        None.
    """
    if min_volume_24h <= 0:
        return markets
    liquid = [m for m in markets if volume_24h_usd(m) >= min_volume_24h]
    liquid.sort(key=volume_24h_usd, reverse=True)
    return liquid
