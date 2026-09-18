"""Tests for the paper execution simulator."""
from datetime import timedelta

import pytest

from backend.arbitrage.costs import FeeModel
from backend.arbitrage.opportunities import detect_bundle_arbitrage
from backend.arbitrage.settings import StrategyConfig
from backend.execution.paper import PaperBroker, latest_book_at
from backend.schemas import PaperTradeStatus
from tests.fixtures import T0, make_book, make_market, polymarket_pair


@pytest.fixture()
def config():
    return StrategyConfig.load()


@pytest.fixture()
def broker(config):
    return PaperBroker(FeeModel(), config)


def _setup(yes_ask=0.45, no_ask=0.45, depth=200.0, at=T0, expiration=None):
    """Return (opportunity, timelines, markets) for a bundle arb."""
    yv, nv = polymarket_pair(yes_ask=yes_ask, no_ask=no_ask, depth=depth, at=at)
    if expiration is not None:
        for v in (yv, nv):
            v.market.expiration = expiration
    config = StrategyConfig.load()
    opp = detect_bundle_arbitrage(yv, nv, config=config,
                                  fee_model=FeeModel(), now=at)
    assert opp is not None
    timelines = {
        (yv.market.venue.value, yv.market.market_id, "YES"): [yv.book],
        (nv.market.venue.value, nv.market.market_id, "NO"): [nv.book],
    }
    markets = {
        (yv.market.venue.value, yv.market.market_id, "YES"): yv.market,
        (nv.market.venue.value, nv.market.market_id, "NO"): nv.market,
    }
    return opp, timelines, markets


def test_filled_when_depth_sufficient(broker):
    opp, timelines, markets = _setup(depth=200.0)
    trades = broker.execute(opp, 50.0, timelines, markets, decide_at=T0)
    assert len(trades) == 2
    assert all(t.status == PaperTradeStatus.FILLED for t in trades)
    assert all(t.filled_quantity == 50.0 for t in trades)
    assert trades[0].simulated_fill_price == pytest.approx(0.45)
    # Buy legs are cash outflows.
    assert all(t.net_pnl < 0 for t in trades)


def test_partially_filled_when_depth_short(broker):
    opp, timelines, markets = _setup(depth=200.0)
    trades = broker.execute(opp, 500.0, timelines, markets, decide_at=T0)
    assert all(t.status == PaperTradeStatus.PARTIALLY_FILLED for t in trades)
    assert all(t.filled_quantity == 200.0 for t in trades)


def test_missed_when_book_stale(broker):
    old = T0 - timedelta(seconds=30)
    opp, timelines, markets = _setup(depth=200.0, at=old)
    trades = broker.execute(opp, 50.0, timelines, markets, decide_at=T0)
    assert all(t.status == PaperTradeStatus.MISSED for t in trades)


def test_missed_when_book_empty(broker):
    opp, timelines, markets = _setup(depth=200.0)
    for key in timelines:
        m = markets[key]
        timelines[key] = [make_book(m, asks=[], at=T0)]
    trades = broker.execute(opp, 50.0, timelines, markets, decide_at=T0)
    assert all(t.status == PaperTradeStatus.MISSED for t in trades)


def test_expired_when_market_expired(broker):
    opp, timelines, markets = _setup(
        depth=200.0, expiration=T0 + timedelta(milliseconds=100))
    trades = broker.execute(opp, 50.0, timelines, markets, decide_at=T0)
    assert all(t.status == PaperTradeStatus.EXPIRED for t in trades)


def test_expired_when_no_quote_yet(broker):
    opp, timelines, markets = _setup(depth=200.0)
    future = T0 + timedelta(seconds=60)
    for key, m in markets.items():
        timelines[key] = [make_book(m, asks=[(0.45, 200.0)], at=future)]
    trades = broker.execute(opp, 50.0, timelines, markets, decide_at=T0)
    assert all(t.status == PaperTradeStatus.EXPIRED for t in trades)


def test_no_look_ahead_uses_only_past_books(broker):
    opp, timelines, markets = _setup(depth=200.0)
    key = next(iter(timelines))
    m = markets[key]
    old_book = make_book(m, asks=[(0.45, 200.0)], at=T0)
    # A better price appears AFTER the fill moment: must not be used.
    new_book = make_book(m, asks=[(0.40, 200.0)], at=T0 + timedelta(seconds=10))
    timelines[key] = [old_book, new_book]
    trades = broker.execute(opp, 50.0, timelines, markets, decide_at=T0)
    first = next(t for t in trades if t.market_id == m.market_id)
    assert first.simulated_fill_price == pytest.approx(0.45)


def test_latest_book_at_picks_newest_not_future():
    m = make_market()
    b1 = make_book(m, asks=[(0.45, 10.0)], at=T0)
    b2 = make_book(m, asks=[(0.46, 10.0)], at=T0 + timedelta(seconds=1))
    b3 = make_book(m, asks=[(0.47, 10.0)], at=T0 + timedelta(seconds=99))
    assert latest_book_at([b1, b2, b3], T0 + timedelta(seconds=2)) is b2
    assert latest_book_at([b3], T0) is None
