"""Labeled fixtures for engine tests.

Every fixture book/market is explicitly labeled "simulated" unless a test
needs the "live" label path. Builders keep books schema-valid (asks
ascending, bids descending).
"""
from datetime import datetime, timedelta, timezone

from backend.schemas import (
    Market,
    MarketStatus,
    OrderBook,
    OrderBookLevel,
    Outcome,
    Venue,
)

T0 = datetime(2026, 9, 18, 12, 0, 0, tzinfo=timezone.utc)
SIM = "simulated"
LIVE = "live"


def make_market(*, venue=Venue.POLYMARKET, outcome=Outcome.YES,
                market_id="pm-yes-1", event_id="evt-1",
                question="Will it rain tomorrow?",
                taker_fee_rate=0.05, expiration=None,
                status=MarketStatus.OPEN) -> Market:
    return Market(
        market_id=market_id, venue=venue, event_id=event_id,
        question=question, outcome=outcome, expiration=expiration,
        status=status, tick_size=0.01, taker_fee_rate=taker_fee_rate, raw={})


def make_book(market: Market, *, bids=None, asks=None,
              at: datetime | None = None) -> OrderBook:
    at = at or T0
    bid_lvls = [OrderBookLevel(price=p, size=s) for p, s in (bids or [])]
    ask_lvls = [OrderBookLevel(price=p, size=s) for p, s in (asks or [])]
    bid_lvls.sort(key=lambda l: l.price, reverse=True)
    ask_lvls.sort(key=lambda l: l.price)
    return OrderBook(
        market_id=market.market_id, venue=market.venue, outcome=market.outcome,
        bids=bid_lvls, asks=ask_lvls,
        venue_timestamp=at, received_timestamp=at)


def polymarket_pair(*, yes_ask=0.45, no_ask=0.45, depth=100.0,
                    at: datetime | None = None, event_id="evt-1",
                    taker_fee_rate=0.05):
    """YES/NO BookViews with a touch-level bundle edge at these asks."""
    from backend.arbitrage.opportunities import BookView
    ym = make_market(venue=Venue.POLYMARKET, outcome=Outcome.YES,
                     market_id="pm-yes-1", event_id=event_id,
                     taker_fee_rate=taker_fee_rate)
    nm = make_market(venue=Venue.POLYMARKET, outcome=Outcome.NO,
                     market_id="pm-no-1", event_id=event_id,
                     taker_fee_rate=taker_fee_rate)
    yb = make_book(ym, bids=[(yes_ask - 0.02, depth)], asks=[(yes_ask, depth)], at=at)
    nb = make_book(nm, bids=[(no_ask - 0.02, depth)], asks=[(no_ask, depth)], at=at)
    return BookView(ym, yb, SIM), BookView(nm, nb, SIM)


def kalshi_yes_no(*, yes_ask=0.45, no_ask=0.45, depth=100.0,
                  at: datetime | None = None, event_id="evt-k1"):
    """Kalshi-style synthetic pair: asks only, bids empty.

    Engine-level shape for testing, NOT the public book: since the
    2026-09-18 adapter fix, Kalshi ladders normalize to bids and asks
    are the documented 1-bid complement.
    """
    from backend.arbitrage.opportunities import BookView
    ym = make_market(venue=Venue.KALSHI, outcome=Outcome.YES,
                     market_id="KX-YES", event_id=event_id,
                     question="Will it rain tomorrow?",
                     taker_fee_rate=None)
    nm = make_market(venue=Venue.KALSHI, outcome=Outcome.NO,
                     market_id="KX-NO", event_id=event_id,
                     question="Will it rain tomorrow?",
                     taker_fee_rate=None)
    yb = make_book(ym, asks=[(yes_ask, depth)], at=at)
    nb = make_book(nm, asks=[(no_ask, depth)], at=at)
    return BookView(ym, yb, SIM), BookView(nm, nb, SIM)
