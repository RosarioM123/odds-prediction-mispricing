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
from datetime import UTC, datetime
from typing import Any

from backend.errors import AdapterParseError, VenueError
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


def _parse_json_array(value: str | list[Any]) -> list[Any]:
    """Parse a JSON array that may arrive as a string or a native list.

    Raises:
        None. Unparseable input yields ``[]``.
    """
    if isinstance(value, list):
        return value
    try:
        # json.loads is untyped; the result is genuinely Any.
        parsed: Any = json.loads(value)
    except (ValueError, TypeError):
        return []
    return parsed if isinstance(parsed, list) else []


class PolymarketAdapter(MarketDataAdapter):
    venue = Venue.POLYMARKET

    def __init__(self) -> None:
        """Create an adapter with an empty per-token fee cache.

        Raises:
            None.
        """
        # token_id -> (resolved_at_epoch, taker_fee_rate)
        self._fee_cache: dict[str, tuple[float, float]] = {}

    # -- discovery ------------------------------------------------------
    def fetch_markets(self, *, status: str = "open", limit: int = 100) -> list[Market]:
        """Return normalized, currently tradable markets from Gamma.

        Raises:
            VenueError: on venue communication failures.
        """
        params: dict[str, Any] = {"limit": min(limit, 500)}
        if status == "open":
            params.update({"active": "true", "closed": "false"})
        markets: list[Market] = []
        offset = 0
        pages = 0
        while len(markets) < limit and pages < 10:
            page = get_json(f"{GAMMA_BASE}/markets", {**params, "offset": offset})
            if not isinstance(page, list) or not page:
                break
            for raw in page:
                markets.extend(self._markets_from_gamma(raw))
                if len(markets) >= limit:
                    break
            offset += len(page)
            pages += 1
        return markets[:limit]

    def _markets_from_gamma(self, raw: dict[str, Any]) -> list[Market]:
        """One Gamma market -> two normalized Markets (YES and NO tokens)."""
        tokens = _parse_json_array(raw.get("clobTokenIds", ""))
        outcomes = _parse_json_array(raw.get("outcomes", ""))
        if len(tokens) < 2:
            return []
        labels = outcomes[:2] if len(outcomes) >= 2 else ["Yes", "No"]
        return [
            self.normalize_market({**raw, "_token_id": tok, "_outcome": out})
            for tok, out in zip(tokens[:2], labels, strict=False)
        ]

    # -- order books ----------------------------------------------------
    def fetch_order_book(self, market: Market) -> OrderBook:
        """Return the normalized live order book for one market outcome.

        Raises:
            VenueError: on venue communication failures.
            AdapterParseError: if ``/book`` returns a non-dict payload.
        """
        raw = get_json(f"{CLOB_BASE}/book", {"token_id": market.market_id})
        if not isinstance(raw, dict):
            raise AdapterParseError(f"unexpected /book response for {market.market_id}")
        venue_ts = self._parse_venue_ts(raw.get("timestamp"))
        return self.normalize_order_book(raw, market, venue_ts)

    @staticmethod
    def _parse_venue_ts(value: object) -> datetime | None:
        try:
            ms = int(str(value))
        except (ValueError, TypeError):
            return None
        return datetime.fromtimestamp(ms / 1000, tz=UTC)

    # -- normalization --------------------------------------------------
    def normalize_market(self, raw: dict[str, Any]) -> Market:
        """Convert one Gamma market payload to the normalized schema.

        Raises:
            None.
        """
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

    def normalize_order_book(
        self, raw: dict[str, Any], market: Market, venue_ts: datetime | None = None
    ) -> OrderBook:
        """Convert one CLOB ``/book`` payload to the normalized schema.

        Raises:
            None.
        """
        # CLOB returns levels worst-first; schema requires best-first.
        bids = sorted(
            (
                OrderBookLevel(price=float(lvl["price"]), size=float(lvl["size"]))
                for lvl in raw.get("bids", [])
            ),
            key=lambda level: level.price,
            reverse=True,
        )
        asks = sorted(
            (
                OrderBookLevel(price=float(lvl["price"]), size=float(lvl["size"]))
                for lvl in raw.get("asks", [])
            ),
            key=lambda level: level.price,
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

        Raises:
            None. Every failure mode degrades to the Gamma-provided rate;
            fee resolution never raises.
        """
        now = time.time()
        cached = self._fee_cache.get(market.market_id)
        if cached and now - cached[0] < _FEE_CACHE_TTL_SECONDS:
            return cached[1]
        rate: float | None
        try:
            raw = get_json(f"{CLOB_BASE}/fee-rate", {"token_id": market.market_id})
            if not isinstance(raw, dict):
                raise AdapterParseError(f"unexpected /fee-rate response for {market.market_id}")
            rate = float(raw["base_fee"]) / 10000.0
        except (
            VenueError,
            AdapterParseError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            RuntimeError,
        ):
            # Deliberately broad: ANY fee-endpoint failure -- throttles,
            # transport errors, malformed payloads, even unexpected client
            # bugs -- must degrade to the Gamma rate, never fail detection.
            rate = market.taker_fee_rate
        if rate is not None:
            self._fee_cache[market.market_id] = (now, rate)
        return rate
