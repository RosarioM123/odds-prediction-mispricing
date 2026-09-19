"""Smoke benchmarks for ODDS hot paths (measurement only).

These tests measure the steady-state cost of the engine's hot paths; they
do NOT optimize anything and must never motivate engine edits for speed.
Each benchmark pins a deterministic fixture (fixed PRNG seed, fixed
timestamps, fixed ``now``) and asserts the timed call reproduces a
precomputed reference output, so results are stable across runs.

Timing discipline:
  * Fixture construction (book building, pydantic validation, config load)
    happens OUTSIDE the timed region -- pytest-benchmark only times the
    function under test.
  * No wall-clock assertions anywhere: pass/fail is about functional
    correctness, and timing numbers are reported by the plugin.
  * Rounds are kept small and inputs modest so total added CI time stays
    under ~10 s.

Expected-complexity notes live in each benchmark's docstring together
with the dominant cost.
"""

from __future__ import annotations

import random
from datetime import timedelta

import pytest

from backend.arbitrage.costs import FeeModel, walk_book
from backend.arbitrage.opportunities import BookView, detect_all
from backend.arbitrage.settings import StrategyConfig
from backend.arbitrage.sizing import size_position
from backend.backtesting.replay import ReplayEngine, SnapshotInput
from backend.execution.paper import PaperBroker
from backend.risk.gates import PortfolioState, RiskGate
from backend.schemas import OrderBook, OrderBookLevel, Outcome, Venue
from tests.fixtures import T0, make_market

# Deterministic fixture seed and frozen decision time.
SEED = 20260918
NOW = T0
SIM = "simulated"


def _deep_book(market, n_levels: int, rng: random.Random, base_price: float, at=NOW) -> OrderBook:
    """Deterministic n-level book: asks ascending, bids descending."""
    asks = [
        OrderBookLevel(
            price=round(base_price + i * 0.002, 4), size=round(rng.uniform(5.0, 50.0), 2)
        )
        for i in range(n_levels)
    ]
    bids = [
        OrderBookLevel(
            price=round(base_price - 0.02 - i * 0.002, 4), size=round(rng.uniform(5.0, 50.0), 2)
        )
        for i in range(n_levels)
    ]
    return OrderBook(
        market_id=market.market_id,
        venue=market.venue,
        outcome=market.outcome,
        bids=bids,
        asks=asks,
        venue_timestamp=at,
        received_timestamp=at,
    )


def _event_views(
    rng: random.Random, event_idx: int, n_levels: int, at=NOW, base_ask: float = 0.45
) -> list[BookView]:
    """PM + Kalshi YES/NO BookViews for one event, sharing a question."""
    question = f"Will benchmark event {event_idx} resolve YES?"
    views = []
    for venue in (Venue.POLYMARKET, Venue.KALSHI):
        for outcome in (Outcome.YES, Outcome.NO):
            market = make_market(
                venue=venue,
                outcome=outcome,
                market_id=f"{venue.value}-evt{event_idx}-{outcome.value}",
                event_id=f"evt-{event_idx}",
                question=question,
                taker_fee_rate=0.05 if venue == Venue.POLYMARKET else None,
            )
            views.append(BookView(market, _deep_book(market, n_levels, rng, base_ask, at), SIM))
    return views


def _opp_signature(opp) -> tuple:
    """Structural identity of an opportunity, ignoring the utcnow() ID."""
    return (
        opp.strategy,
        opp.legs[0]["venue"],
        round(opp.costs.net_edge, 6),
        round(opp.liquidity, 6),
    )


@pytest.fixture(scope="module")
def config() -> StrategyConfig:
    return StrategyConfig.load()


@pytest.fixture(scope="module")
def fee_model() -> FeeModel:
    return FeeModel()


@pytest.fixture(scope="module")
def detection_views() -> list[BookView]:
    """32 views: 8 events x (PM YES/NO + Kalshi YES/NO), 20 levels each.

    Pricing (YES/NO asks 0.45/0.45) yields 4 opportunities per event
    (2 bundle + 2 cross-venue complement), so the reference run finds 32.
    """
    rng = random.Random(SEED)
    views: list[BookView] = []
    for i in range(8):
        views.extend(_event_views(rng, i, n_levels=20))
    return views


