"""Edge-case hardening tests for arbitrage detection, cost modeling, and sizing.

ALL INPUTS SYNTHETIC: every order book, market, fee rate, and latency value
below is hand-constructed for testing. Nothing here is live market data, and
no result in this file may be presented as live trading performance.

Covers: empty books (both/one side), crossed books, single-sided books,
zero-size and duplicate price levels, fee boundaries, Kelly boundaries, and
invalid inputs the engine/schemas must reject.
"""
import math

import pytest
from pydantic import ValidationError

from backend.arbitrage.costs import (
    FeeModel,
    build_cost_breakdown,
    half_spread_cost,
    walk_book,
)
from backend.arbitrage.opportunities import (
    BookView,
    detect_all,
    detect_bundle_arbitrage,
    detect_cross_venue_complement,
    detect_cross_venue_direct,
    deterministic_match,
)
from backend.arbitrage.settings import StrategyConfig
from backend.arbitrage.sizing import kelly_optimal_fraction, size_position
from backend.schemas import Market, OrderBook, OrderBookLevel, Outcome, Venue
from tests.fixtures import (
    T0,
    SIM,
    kalshi_yes_no,
    make_book,
    make_market,
    polymarket_pair,
)


@pytest.fixture()
def config():
    return StrategyConfig.load()


@pytest.fixture()
def fees():
    return FeeModel()


def _two_venue_direct(buy_ask=0.50, sell_bid=0.60, depth=100.0):
    """Polymarket YES (buy) vs Kalshi YES (sell) matched pair, synthetic."""
    buy_m = make_market(venue=Venue.POLYMARKET, outcome=Outcome.YES,
                        market_id="pm-a", question="Will it rain tomorrow?")
    sell_m = make_market(venue=Venue.KALSHI, outcome=Outcome.YES,
                         market_id="kx-a", question="Will it rain tomorrow?",
                         taker_fee_rate=None)
    buy_book = make_book(buy_m, bids=[(buy_ask - 0.02, depth)],
                         asks=[(buy_ask, depth)], at=T0)
    sell_book = make_book(sell_m, bids=[(sell_bid, depth)],
                          asks=[(sell_bid + 0.02, depth)], at=T0)
    return (BookView(buy_m, buy_book, SIM),
            BookView(sell_m, sell_book, SIM))


# -- empty books ---------------------------------------------------------------

