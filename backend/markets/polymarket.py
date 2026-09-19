"""Polymarket market-data adapter.

Uses only public endpoints (no credentials):
  Gamma  https://gamma-api.polymarket.com/markets   market discovery
  CLOB   https://clob.polymarket.com/book           order books
  CLOB   https://clob.polymarket.com/fee-rate        per-token taker fee
  CLOB   https://clob.polymarket.com/tick-size       minimum price increment

Verified response shapes (2026-09-18, see docs/api-research.md):
  /book -> {"market", "asset_id", "timestamp" (ms, str), "hash",
            "bids": [{"price": str, "size": str}],   # worst-first
            "asks": [{"price": str, "size": str}]}   # worst-first
  /fee-rate -> {"base_fee": int}      # e.g. 0, 1000 — basis points
  /tick-size -> {"minimum_tick_size": float}
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from backend.markets.base import MarketDataAdapter
from backend.markets.http import get_json
from backend.schemas import (
    Market,
    MarketStatus,
    OrderBook,
    OrderBookLevel,
    Outcome,
    Venue,
    utcnow,
)

GAMMA_BASE = "https://gamma-api.polymarket.com"
CLOB_BASE = "https://clob.polymarket.com"

_FEE_CACHE_TTL_SECONDS = 6 * 3600


def _parse_json_array(value: str | list) -> list:
    if isinstance(value, list):
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return []


class PolymarketAdapter(MarketDataAdapter):
    venue = Venue.POLYMARKET

    def __init__(self) -> None:
        # token_id -> (resolved_at_epoch, taker_fee_rate)
        self._fee_cache: dict[str, tuple[float, float]] = {}

    # -- discovery ------------------------------------------------------
    def fetch_markets(self, *, status: str = "open", limit: int = 100) -> list[Market]:
        params: dict = {"limit": min(limit, 500)}
        if status == "open":
            params.update({"active": "true", "closed": "false"})
        markets: list[Market] = []
        offset = 0
        pages = 0
        while len(markets) < limit and pages < 10:
            page = get_json(f"{GAMMA_BASE}/markets",
                            {**params, "offset": offset})
            if not isinstance(page, list) or not page:
                break
            for raw in page:
                markets.extend(self._markets_from_gamma(raw))
                if len(markets) >= limit:
                    break
            offset += len(page)
            pages += 1
        return markets[:limit]

    def _markets_from_gamma(self, raw: dict) -> list[Market]:
        """One Gamma market -> two normalized Markets (YES and NO tokens)."""
        tokens = _parse_json_array(raw.get("clobTokenIds", ""))
        outcomes = _parse_json_array(raw.get("outcomes", ""))
        if len(tokens) < 2:
            return []
        labels = outcomes[:2] if len(outcomes) >= 2 else ["Yes", "No"]
        return [self.normalize_market({**raw, "_token_id": tok, "_outcome": out})
                for tok, out in zip(tokens[:2], labels)]

    # -- order books ----------------------------------------------------
    def fetch_order_book(self, market: Market) -> OrderBook:
        raw = get_json(f"{CLOB_BASE}/book", {"token_id": market.market_id})
        if not isinstance(raw, dict):
            raise ValueError(f"unexpected /book response for {market.market_id}")
        venue_ts = self._parse_venue_ts(raw.get("timestamp"))
        return self.normalize_order_book(raw, market, venue_ts)

    @staticmethod
    def _parse_venue_ts(value: object) -> datetime | None:
        try:
            ms = int(str(value))
        except (ValueError, TypeError):
            return None
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)

    # -- normalization --------------------------------------------------
    def normalize_market(self, raw: dict) -> Market:
        token_id = str(raw.get("_token_id") or "")
        outcome_raw = str(raw.get("_outcome") or "Yes").strip().upper()
        outcome = Outcome.YES if outcome_raw.startswith("Y") else Outcome.NO
        taker_bps = raw.get("takerBaseFee")
        expiration = self._parse_dt(raw.get("endDate"))
        if raw.get("active") and not raw.get("closed"):
            status = MarketStatus.OPEN
        elif raw.get("closed"):
            status = MarketStatus.CLOSED
        else:
            status = MarketStatus.UNKNOWN
        return Market(
            market_id=token_id,
            venue=self.venue,
            event_id=str(raw.get("conditionId") or raw.get("id") or ""),
            question=str(raw.get("question") or ""),
            outcome=outcome,
            expiration=expiration,
            status=status,
            tick_size=float(raw.get("orderPriceMinTickSize") or 0.01),
            taker_fee_rate=(float(taker_bps) / 10000.0) if taker_bps is not None else None,
            raw={k: v for k, v in raw.items() if not k.startswith("_")},
        )

    @staticmethod
    def _parse_dt(value: object) -> datetime | None:
        if not value:
            return None
        try:
            text = str(value).replace("Z", "+00:00")
            return datetime.fromisoformat(text)
        except ValueError:
            return None

    def normalize_order_book(self, raw: dict, market: Market,
                             venue_ts: datetime | None = None) -> OrderBook:
        # CLOB returns levels worst-first; schema requires best-first.
        bids = sorted(
            (OrderBookLevel(price=float(lvl["price"]), size=float(lvl["size"]))
             for lvl in raw.get("bids", [])),
            key=lambda l: l.price, reverse=True,
        )
        asks = sorted(
            (OrderBookLevel(price=float(lvl["price"]), size=float(lvl["size"]))
             for lvl in raw.get("asks", [])),
            key=lambda l: l.price,
        )
        return OrderBook(
            market_id=market.market_id,
            venue=self.venue,
            outcome=market.outcome,
            bids=bids,
            asks=asks,
            venue_timestamp=venue_ts or self._parse_venue_ts(raw.get("timestamp")),
            received_timestamp=utcnow(),
        )

    # -- fees -----------------------------------------------------------
    def resolve_taker_fee_rate(self, market: Market) -> float | None:
        """Resolve the live taker fee rate per token via GET /fee-rate.

        The official docs warn against hardcoding rates; this caches the
        resolved rate for 6h and falls back to the Gamma-provided rate.
        The official CLOB OpenAPI spec defines ``FeeRate.base_fee`` as
        "Base fee in basis points" (verified 2026-09-18:
        https://docs.polymarket.com/api-spec/clob-openapi.yaml), so
        ``base_fee / 10000`` is the decimal taker fee rate. The rate is per
        token and applies at match time to taker fills only; makers pay 0
        (https://docs.polymarket.com/trading/fees).
        """
        now = time.time()
        cached = self._fee_cache.get(market.market_id)
        if cached and now - cached[0] < _FEE_CACHE_TTL_SECONDS:
            return cached[1]
        try:
            raw = get_json(f"{CLOB_BASE}/fee-rate",
                           {"token_id": market.market_id})
            rate = float(raw["base_fee"]) / 10000.0
        except Exception:
            rate = market.taker_fee_rate
        if rate is not None:
            self._fee_cache[market.market_id] = (now, rate)
        return rate