@pytest.fixture(scope="module")
def reference_opps(detection_views, config, fee_model):
    opps, flags = detect_all(detection_views, config=config, fee_model=fee_model, now=NOW)
    assert len(opps) == 32, f"fixture expects 32 opps, got {len(opps)}"
    return [_opp_signature(o) for o in opps], sorted(flags)


@pytest.fixture(scope="module")
def deep_ask_book():
    """Single 200-level ask book for full-book VWAP measurement."""
    rng = random.Random(SEED)
    market = make_market(venue=Venue.POLYMARKET, outcome=Outcome.YES, market_id="pm-deep")
    return _deep_book(market, 200, rng, 0.45)


@pytest.fixture(scope="module")
def bundle_setup(config, fee_model):
    """One priced bundle opportunity + timelines/markets for paper execution."""
    rng = random.Random(SEED + 1)
    views = _event_views(rng, 99, n_levels=20)
    yes_v, no_v = views[0], views[1]
    from backend.arbitrage.opportunities import detect_bundle_arbitrage

    opp = detect_bundle_arbitrage(yes_v, no_v, config=config, fee_model=fee_model, now=NOW)
    assert opp is not None
    timelines = {}
    markets = {}
    for v in views:
        key = (v.market.venue.value, v.market.market_id, v.market.outcome.value)
        timelines.setdefault(key, []).append(v.book)
        markets[key] = v.market
    broker = PaperBroker(fee_model, config)
    return opp, broker, timelines, markets


@pytest.fixture(scope="module")
def replay_setup(config):
    """4 labeled snapshots, 60 s apart -- the demo-fixture snapshot shape
    used by tests/test_replay.py (data/ holds no committed snapshots)."""
    snapshots = []
    for i in range(4):
        at = T0 + timedelta(seconds=60 * i)
        rng = random.Random(SEED + i)
        views = _event_views(rng, i, n_levels=20, at=at)
        snapshots.append(
            SnapshotInput(
                label=SIM,
                venue=Venue.POLYMARKET,
                markets=[v.market for v in views[:2]],
                books=[v.book for v in views[:2]],
                captured_at=at,
            )
        )
    engine = ReplayEngine(config=config, bankroll=100.0)
    report = engine.run(snapshots)
    assert report.n_executed == 4, "fixture expects all 4 snapshots to execute"
    reference = {
        "n_snapshots": report.n_snapshots,
        "n_executed": report.n_executed,
        "n_rejected": report.n_rejected,
        "trades_by_status": dict(report.trades_by_status),
        "total_realized_net": round(report.total_realized_net, 6),
        "total_expected_net": round(report.total_expected_net, 6),
    }
    return engine, snapshots, reference


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


@pytest.mark.benchmark(min_rounds=5, max_time=1.0)
def test_bench_detect_all(benchmark, detection_views, config, fee_model, reference_opps):
    """Arbitrage detection over a realistic view set.

    Expected complexity: O(V^2) in the number of views for the cross-venue
    pair loop (deterministic_match is O(1) string compare; most pairs exit
    on question mismatch), plus O(levels) walk_book per candidate leg.
    Dominant cost: per-pair match attempts + book walks for the ~32
    candidates that survive to costing.
    """
    expected_sigs, expected_flags = reference_opps

    def run():
        return detect_all(detection_views, config=config, fee_model=fee_model, now=NOW)

    opps, flags = benchmark(run)
    assert [_opp_signature(o) for o in opps] == expected_sigs
    assert sorted(flags) == expected_flags


@pytest.mark.benchmark(min_rounds=50, max_time=0.5)
def test_bench_walk_book_vwap(benchmark, deep_ask_book):
    """Full-book VWAP cost computation over a 200-level book.

    Expected complexity: O(levels) -- a single linear scan consuming asks
    best-first, accumulating notional and filled size.
    Dominant cost: the Python-level loop over OrderBookLevel objects.
    """
    size = deep_ask_book.ask_depth
    expected = walk_book(deep_ask_book, "buy", size)

    result = benchmark(walk_book, deep_ask_book, "buy", size)
    assert result.filled == expected.filled
    assert result.shortfall == expected.shortfall
    assert result.vwap == expected.vwap
    assert result.levels_used == 200


