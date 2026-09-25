"""Tests for the transaction-cost model."""

import pytest

from backend.arbitrage.costs import (
    FEE_RATE_FALLBACK,
    LATENCY_DRIFT_PLACEHOLDER,
    POLYMARKET_FEE_PROVISIONAL,
    SPREAD_UNAVAILABLE,
    FeeModel,
    build_cost_breakdown,
    half_spread_cost,
    latency_adjustment,
    walk_book,
)
from backend.errors import DataValidationError
from tests.fixtures import kalshi_yes_no, make_book, make_market, polymarket_pair


@pytest.fixture()
def fees():
    return FeeModel()


def test_walk_book_buy_walks_asks_best_first(fees):
    yv, _ = polymarket_pair(yes_ask=0.45, depth=100.0)
    # Replace asks with a two-level ladder.
    book = make_book(yv.market, asks=[(0.45, 30.0), (0.47, 100.0)])
    res = walk_book(book, "buy", 50.0)
    assert res.filled == 50.0
    assert res.shortfall == 0.0
    assert res.vwap == pytest.approx((30 * 0.45 + 20 * 0.47) / 50)
    assert res.levels_used == 2


def test_walk_book_partial_fill_shortfall():
    yv, _ = polymarket_pair(yes_ask=0.45, depth=40.0)
    res = walk_book(yv.book, "buy", 100.0)
    assert res.filled == 40.0
    assert res.shortfall == 60.0
    assert res.vwap == pytest.approx(0.45)


def test_walk_book_empty_side():
    yv, _ = kalshi_yes_no()  # bids empty
    res = walk_book(yv.book, "sell", 10.0)
    assert res.filled == 0.0 and res.vwap is None


def test_walk_book_bad_side_raises():
    yv, _ = polymarket_pair()
    with pytest.raises(ValueError):
        walk_book(yv.book, "hold", 10.0)


def test_polymarket_taker_fee_formula(fees):
    yv, _ = polymarket_pair(taker_fee_rate=0.05)
    q = fees.taker_fee(yv.market, contracts=100.0, price=0.45)
    # C * rate * (p*(1-p)) = 100 * 0.05 * 0.2475
    assert q.fee_dollars == pytest.approx(1.2375)
    assert POLYMARKET_FEE_PROVISIONAL in q.notes


def test_polymarket_dust_fee_rounds_to_zero(fees):
    yv, _ = polymarket_pair(taker_fee_rate=0.05)
    q = fees.taker_fee(yv.market, contracts=0.001, price=0.5)
    assert q.fee_dollars == 0.0


def test_polymarket_fee_fallback_labeled(fees):
    m = make_market(taker_fee_rate=None)  # Polymarket, rate unknown
    rate, notes = fees.resolve_taker_rate(m)
    assert rate == pytest.approx(0.05)
    assert FEE_RATE_FALLBACK in notes


def test_kalshi_taker_fee_rounds_up_to_cent(fees):
    yv, _ = kalshi_yes_no()
    q = fees.taker_fee(yv.market, contracts=50.0, price=0.45)
    # 0.07 * 50 * 0.45 * 0.55 = 0.86625 -> rounds UP to 0.87
    assert q.fee_dollars == pytest.approx(0.87)
    rate, notes = fees.resolve_taker_rate(yv.market)
    assert rate is None  # formula-based, no single rate


def test_half_spread_unavailable_without_bids():
    yv, _ = kalshi_yes_no()
    spread, notes = half_spread_cost(yv.book)
    assert spread is None
    assert SPREAD_UNAVAILABLE in notes


def test_half_spread_with_bids():
    yv, _ = polymarket_pair(yes_ask=0.45)
    spread, _ = half_spread_cost(yv.book)
    assert spread == pytest.approx(0.01)


def test_latency_adjustment_placeholder_flagged():
    adj, notes = latency_adjustment(0.8, 0.002, drift_status="placeholder")
    assert adj == pytest.approx(0.0016)
    assert notes == [LATENCY_DRIFT_PLACEHOLDER]


def test_latency_adjustment_status_labels():
    from backend.arbitrage.costs import (
        LATENCY_DRIFT_CALIBRATED,
        LATENCY_DRIFT_PRELIMINARY,
    )

    _, notes = latency_adjustment(0.8, 0.002, drift_status="preliminary")
    assert notes == [LATENCY_DRIFT_PRELIMINARY]
    _, notes = latency_adjustment(0.8, 0.002, drift_status="calibrated")
    assert notes == [LATENCY_DRIFT_CALIBRATED]
    with pytest.raises(DataValidationError):
        latency_adjustment(0.8, 0.002, drift_status="bogus")


def test_cost_breakdown_waterfall_math():
    cb = build_cost_breakdown(
        raw_edge_per_contract=0.10,
        size=100.0,
        fees_total=2.475,
        slippage_total=0.5,
        spread_info_per_contract=0.01,
        latency_total=0.16,
    )
    assert cb.net_edge == pytest.approx(0.10 - 0.02475 - 0.005 - 0.0016)
    assert cb.spread_cost == pytest.approx(0.01)  # informational only