def test_bundle_both_books_empty_returns_none(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    yv = BookView(yv.market, make_book(yv.market, asks=[], at=T0), SIM)
    nv = BookView(nv.market, make_book(nv.market, asks=[], at=T0), SIM)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_bundle_yes_side_empty_returns_none(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    yv = BookView(yv.market, make_book(yv.market, asks=[], at=T0), SIM)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_cross_venue_direct_buy_side_empty_returns_none(config, fees):
    bv, sv = _two_venue_direct()
    bv = BookView(bv.market, make_book(bv.market, asks=[], at=T0), SIM)
    assert detect_cross_venue_direct(bv, sv, config=config, fee_model=fees,
                                     now=T0) is None


def test_cross_venue_direct_sell_side_empty_returns_none(config, fees):
    bv, sv = _two_venue_direct()
    sv = BookView(sv.market, make_book(sv.market, asks=[], bids=[], at=T0), SIM)
    assert detect_cross_venue_direct(bv, sv, config=config, fee_model=fees,
                                     now=T0) is None


def test_detect_all_with_only_empty_books_no_crash(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    yv = BookView(yv.market, make_book(yv.market, asks=[], at=T0), SIM)
    nv = BookView(nv.market, make_book(nv.market, asks=[], at=T0), SIM)
    opps, flags = detect_all([yv, nv], config=config, fee_model=fees, now=T0)
    assert opps == []
    assert flags == []


def test_detect_all_single_view_no_pairs(config, fees):
    yv, _ = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    opps, _ = detect_all([yv], config=config, fee_model=fees, now=T0)
    assert opps == []


# -- crossed books (bid > ask): schema-valid, must not crash or invent edge --

def test_crossed_book_no_edge_returns_none(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    # YES book crossed: bid 0.60 > ask 0.45, but asks sum > $1 -> no edge.
    crossed = make_book(yv.market, bids=[(0.60, 100.0)],
                        asks=[(0.45, 100.0)], at=T0)
    yv = BookView(yv.market, crossed, SIM)
    nv_crossed = make_book(nv.market, bids=[(0.60, 100.0)],
                           asks=[(0.60, 100.0)], at=T0)
    nv = BookView(nv.market, nv_crossed, SIM)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_crossed_book_edge_still_measured_at_ask(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.40, no_ask=0.45, depth=100.0)
    crossed = make_book(yv.market, bids=[(0.60, 100.0)],
                        asks=[(0.40, 100.0)], at=T0)
    yv = BookView(yv.market, crossed, SIM)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0)
    assert opp is not None
    assert opp.costs.raw_edge == pytest.approx(0.15)


def test_half_spread_crossed_book_is_negative_and_informational():
    m = make_market(outcome=Outcome.YES)
    book = make_book(m, bids=[(0.60, 10.0)], asks=[(0.55, 10.0)], at=T0)
    value, notes = half_spread_cost(book)
    # Informational only: documents the crossed-book value, never subtracted.
    assert value == pytest.approx(-0.025)
    assert notes == []


def test_cross_venue_direct_crossed_sell_book_no_crash(config, fees):
    bv, sv = _two_venue_direct(buy_ask=0.50, sell_bid=0.60)
    crossed_sell = make_book(sv.market, bids=[(0.60, 100.0)],
                             asks=[(0.55, 100.0)], at=T0)
    sv = BookView(sv.market, crossed_sell, SIM)
    opp = detect_cross_venue_direct(bv, sv, config=config, fee_model=fees,
                                    now=T0)
    assert opp is not None  # real edge at the touch, crossing is data noise


# -- single-sided books --------------------------------------------------------

def test_bundle_single_sided_books_detects(config, fees):
    # No bids anywhere: bundle only needs asks.
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    yv = BookView(yv.market, make_book(yv.market, asks=[(0.45, 100.0)],
                                       at=T0), SIM)
    nv = BookView(nv.market, make_book(nv.market, asks=[(0.45, 100.0)],
                                       at=T0), SIM)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0)
    assert opp is not None
    assert opp.costs.net_edge > config.detection.min_net_edge


def test_complement_single_sided_both_venues_detects(config, fees):
    # Asks-only books on both legs (synthetic engine edge case, not the
    # Kalshi public-book shape); complement needs no bids.
    yv, _ = kalshi_yes_no(yes_ask=0.45, depth=100.0)
    _, knv = kalshi_yes_no(no_ask=0.45, depth=100.0)
    pm_y = make_market(venue=Venue.POLYMARKET, outcome=Outcome.YES,
                       market_id="pm-y", event_id="evt-k1",
                       question="Will it rain tomorrow?")
    pm_yv = BookView(pm_y, make_book(pm_y, asks=[(0.45, 100.0)], at=T0), SIM)
    opp = detect_cross_venue_complement(pm_yv, knv, config=config,
                                        fee_model=fees, now=T0)
    assert opp is not None


# -- zero-size and duplicate price levels --------------------------------------

def test_walk_book_zero_size_levels_do_not_distort_vwap():
    yv, _ = polymarket_pair(yes_ask=0.45, depth=100.0)
    book = make_book(yv.market, asks=[(0.45, 0.0), (0.45, 50.0),
                                      (0.47, 100.0)], at=T0)
    res = walk_book(book, "buy", 60.0)
    assert res.filled == pytest.approx(60.0)
    assert res.shortfall == pytest.approx(0.0)
    assert res.vwap == pytest.approx((50 * 0.45 + 10 * 0.47) / 60)


def test_bundle_all_zero_size_levels_returns_none(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    zero = make_book(yv.market, asks=[(0.45, 0.0)], at=T0)
    yv = BookView(yv.market, zero, SIM)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_walk_book_duplicate_price_levels_aggregate():
    yv, _ = polymarket_pair(yes_ask=0.45, depth=100.0)
    book = make_book(yv.market, asks=[(0.45, 30.0), (0.45, 70.0)], at=T0)
    res = walk_book(book, "buy", 50.0)
    assert res.filled == pytest.approx(50.0)
    assert res.vwap == pytest.approx(0.45)
    assert res.levels_used == 2


def test_bundle_duplicate_price_levels_sizes_sum(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    dup = make_book(yv.market, asks=[(0.45, 30.0), (0.45, 70.0)], at=T0)
    yv = BookView(yv.market, dup, SIM)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0)
    assert opp is not None
    assert opp.legs[0]["quantity"] == pytest.approx(100.0)


def test_walk_book_nonpositive_size_fills_nothing():
    yv, _ = polymarket_pair(yes_ask=0.45, depth=100.0)
    for size in (0.0, -5.0):
        res = walk_book(yv.book, "buy", size)
        assert res.filled == 0.0
        assert res.shortfall == 0.0
        assert res.vwap is None


# -- fee boundaries ------------------------------------------------------------

def test_zero_fee_rate_still_signals(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0,
                             taker_fee_rate=0.0)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0)
    assert opp is not None
    assert opp.costs.trading_fees == pytest.approx(0.0)
    # Only latency remains: 0.8s * 0.002 drift.
    assert opp.costs.net_edge == pytest.approx(0.10 - 0.0016)


def test_fee_exactly_equal_to_edge_signals_no_trade(config, fees):
    # Per-contract fees == raw edge exactly; latency tips net below minimum.
    rate = 0.10 / 0.495  # 2 legs x rate x 0.45 x 0.55 == 0.10/contract
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0,
                             taker_fee_rate=rate)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_fee_exceeding_edge_signals_no_trade(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0,
                             taker_fee_rate=0.9)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_kalshi_round_up_erases_thin_edge(config, fees):
    # 1c raw edge; Kalshi rounds each leg's fee UP to the cent -> no trade.
    yv, nv = kalshi_yes_no(yes_ask=0.495, no_ask=0.495, depth=50.0)
    leg_fee = fees.taker_fee(yv.market, 50.0, 0.495).fee_dollars
    assert leg_fee == pytest.approx(0.88)  # ceil(87.49125c) -> $0.88
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_cost_breakdown_fee_equal_edge_net_nonpositive(config):
    size = 100.0
    cb = build_cost_breakdown(raw_edge_per_contract=0.10, size=size,
                              fees_total=0.10 * size, slippage_total=0.0,
                              spread_info_per_contract=0.0, latency_total=0.0)
    assert cb.net_edge == pytest.approx(0.0)
    assert cb.net_edge < config.detection.min_net_edge


def test_polymarket_dust_fee_boundaries(fees):
    m = make_market(taker_fee_rate=0.05)
    below = fees.taker_fee(m, 0.007, 0.5)   # 8.75e-05 < $0.0001 -> zeroed
    above = fees.taker_fee(m, 0.01, 0.5)    # 1.25e-04 >= $0.0001 -> kept
    assert below.fee_dollars == 0.0
    assert above.fee_dollars == pytest.approx(0.000125)


def test_cost_breakdown_size_zero_no_division_error():
    # Documents the per=1.0 convention: totals are NOT divided by zero.
    cb = build_cost_breakdown(raw_edge_per_contract=0.10, size=0.0,
                              fees_total=1.0, slippage_total=0.5,
                              spread_info_per_contract=0.0, latency_total=0.2)
    assert cb.net_edge == pytest.approx(0.10 - 1.0 - 0.5 - 0.2)


# -- Kelly / sizing boundaries -------------------------------------------------

def test_kelly_zero_odds_sizes_zero():
    assert kelly_optimal_fraction(0.55, 0.0) == 0.0
    assert kelly_optimal_fraction(0.99, -1.0) == 0.0


def test_kelly_exact_fair_odds_floors_at_zero():
    # b*p - q == 0 exactly: no edge, never bet.
    assert kelly_optimal_fraction(0.5, 1.0) == 0.0


def test_kelly_p_win_boundaries_raise():
    for p in (0.0, 1.0, -0.1, 1.1):
        with pytest.raises(ValueError):
            kelly_optimal_fraction(p, 1.0)


def test_size_position_zero_net_edge_kelly_zero():
    r = size_position(net_edge_per_contract=0.0, cost_per_contract=0.9,
                      liquidity=500.0, bankroll=100.0, fraction=0.5,
                      max_position=500.0, p_win=0.99,
                      p_win_is_placeholder=False)
    assert r.quantity == 0.0
    assert r.capped_by == "kelly_zero"


def test_size_position_enormous_edge_capped_by_liquidity():
    r = size_position(net_edge_per_contract=1.0, cost_per_contract=0.01,
                      liquidity=100.0, bankroll=100000.0, fraction=1.0,
                      max_position=500.0, p_win=0.99,
                      p_win_is_placeholder=False)
    assert r.quantity == pytest.approx(100.0)
    assert r.capped_by == "liquidity"


def test_size_position_enormous_edge_capped_by_both():
    r = size_position(net_edge_per_contract=1.0, cost_per_contract=0.01,
                      liquidity=1000.0, bankroll=100000.0, fraction=1.0,
                      max_position=500.0, p_win=0.99,
                      p_win_is_placeholder=False)
    assert r.quantity == pytest.approx(500.0)
    assert r.capped_by == "liquidity+max_position"


def test_size_position_zero_bankroll_sizes_zero():
    r = size_position(net_edge_per_contract=0.08, cost_per_contract=0.9,
                      liquidity=500.0, bankroll=0.0, fraction=0.5,
                      max_position=500.0, p_win=0.99,
                      p_win_is_placeholder=False)
    assert r.quantity == 0.0
    assert r.capped_by == "no_bankroll_or_cost"


def test_size_position_zero_cost_sizes_zero():
    r = size_position(net_edge_per_contract=0.08, cost_per_contract=0.0,
                      liquidity=500.0, bankroll=100.0, fraction=0.5,
                      max_position=500.0, p_win=0.99,
                      p_win_is_placeholder=False)
    assert r.quantity == 0.0
    assert r.capped_by == "no_bankroll_or_cost"


def test_size_position_fraction_zero_rejected():
    with pytest.raises(ValueError):
        size_position(net_edge_per_contract=0.08, cost_per_contract=0.9,
                      liquidity=500.0, bankroll=100.0, fraction=0.0,
                      max_position=500.0, p_win=0.99)


# -- size_cap behavior -----------------------------------------------------------

def test_size_cap_below_min_liquidity_returns_none(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0, size_cap=10.0) is None


def test_size_cap_limits_quantity(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                  now=T0, size_cap=60.0)
    assert opp is not None
    assert opp.legs[0]["quantity"] == pytest.approx(60.0)


def test_size_cap_zero_means_no_cap_not_zero_size(config, fees):
    # Documents the falsy-cap convention: size_cap=0 -> full depth, not 0.
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                  now=T0, size_cap=0.0)
    assert opp is not None
    assert opp.legs[0]["quantity"] == pytest.approx(100.0)