@pytest.mark.benchmark(min_rounds=100, max_time=0.5)
def test_bench_size_position(benchmark, config):
    """Fractional-Kelly sizing.

    Expected complexity: O(1) -- closed-form arithmetic (full-Kelly
    fraction, stake, liquidity/max-position caps). Included as a smoke
    check that the sizing path stays allocation-light.
    Dominant cost: a handful of float ops and a dataclass construction.
    """
    expected = size_position(
        net_edge_per_contract=0.068,
        cost_per_contract=0.91,
        liquidity=1000.0,
        bankroll=100.0,
        fraction=config.kelly.fraction,
        max_position=config.risk.max_position_per_market,
        p_win=config.kelly.arbitrage_p_win,
        p_win_is_placeholder=False,
    )

    result = benchmark(
        size_position,
        net_edge_per_contract=0.068,
        cost_per_contract=0.91,
        liquidity=1000.0,
        bankroll=100.0,
        fraction=config.kelly.fraction,
        max_position=config.risk.max_position_per_market,
        p_win=config.kelly.arbitrage_p_win,
        p_win_is_placeholder=False,
    )
    assert result.quantity == expected.quantity
    assert result.stake_dollars == expected.stake_dollars
    assert result.capped_by == expected.capped_by


@pytest.mark.benchmark(min_rounds=5, max_time=1.0)
def test_bench_risk_gate(benchmark, config, bundle_setup):
    """Risk-gate evaluation for one opportunity.

    Expected complexity: O(legs + portfolio entries) -- a fixed sequence
    of scalar checks (edge, quantity, position/exposure limits, loss
    limit, book age, match confidence).
    Dominant cost: dict lookups over the (small) portfolio state.
    """
    opp, _broker, _timelines, _markets = bundle_setup
    gate = RiskGate(config)
    portfolio = PortfolioState()
    expected = gate.evaluate(opp, 100.0, portfolio, book_ages_s=[0.0], now=NOW)

    result = benchmark(gate.evaluate, opp, 100.0, portfolio, book_ages_s=[0.0], now=NOW)
    assert result.allow == expected.allow
    assert result.reason == expected.reason


@pytest.mark.benchmark(min_rounds=5, max_time=1.0)
def test_bench_paper_execute(benchmark, bundle_setup):
    """Paper fill application: one opportunity, two legs.

    Expected complexity: O(T + levels) per leg, where T is the timeline
    length scanned by latest_book_at (no-look-ahead scan) and levels is
    the book walked for the fill VWAP.
    Dominant cost: latest_book_at timeline scan + walk_book per leg.
    """
    opp, broker, timelines, markets = bundle_setup
    expected = broker.execute(opp, 100.0, timelines, markets, decide_at=NOW)
    expected_statuses = [t.status.value for t in expected]
    expected_filled = [t.filled_quantity for t in expected]

    trades = benchmark(broker.execute, opp, 100.0, timelines, markets, decide_at=NOW)
    assert [t.status.value for t in trades] == expected_statuses
    assert [t.filled_quantity for t in trades] == expected_filled


@pytest.mark.benchmark(min_rounds=3, max_time=1.5)
def test_bench_replay(benchmark, replay_setup):
    """End-to-end chronological replay over 4 labeled snapshot fixtures.

    Expected complexity: O(S * (V^2 + legs*levels)) for S snapshots:
    per snapshot detect_all dominates, then sizing (O(1)), risk gate
    (O(legs)), and paper execution (O(T + levels) per leg).
    Dominant cost: repeated detect_all over the growing timelines; note
    there is intentionally no rolling-window model fit in this engine --
    nothing here refits parameters, so per-snapshot cost is flat.
    """
    engine, snapshots, reference = replay_setup

    report = benchmark(engine.run, snapshots)
    assert report.n_snapshots == reference["n_snapshots"]
    assert report.n_executed == reference["n_executed"]
    assert report.n_rejected == reference["n_rejected"]
    assert dict(report.trades_by_status) == reference["trades_by_status"]
    assert round(report.total_realized_net, 6) == reference["total_realized_net"]
    assert round(report.total_expected_net, 6) == reference["total_expected_net"]
