"""Phase 1 tests: normalized schemas and configuration loading."""

from datetime import UTC, datetime

import pytest

from backend.config import load_all, load_yaml
from backend.schemas import (
    CostBreakdown,
    LatencyBreakdown,
    Market,
    MarketStatus,
    Opportunity,
    OrderBook,
    OrderBookLevel,
    Outcome,
    PaperTrade,
    PaperTradeStatus,
    Venue,
)


def make_book(**kwargs) -> OrderBook:
    params = {
        "market_id": "poly-1-yes",
        "venue": Venue.POLYMARKET,
        "outcome": Outcome.YES,
        "bids": [OrderBookLevel(price=0.48, size=100), OrderBookLevel(price=0.47, size=200)],
        "asks": [OrderBookLevel(price=0.52, size=150), OrderBookLevel(price=0.53, size=250)],
    }
    params.update(kwargs)
    return OrderBook(**params)


def test_order_book_best_bid_ask_spread_mid():
    book = make_book()
    assert book.best_bid == 0.48
    assert book.best_ask == 0.52
    assert book.spread == pytest.approx(0.04)
    assert book.mid_price == pytest.approx(0.50)


def test_order_book_rejects_unsorted_levels():
    with pytest.raises(ValueError):
        make_book(bids=[OrderBookLevel(price=0.47, size=1), OrderBookLevel(price=0.48, size=1)])


def test_order_book_rejects_out_of_range_price():
    with pytest.raises(ValueError):
        OrderBookLevel(price=1.5, size=10)


def test_order_book_empty_side_gives_none():
    book = make_book(bids=[], asks=[])
    assert book.best_bid is None
    assert book.spread is None
    assert book.bid_depth == 0


def test_order_book_depth():
    book = make_book()
    assert book.bid_depth == pytest.approx(300)
    assert book.ask_depth == pytest.approx(400)


def test_market_schema_defaults():
    m = Market(
        market_id="kalshi-FED-25DEC",
        venue=Venue.KALSHI,
        event_id="FED-25",
        question="Will the Fed cut rates in December?",
        outcome=Outcome.YES,
    )
    assert m.status == MarketStatus.UNKNOWN
    assert m.tick_size == 0.01
    assert m.raw == {}


def test_cost_breakdown_defaults_to_zero():
    costs = CostBreakdown(raw_edge=0.042)
    assert costs.net_edge == 0.0
    assert costs.trading_fees == 0.0


def test_latency_breakdown_math():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    lb = LatencyBreakdown(
        market_data_timestamp=t0,
        detection_timestamp=t0,
        decision_timestamp=t0,
        simulated_execution_timestamp=t0,
    )
    assert lb.total_latency_ms == 0.0


def test_opportunity_and_paper_trade_link():
    opp = Opportunity(
        opportunity_id="opp-1",
        strategy="bundle_arbitrage",
        costs=CostBreakdown(raw_edge=0.02, net_edge=0.012),
        liquidity=500,
        decision="PAPER_EXECUTE",
    )
    trade = PaperTrade(
        trade_id="t-1",
        opportunity_id=opp.opportunity_id,
        venue=Venue.POLYMARKET,
        market_id="poly-1-yes",
        side="BUY",
        quantity=100,
        expected_price=0.48,
        simulated_fill_price=0.481,
        status=PaperTradeStatus.FILLED,
    )
    assert trade.opportunity_id == opp.opportunity_id


def test_configs_load():
    cfg = load_all()
    assert "fees" in cfg and "strategy" in cfg and "venues" in cfg
    assert cfg["fees"]["kalshi"]["settlement_fee"] == 0.0
    assert cfg["strategy"]["detection"]["min_net_edge"] > 0
    assert cfg["venues"]["polymarket"]["auth_for_market_data"] is False


def test_fee_formulas_present():
    fees = load_yaml("fees.yaml")
    assert "0.07" in fees["kalshi"]["taker_formula"]
    assert "fee_rate" in fees["polymarket"]["formula"]