# -- invalid inputs the engine/schemas must reject -----------------------------

@pytest.mark.parametrize("price", [-0.1, -1.0, 1.01, 2.0])
def test_invalid_prices_rejected(price):
    with pytest.raises(ValidationError):
        OrderBookLevel(price=price, size=10.0)


def test_nan_price_rejected():
    with pytest.raises(ValidationError):
        OrderBookLevel(price=math.nan, size=10.0)


@pytest.mark.parametrize("size", [-0.1, -100.0])
def test_negative_sizes_rejected(size):
    with pytest.raises(ValidationError):
        OrderBookLevel(price=0.5, size=size)


def test_unsorted_asks_rejected():
    m = make_market(outcome=Outcome.YES)
    with pytest.raises(ValidationError):
        OrderBook(market_id=m.market_id, venue=m.venue, outcome=m.outcome,
                  bids=[],
                  asks=[OrderBookLevel(price=0.5, size=10.0),
                        OrderBookLevel(price=0.4, size=10.0)])


def test_unsorted_bids_rejected():
    m = make_market(outcome=Outcome.YES)
    with pytest.raises(ValidationError):
        OrderBook(market_id=m.market_id, venue=m.venue, outcome=m.outcome,
                  bids=[OrderBookLevel(price=0.4, size=10.0),
                        OrderBookLevel(price=0.5, size=10.0)],
                  asks=[])


