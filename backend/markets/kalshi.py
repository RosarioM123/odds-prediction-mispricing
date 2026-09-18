"""Kalshi market-data adapter.

Uses only public endpoints (no credentials):
  GET {base}/markets?status=open                 market discovery (cursor pages)
  GET {base}/markets/{ticker}                    single market detail
  GET {base}/markets/{ticker}/orderbook          order book (both sides)

Verified response shapes (2026-09-18, demo environment):
  /markets -> {"markets": [{"ticker", "event_ticker", "title",
                "yes_sub_title", "no_sub_title", "status" ("active"),
                "yes_bid", "yes_ask" (cents or null), "expiration_time", ...}],
               "cursor": str}
  /markets/{ticker}/orderbook -> {"orderbook_fp":
                {"yes_dollars": [[dollars, count]], "no_dollars": [...]}}}
  The documented classic envelope {"orderbook":
                {"yes": [[cents, count]], "no": [...]}} is also accepted.

Notes:
  - Production (external-api.kalshi.com) returned HTTP 403 to datacenter IPs
    on 2026-09-18; the demo host works. Default env is "demo".
  - Fees are formula-based (see configs/fees.yaml), so resolve_taker_fee_rate
    returns None: the cost model computes fees per trade at execution price.
"""
from __future__ import annotations

from datetime import datetime

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


class KalshiAdapter(MarketDataAdapter):
    venue = Venue.KALSHI

    PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"
    DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"

    def __init__(self, env: str = "demo"):
        if env not in ("demo", "prod"):
            raise ValueError("env must be 'demo' or 'prod'")
        self.env = env
        self.base_url = self.DEMO_BASE if env == "demo" else self.PROD_BASE

    # -- discovery ------------------------------------------------------
    def fetch_markets(self, *, status: str = "open", limit: int = 100) -> list[Market]:
        markets: list[Market] = []
        cursor: str | None = None
        pages = 0
        while len(markets) < limit and pages < 10:
            params: dict = {"status": status, "limit": min(limit, 100)}
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

    def _markets_from_kalshi(self, raw: dict) -> list[Market]:
        """One Kalshi market -> two normalized Markets (YES and NO sides)."""
        if str(raw.get("status", "")).lower() not in _OPEN_STATUSES:
            return []
        return [self.normalize_market({**raw, "_outcome": Outcome.YES}),
                self.normalize_market({**raw, "_outcome": Outcome.NO})]

    # -- order books ----------------------------------------------------
    def fetch_order_book(self, market: Market) -> OrderBook:
        data = get_json(f"{self.base_url}/markets/{market.market_id}/orderbook")
        if not isinstance(data, dict):
            raise ValueError(f"unexpected orderbook response for {market.market_id}")
        return self.normalize_order_book(data, market)

    # -- normalization --------------------------------------------------
    def normalize_market(self, raw: dict) -> Market:
        outcome = raw.get("_outcome", Outcome.YES)
        if isinstance(outcome, str):
            outcome = Outcome[outcome.upper()]
        status_raw = str(raw.get("status", "")).lower()
        status = (MarketStatus.OPEN if status_raw in _OPEN_STATUSES
                  else MarketStatus.CLOSED if status_raw in {"closed", "settled"}
                  else MarketStatus.UNKNOWN)
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

    def normalize_order_book(self, raw: dict, market: Market,
                             venue_ts: datetime | None = None) -> OrderBook:
        yes_levels, no_levels = self._extract_ladders(raw)
        levels = yes_levels if market.outcome == Outcome.YES else no_levels
        # Each side's ladder holds resting orders at which WE can buy that
        # outcome, so it normalizes to the ASK side.
        #
        # LIMITATION (documented, not silently worked around): the public
        # orderbook envelope does not separate bids from asks, so bids are
        # left empty. Spread and mid-price are therefore unavailable for
        # Kalshi until bid semantics are verified against the official docs.
        # Bundle arbitrage only needs YES ask + NO ask, which this covers.
        asks = sorted(
            (OrderBookLevel(price=p, size=s) for p, s in levels),
            key=lambda l: l.price,
        )
        return OrderBook(
            market_id=market.market_id,
            venue=self.venue,
            outcome=market.outcome,
            bids=[],
            asks=asks,
            venue_timestamp=venue_ts,
            received_timestamp=utcnow(),
        )

    @staticmethod
    def _extract_ladders(raw: dict) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
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
        raise ValueError(f"unrecognized Kalshi orderbook envelope: {sorted(raw)}")

    # -- fees -----------------------------------------------------------
    def resolve_taker_fee_rate(self, market: Market) -> float | None:
        # Kalshi fees are computed per trade from the official formula
        # (configs/fees.yaml); there is no single per-market rate to resolve.
        return None
