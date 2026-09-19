"""Kalshi market-data adapter.

Uses only public endpoints (no credentials):
  GET {base}/markets?status=open                 market discovery (cursor pages)
  GET {base}/markets/{ticker}                    single market detail
  GET {base}/markets/{ticker}/orderbook          order book (both sides)

Verified response shapes (official OpenAPI spec, https://docs.kalshi.com/openapi.yaml,
GET /markets/{ticker}/orderbook):
  - The order book "returns yes bids and no bids only (no asks are returned)".
  - "a bid for yes at price X is equivalent to an ask for no at price
    (100-X) ... with identical contract sizes", because a yes order and a no
    order whose prices sum to $1 form a trade.
  - Price levels are "organized from best to worst prices" (bids: descending).
  /markets -> {"markets": [{"ticker", "event_ticker", "title",
                "yes_sub_title", "no_sub_title", "status" ("active"),
                "yes_bid", "yes_ask" (cents or null), "expiration_time", ...}],
               "cursor": str}
  /markets/{ticker}/orderbook -> {"orderbook_fp":
                {"yes_dollars": [[dollars, count_fp]], "no_dollars": [...]}}}
  (fp values are decimal strings, e.g. ["0.1500", "100.00"]).
  The legacy envelope {"orderbook":
                {"yes": [[cents, count]], "no": [...]}} is also accepted and
  carries the same bids-only semantics.

Notes:
  - Production (external-api.kalshi.com) returned HTTP 403 to datacenter IPs
    on 2026-09-18; the demo host works. Default env is "demo".
  - Fees are formula-based (see configs/fees.yaml), so resolve_taker_fee_rate
    returns None: the cost model computes fees per trade at execution price.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from backend.errors import AdapterParseError, DataValidationError
from backend.markets.base import MarketDataAdapter
from backend.markets.http import VenueError, get_json
from backend.schemas import (
    Market,
    MarketStatus,
    OrderBook,
    OrderBookLevel,
    Outcome,
    Venue,
    utcnow,
)

_OPEN_STATUSES = {"open", "active"}


def _complement_price(price_dollars: float) -> float:
    """Ask-side price implied by an opposite-side bid (binary complement).

    A bid for one side at price p is equivalent to an ask for the other side
    at (1 - p) -- official Kalshi OpenAPI semantics. Rounded to 4 decimals to
    match Kalshi's fixed-point dollar precision.
    """
    return round(1.0 - price_dollars, 4)


class KalshiAdapter(MarketDataAdapter):
    venue = Venue.KALSHI

    PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"
    DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"

    def __init__(self, env: str = "demo"):
        """Create an adapter for the demo or production Kalshi host.

        Raises:
            DataValidationError: if ``env`` is not "demo" or "prod".
        """
        if env not in ("demo", "prod"):
            raise DataValidationError("env must be 'demo' or 'prod'")
        self.env = env
        self.base_url = self.DEMO_BASE if env == "demo" else self.PROD_BASE

    # -- discovery ------------------------------------------------------
    def fetch_markets(self, *, status: str = "open", limit: int = 100) -> list[Market]:
        """Return normalized, currently tradable markets (cursor pages).

        Raises:
            VenueError: on venue communication failures, including the
                documented HTTP 403 from production datacenter egress.
        """
        markets: list[Market] = []
        cursor: str | None = None
        pages = 0
        while len(markets) < limit and pages < 10:
            params: dict[str, Any] = {"status": status, "limit": min(limit, 100)}
            if cursor:
                params["cursor"] = cursor
            try:
                data = get_json(f"{self.base_url}/markets", params)
            except VenueError as exc:
                if "403" in str(exc) and self.env == "prod":
                    raise VenueError(
                        "Kalshi production rejected this network (HTTP 403). "
                        "Use env='demo' or run from non-datacenter egress."
                    ) from exc
                raise
            if not isinstance(data, dict):
                break
            for raw in data.get("markets", []):
                markets.extend(self._markets_from_kalshi(raw))
                if len(markets) >= limit:
                    break
            cursor = data.get("cursor")
            pages += 1
            if not cursor:
                break
        return markets[:limit]

    def _markets_from_kalshi(self, raw: dict[str, Any]) -> list[Market]:
        """One Kalshi market -> two normalized Markets (YES and NO sides)."""
        if str(raw.get("status", "")).lower() not in _OPEN_STATUSES:
            return []
        return [
            self.normalize_market({**raw, "_outcome": Outcome.YES}),
            self.normalize_market({**raw, "_outcome": Outcome.NO}),
        ]

    # -- order books ----------------------------------------------------
    def fetch_order_book(self, market: Market) -> OrderBook:
        """Return the normalized live order book for one market outcome.

        Raises:
            VenueError: on venue communication failures.
            AdapterParseError: if the order-book envelope is unrecognized.
        """
        data = get_json(f"{self.base_url}/markets/{market.market_id}/orderbook")
        if not isinstance(data, dict):
            raise AdapterParseError(f"unexpected orderbook response for {market.market_id}")
        return self.normalize_order_book(data, market)

    # -- normalization --------------------------------------------------
    def normalize_market(self, raw: dict[str, Any]) -> Market:
        """Convert one venue-native market payload to the normalized schema.

        Raises:
            None.
        """
        outcome = raw.get("_outcome", Outcome.YES)
        if isinstance(outcome, str):
            outcome = Outcome[outcome.upper()]
        status_raw = str(raw.get("status", "")).lower()
        status = (
            MarketStatus.OPEN
            if status_raw in _OPEN_STATUSES
            else MarketStatus.CLOSED
            if status_raw in {"closed", "settled"}
            else MarketStatus.UNKNOWN
        )
        expiration = self._parse_dt(raw.get("expiration_time"))
        question = str(raw.get("title") or "")
        sub = raw.get("yes_sub_title" if outcome == Outcome.YES else "no_sub_title")
        if sub and sub != question:
            question = f"{question} [{sub}]"
        return Market(
            market_id=str(raw.get("ticker") or ""),
            venue=self.venue,
            event_id=str(raw.get("event_ticker") or ""),
            question=question,
            outcome=outcome,
            expiration=expiration,
            status=status,
            tick_size=0.01,  # Kalshi quotes integer cents
            taker_fee_rate=None,  # formula-based; computed per trade
            raw={k: v for k, v in raw.items() if not k.startswith("_")},
        )

    @staticmethod
    def _parse_dt(value: object) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def normalize_order_book(
        self, raw: dict[str, Any], market: Market, venue_ts: datetime | None = None
    ) -> OrderBook:
        """Convert one venue-native order-book payload to the normalized schema.

        Raises:
            AdapterParseError: if the order-book envelope is unrecognized.
        """
        yes_levels, no_levels = self._extract_ladders(raw)
        # Per the official Kalshi OpenAPI spec (GET /markets/{ticker}/orderbook):
        # the public envelope carries YES bids and NO bids ONLY -- there are no
        # ask levels to parse. Each side's ladder normalizes to BIDS.
        # The ask for a side is the documented complement of the OPPOSITE
        # side's bids: "a bid for yes at price X is equivalent to an ask for
        # no at price (100-X) ... with identical contract sizes", because a
        # yes order and a no order whose prices sum to $1 form a trade. So to
        # buy YES immediately you cross the resting NO bids at price (1 - p).
        if market.outcome == Outcome.YES:
            bid_levels, opposite_levels = yes_levels, no_levels
        else:
            bid_levels, opposite_levels = no_levels, yes_levels
        bids = sorted(
            (OrderBookLevel(price=p, size=s) for p, s in bid_levels),
            key=lambda level: level.price,
            reverse=True,  # best bid first (highest price)
        )
        asks = sorted(
            (OrderBookLevel(price=_complement_price(p), size=s) for p, s in opposite_levels),
            key=lambda level: level.price,  # best ask first (lowest price)
        )
        return OrderBook(
            market_id=market.market_id,
            venue=self.venue,
            outcome=market.outcome,
            bids=bids,
            asks=asks,
            venue_timestamp=venue_ts,
            received_timestamp=utcnow(),
        )

    @staticmethod
    def _extract_ladders(
        raw: dict[str, Any],
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Return (yes_levels, no_levels) as (dollars, size) tuples.

        Accepts the classic cents envelope and the floating-point dollars
        envelope observed on the demo host.
        """
        if "orderbook" in raw:
            book = raw["orderbook"] or {}
            yes = [(c / 100.0, float(n)) for c, n in (book.get("yes") or [])]
            no = [(c / 100.0, float(n)) for c, n in (book.get("no") or [])]
            return yes, no
        if "orderbook_fp" in raw:
            book = raw["orderbook_fp"] or {}
            yes = [(float(p), float(n)) for p, n in (book.get("yes_dollars") or [])]
            no = [(float(p), float(n)) for p, n in (book.get("no_dollars") or [])]
            return yes, no
        raise AdapterParseError(f"unrecognized Kalshi orderbook envelope: {sorted(raw)}")

    # -- fees -----------------------------------------------------------
    def resolve_taker_fee_rate(self, market: Market) -> float | None:
        """Kalshi fees are per-trade formula-based; no single rate exists.

        Raises:
            None.
        """
        # Kalshi fees are computed per trade from the official formula
        # (configs/fees.yaml); there is no single per-market rate to resolve.
        return None