def test_walk_book_invalid_side_raises():
    yv, _ = polymarket_pair()
    with pytest.raises(ValueError):
        walk_book(yv.book, "sideways", 10.0)


def test_deterministic_match_empty_question_rejected():
    a = make_market(venue=Venue.POLYMARKET, market_id="a", question="!!!")
    b = make_market(venue=Venue.KALSHI, market_id="b", question="???")
    assert not deterministic_match(a, b).matched


def test_bundle_exactly_zero_raw_edge_returns_none(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.55, no_ask=0.45, depth=100.0)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_cross_venue_direct_equal_bid_ask_returns_none(config, fees):
    bv, sv = _two_venue_direct(buy_ask=0.50, sell_bid=0.50)
    assert detect_cross_venue_direct(bv, sv, config=config, fee_model=fees,
                                     now=T0) is None


# -- non-finite sizes are rejected by the schema ----------------------------------

def test_infinite_size_levels_rejected_by_schema():
    """Regression (fixed 2026-09-18): OrderBookLevel must reject non-finite
    sizes. Previously an infinite ask size passed validation and the bundle
    detector emitted an opportunity with net_edge=nan and quantity=inf,
    defeating the min-edge gate (nan < x is False)."""
    with pytest.raises(ValidationError):
        OrderBookLevel(price=0.45, size=math.inf)
    with pytest.raises(ValidationError):
        OrderBookLevel(price=0.45, size=math.nan)
    with pytest.raises(ValidationError):
        OrderBookLevel(price=math.inf, size=100.0)
