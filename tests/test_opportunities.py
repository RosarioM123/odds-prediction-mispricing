"""Tests for arbitrage detection: bundle, cross-venue, matcher, detect_all."""
from datetime import timedelta

import pytest

from backend.arbitrage.costs import FeeModel
from backend.arbitrage.opportunities import (
    CONFLICTING_METADATA,
    BookView,
    detect_all,
    detect_bundle_arbitrage,
    detect_cross_venue_complement,
    detect_cross_venue_direct,
    deterministic_match,
    normalize_question,
)
from backend.arbitrage.settings import StrategyConfig
from backend.schemas import Outcome, Venue
from tests.fixtures import (
    T0,
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


# -- bundle arbitrage ----------------------------------------------------

def test_bundle_detected_with_edge(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0)
    assert opp is not None
    assert opp.strategy == "bundle_arbitrage"
    assert opp.costs.raw_edge == pytest.approx(0.10)
    assert opp.costs.net_edge > config.detection.min_net_edge
    assert len(opp.legs) == 2
    assert "summary" in opp.explanation
    assert "Polymarket" in opp.explanation["summary"] or "polymarket" in opp.explanation["summary"].lower()


def test_bundle_no_edge_returns_none(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.55, no_ask=0.55, depth=100.0)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0) is None


def test_bundle_empty_book_returns_none(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45)
    empty = make_book(nv.market, asks=[], at=T0)
    nv_empty = BookView(nv.market, empty, "simulated")
    assert detect_bundle_arbitrage(yv, nv_empty, config=config, fee_model=fees, now=T0) is None


def test_bundle_stale_book_rejected(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45)
    stale_at = T0 - timedelta(seconds=30)
    stale_book = make_book(yv.market, asks=[(0.45, 100.0)], at=stale_at)
    yv_stale = BookView(yv.market, stale_book, "simulated")
    assert detect_bundle_arbitrage(yv_stale, nv, config=config, fee_model=fees, now=T0) is None


def test_bundle_below_min_liquidity_rejected(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=10.0)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0) is None


def test_bundle_below_min_net_edge_after_costs_rejected(config, fees):
    # Tiny edge that fees wipe out.
    yv, nv = polymarket_pair(yes_ask=0.495, no_ask=0.495, depth=100.0)
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0) is None


def test_bundle_labels_propagate(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees, now=T0)
    assert "LABEL_SIMULATED" in opp.explanation["flags"]


# -- deterministic matcher -------------------------------------------------

def test_match_exact_question():
    a = make_market(venue=Venue.POLYMARKET, question="Will it rain tomorrow?")
    b = make_market(venue=Venue.KALSHI, question="Will it rain tomorrow?!!")
    m = deterministic_match(a, b)
    assert m.matched
    assert m.confidence >= 0.85


def test_match_different_questions():
    a = make_market(venue=Venue.POLYMARKET, question="Will it rain tomorrow?")
    b = make_market(venue=Venue.KALSHI, question="Will the Fed cut rates?")
    assert not deterministic_match(a, b).matched


def test_match_same_venue_rejected():
    a = make_market(venue=Venue.POLYMARKET, question="Will it rain tomorrow?")
    b = make_market(venue=Venue.POLYMARKET, question="Will it rain tomorrow?")
    assert not deterministic_match(a, b).matched


def test_normalize_question():
    assert normalize_question("  Will it RAIN tomorrow?! ") == "will it rain tomorrow"


# -- cross-venue ------------------------------------------------------------

def test_cross_venue_direct_detected(config, fees):
    buy_m = make_market(venue=Venue.POLYMARKET, outcome=Outcome.YES,
                        market_id="pm-a", question="Will it rain tomorrow?")
    sell_m = make_market(venue=Venue.POLYMARKET, outcome=Outcome.YES,
                         market_id="pm-b", question="Will it rain tomorrow?")
    # second venue for the sell side
    sell_m = make_market(venue=Venue.KALSHI, outcome=Outcome.YES,
                         market_id="kx-a", question="Will it rain tomorrow?",
                         taker_fee_rate=None)
    buy_book = make_book(buy_m, bids=[(0.48, 100.0)], asks=[(0.50, 100.0)], at=T0)
    sell_book = make_book(sell_m, bids=[(0.60, 100.0)], asks=[(0.62, 100.0)], at=T0)
    bv, sv = BookView(buy_m, buy_book, "simulated"), BookView(sell_m, sell_book, "simulated")
    opp = detect_cross_venue_direct(bv, sv, config=config, fee_model=fees, now=T0)
    assert opp is not None
    assert opp.strategy == "cross_venue_arbitrage"
    assert opp.match_confidence >= config.cross_venue.min_match_confidence
    legs = {l["side"] for l in opp.legs}
    assert legs == {"buy", "sell"}


def test_cross_venue_direct_no_bids_not_invented(config, fees):
    # Kalshi-style sell book: asks only. Detector must skip, not invent a bid.
    buy_m = make_market(venue=Venue.POLYMARKET, outcome=Outcome.YES,
                        market_id="pm-a", question="Will it rain tomorrow?")
    sell_m = make_market(venue=Venue.KALSHI, outcome=Outcome.YES,
                         market_id="kx-a", question="Will it rain tomorrow?",
                         taker_fee_rate=None)
    buy_book = make_book(buy_m, asks=[(0.50, 100.0)], at=T0)
    sell_book = make_book(sell_m, asks=[(0.62, 100.0)], at=T0)  # no bids
    bv = BookView(buy_m, buy_book, "simulated")
    sv = BookView(sell_m, sell_book, "simulated")
    assert detect_cross_venue_direct(bv, sv, config=config, fee_model=fees, now=T0) is None


def test_cross_venue_complement_works_without_bids(config, fees):
    # Polymarket YES + Kalshi NO (asks only): complement needs no bids.
    yv, _ = polymarket_pair(yes_ask=0.45, depth=100.0)
    _, knv = kalshi_yes_no(no_ask=0.45, depth=100.0)
    opp = detect_cross_venue_complement(yv, knv, config=config, fee_model=fees, now=T0)
    assert opp is not None
    assert opp.strategy == "cross_venue_arbitrage"
    assert opp.costs.net_edge > 0


# -- detect_all ---------------------------------------------------------------

def test_detect_all_dedupes_to_newest(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=100.0)
    # Duplicate YES book, older timestamp: must be ignored.
    old_book = make_book(yv.market, asks=[(0.45, 100.0)], at=T0 - timedelta(seconds=2))
    dup = BookView(yv.market, old_book, "simulated")
    opps, _ = detect_all([yv, nv, dup], config=config, fee_model=fees, now=T0)
    bundles = [o for o in opps if o.strategy == "bundle_arbitrage"]
    assert len(bundles) == 1


def test_detect_all_conflicting_metadata_skipped(config, fees):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45)
    bad = make_market(venue=Venue.POLYMARKET, outcome=Outcome.NO,
                      market_id="pm-no-2", event_id="evt-1",
                      question="Something completely different")
    bad_book = make_book(bad, asks=[(0.45, 100.0)], at=T0)
    opps, flags = detect_all(
        [yv, nv, BookView(bad, bad_book, "simulated")],
        config=config, fee_model=fees, now=T0)
    assert opps == []
    assert any(CONFLICTING_METADATA in f for f in flags)
