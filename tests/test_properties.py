"""Property-based tests for ODDS engine invariants (Hypothesis).

Covers, over generated inputs:

  1. Arbitrage detectors never emit an opportunity with negative net edge,
     and respect the configured min-net-edge thresholds
     (``detection.min_net_edge`` for bundle, ``cross_venue.min_net_edge``
     for cross-venue strategies).
  2. Fractional-Kelly sizing output stays within [0, cap], where
     cap = min(liquidity, max_position_per_market).
  3. Paper execution conserves cash: starting from cash C, a random sequence
     of fills satisfies
     cash == initial_cash - total_paid + total_received - total_fees
     (no money created or destroyed beyond rounding).
  4. Schema models survive a JSON round-trip unchanged
     (``model_dump_json`` -> ``model_validate_json`` -> equal).
  5. Determinism: detector output is identical when book level order is
     shuffled (adapters canonicalize to best-first); ``detect_all`` output
     is identical under view reordering; replay output is identical for
     identical snapshot sequences (and for reordered input snapshots).

Engine-wide / slower properties are marked ``pytest.mark.slow``.
``max_examples`` is kept modest (25-75) and deadlines are disabled so CI
stays sane.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from backend.arbitrage.costs import FeeModel
from backend.arbitrage.opportunities import (
    BookView,
    detect_all,
    detect_bundle_arbitrage,
    detect_cross_venue_complement,
    detect_cross_venue_direct,
)
from backend.arbitrage.settings import StrategyConfig
from backend.arbitrage.sizing import (
    KELLY_FRACTIONS,
    kelly_optimal_fraction,
    size_position,
)
from backend.backtesting.replay import ReplayEngine, SnapshotInput
from backend.execution.paper import PaperBroker
from backend.markets.kalshi import KalshiAdapter
from backend.markets.polymarket import PolymarketAdapter
from backend.schemas import (
    CostBreakdown,
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
from tests.fixtures import T0, make_book, make_market, polymarket_pair

_CONFIG = StrategyConfig.load()
_FEES = FeeModel()
_BROKER = PaperBroker(_FEES, _CONFIG)
_PMA = PolymarketAdapter()
_KA = KalshiAdapter()

# -- shared strategies ---------------------------------------------------

_PRICE = st.floats(min_value=0.01, max_value=0.99, allow_nan=False, allow_infinity=False)
_PRICE01 = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
_PSIZE = st.floats(
    min_value=0.0, max_value=10000.0, exclude_min=True, allow_nan=False, allow_infinity=False
)
_NNSIZE = st.floats(min_value=0.0, max_value=10000.0, allow_nan=False, allow_infinity=False)
_ID = st.text(min_size=1, max_size=24)
_DT = st.datetimes(min_value=datetime(2020, 1, 1), max_value=datetime(2030, 1, 1)).map(
    lambda d: d.replace(tzinfo=UTC)
)
_FINITE = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)


def _canon_opp(opp: Opportunity) -> str:
    """Opportunity with volatile fields removed, as canonical JSON.

    ``opportunity_id`` embeds ``utcnow()`` and ``detected_at`` defaults to
    ``utcnow()``, so both are excluded when comparing detector outputs for
    equality.
    """
    data = opp.model_dump(mode="json", exclude={"opportunity_id", "detected_at"})
    return json.dumps(data, sort_keys=True)


def _canon_book(book: OrderBook) -> str:
    """OrderBook as canonical JSON, excluding the volatile ``received_timestamp``.

    ``normalize_order_book`` stamps ``received_timestamp=utcnow()`` on every
    call, so two normalizations of the same payload can never compare equal
    field-for-field; everything else must be identical.
    """
    return json.dumps(book.model_dump(mode="json", exclude={"received_timestamp"}), sort_keys=True)


def _canon_report(report) -> str:
    data = report.to_dict()
    for row in data["rows"]:
        # opportunity_id embeds the detection timestamp -> scrub for equality.
        row.pop("opportunity_id", None)
    return json.dumps(data, sort_keys=True)


# -- 1. detector: never negative net edge ----------------------------------


@st.composite
def _bundle_views(draw):
    """YES/NO BookViews on one venue/event with generated touch + depth."""
    fee_rate = draw(st.floats(0.0, 0.2, allow_nan=False, allow_infinity=False))
    yes_ask = draw(st.floats(0.05, 0.95, allow_nan=False, allow_infinity=False))
    no_ask = draw(st.floats(0.05, 0.95, allow_nan=False, allow_infinity=False))
    depth = draw(st.floats(1.0, 5000.0, allow_nan=False, allow_infinity=False))
    n_extra = draw(st.integers(0, 3))

    def asks(touch):
        lvls = [(touch, depth)]
        for k in range(1, n_extra + 1):
            lvls.append((min(touch + 0.02 * k, 0.999), draw(_PSIZE)))
        return lvls

    ym = make_market(
        venue=Venue.POLYMARKET,
        outcome=Outcome.YES,
        market_id="pm-yes-1",
        event_id="evt-1",
        taker_fee_rate=fee_rate,
    )
    nm = make_market(
        venue=Venue.POLYMARKET,
        outcome=Outcome.NO,
        market_id="pm-no-1",
        event_id="evt-1",
        taker_fee_rate=fee_rate,
    )
    yb = make_book(ym, bids=[(max(yes_ask - 0.02, 0.001), depth)], asks=asks(yes_ask), at=T0)
    nb = make_book(nm, bids=[(max(no_ask - 0.02, 0.001), depth)], asks=asks(no_ask), at=T0)
    return BookView(ym, yb, "simulated"), BookView(nm, nb, "simulated")


@given(views=_bundle_views())
@settings(max_examples=50, deadline=None)
def test_bundle_arbitrage_never_negative_net_edge(views):
    yv, nv = views
    emitted = []
    opp = detect_bundle_arbitrage(yv, nv, config=_CONFIG, fee_model=_FEES, now=T0)
    if opp is not None:
        emitted.append(opp)
    opps, _flags = detect_all([yv, nv], config=_CONFIG, fee_model=_FEES, now=T0)
    emitted.extend(opps)
    for o in emitted:
        assert o.costs.net_edge >= 0.0
        # min-edge threshold semantics: nothing below min_net_edge is emitted.
        assert o.costs.net_edge >= _CONFIG.detection.min_net_edge
        assert o.costs.raw_edge > 0.0
        assert all(leg["quantity"] > 0 for leg in o.legs)


@st.composite
def _cross_venue_views(draw):
    """YES/NO views on Polymarket + Kalshi for one shared event/question."""
    question = "Will it rain tomorrow?"
    fee_rate = draw(st.floats(0.0, 0.2, allow_nan=False, allow_infinity=False))
    views = []
    specs = [
        (Venue.POLYMARKET, Outcome.YES, "pm-yes-1", fee_rate),
        (Venue.POLYMARKET, Outcome.NO, "pm-no-1", fee_rate),
        (Venue.KALSHI, Outcome.YES, "kx-yes-1", None),
        (Venue.KALSHI, Outcome.NO, "kx-no-1", None),
    ]
    for venue, outcome, mid, fr in specs:
        touch = draw(st.floats(0.05, 0.95, allow_nan=False, allow_infinity=False))
        depth = draw(st.floats(60.0, 5000.0, allow_nan=False, allow_infinity=False))
        n_extra = draw(st.integers(0, 3))
        ask_lvls = [(touch, depth)]
        for k in range(1, n_extra + 1):
            ask_lvls.append((min(touch + 0.02 * k, 0.999), draw(_PSIZE)))
        n_bids = draw(st.integers(0, 3))
        bid_lvls = [(max(touch - 0.02 * (k + 1), 0.001), draw(_PSIZE)) for k in range(n_bids)]
        m = make_market(
            venue=venue,
            outcome=outcome,
            market_id=mid,
            event_id="evt-x",
            question=question,
            taker_fee_rate=fr,
        )
        b = make_book(m, bids=bid_lvls, asks=ask_lvls, at=T0)
        views.append(BookView(m, b, "simulated"))
    return views


@given(views=_cross_venue_views())
@settings(max_examples=50, deadline=None)
def test_cross_venue_never_negative_net_edge(views):
    by_key = {(v.market.venue, v.market.outcome): v for v in views}
    pm_y = by_key[(Venue.POLYMARKET, Outcome.YES)]
    pm_n = by_key[(Venue.POLYMARKET, Outcome.NO)]
    kx_y = by_key[(Venue.KALSHI, Outcome.YES)]
    kx_n = by_key[(Venue.KALSHI, Outcome.NO)]
    candidates = [
        detect_cross_venue_direct(pm_y, kx_y, config=_CONFIG, fee_model=_FEES, now=T0),
        detect_cross_venue_direct(kx_y, pm_y, config=_CONFIG, fee_model=_FEES, now=T0),
        detect_cross_venue_complement(pm_y, kx_n, config=_CONFIG, fee_model=_FEES, now=T0),
        detect_cross_venue_complement(kx_y, pm_n, config=_CONFIG, fee_model=_FEES, now=T0),
    ]
    opps, _flags = detect_all(views, config=_CONFIG, fee_model=_FEES, now=T0)
    for o in [c for c in candidates if c is not None] + opps:
        assert o.costs.net_edge >= 0.0
        if o.strategy == "bundle_arbitrage":
            assert o.costs.net_edge >= _CONFIG.detection.min_net_edge
        else:
            assert o.strategy == "cross_venue_arbitrage"
            assert o.costs.net_edge >= _CONFIG.cross_venue.min_net_edge


# -- 2. Kelly sizing bounds -------------------------------------------------


@given(
    net_edge=st.floats(-2.0, 2.0, allow_nan=False, allow_infinity=False),
    cost_per_contract=st.floats(0.001, 20.0, allow_nan=False, allow_infinity=False),
    liquidity=st.floats(0.0, 20000.0, allow_nan=False, allow_infinity=False),
    bankroll=st.floats(-5000.0, 1_000_000.0, allow_nan=False, allow_infinity=False),
    fraction=st.sampled_from(KELLY_FRACTIONS),
    max_position=st.floats(0.0, 20000.0, allow_nan=False, allow_infinity=False),
    p_win=st.floats(0.001, 0.999, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=75, deadline=None)
def test_fractional_kelly_output_bounded(
    net_edge, cost_per_contract, liquidity, bankroll, fraction, max_position, p_win
):
    result = size_position(
        net_edge_per_contract=net_edge,
        cost_per_contract=cost_per_contract,
        liquidity=liquidity,
        bankroll=bankroll,
        fraction=fraction,
        max_position=max_position,
        p_win=p_win,
    )
    cap = min(liquidity, max_position)
    # quantity is rounded to 6 decimals, which can nudge a capped value by
    # < 1e-6 above the cap.
    assert 0.0 <= result.quantity <= cap + 1e-6
    assert result.stake_dollars >= 0.0
    assert result.kelly_full >= 0.0
    assert result.fraction_used == fraction


@given(
    p_win=st.floats(0.001, 0.999, allow_nan=False, allow_infinity=False),
    odds_b=st.floats(-5.0, 5.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=50, deadline=None)
def test_kelly_fraction_never_negative(p_win, odds_b):
    assert kelly_optimal_fraction(p_win, odds_b) >= 0.0


# -- 3. paper ledger conservation --------------------------------------------


@st.composite
def _ledger_scenario(draw):
    """One market + book plus a random sequence of (side, qty, stale?) fills."""
    fee_rate = draw(st.floats(0.0, 0.2, allow_nan=False, allow_infinity=False))
    market = make_market(
        venue=Venue.POLYMARKET,
        outcome=Outcome.YES,
        market_id="pm-ledger-1",
        event_id="evt-ledger",
        taker_fee_rate=fee_rate,
    )
    n = draw(st.integers(1, 5))
    ask_pts = {
        (
            draw(st.floats(0.5, 0.95, allow_nan=False, allow_infinity=False)),
            draw(st.floats(1.0, 1000.0, allow_nan=False, allow_infinity=False)),
        )
        for _ in range(n)
    }
    bid_pts = {
        (
            draw(st.floats(0.05, 0.499, allow_nan=False, allow_infinity=False)),
            draw(st.floats(1.0, 1000.0, allow_nan=False, allow_infinity=False)),
        )
        for _ in range(n)
    }
    book = make_book(market, bids=sorted(bid_pts, reverse=True), asks=sorted(ask_pts), at=T0)
    fills = []
    for _ in range(draw(st.integers(1, 10))):
        fills.append(
            (
                draw(st.sampled_from(["buy", "sell"])),
                draw(st.floats(0.01, 400.0, allow_nan=False, allow_infinity=False)),
                draw(st.booleans()),  # stale -> MISSED, exercises the zero-flow path
            )
        )
    return market, book, fills


@pytest.mark.slow
@given(
    scenario=_ledger_scenario(),
    initial_cash=st.floats(100.0, 1_000_000.0, allow_nan=False, allow_infinity=False),
)
@settings(max_examples=25, deadline=None)
def test_paper_execution_conserves_cash(scenario, initial_cash):
    market, book, fills = scenario
    key = (market.venue.value, market.market_id, "YES")
    timelines = {key: [book]}
    markets = {key: market}
    cash = initial_cash
    paid = received = fees_paid = 0.0
    n = 0
    for side, qty, stale in fills:
        expected_price = book.best_ask if side == "buy" else book.best_bid
        opp = Opportunity(
            opportunity_id=f"ledger-{n}",
            strategy="test",
            legs=[
                {
                    "venue": market.venue.value,
                    "market_id": market.market_id,
                    "outcome": "YES",
                    "side": side,
                    "quantity": qty,
                    "expected_price": expected_price,
                }
            ],
            costs=CostBreakdown(raw_edge=0.0),
            liquidity=qty,
        )
        decide_at = T0 + timedelta(seconds=60) if stale else T0
        (trade,) = _BROKER.execute(opp, qty, timelines, markets, decide_at=decide_at)
        n += 1
        cash += trade.net_pnl
        if trade.filled_quantity > 0:
            assert trade.status in (PaperTradeStatus.FILLED, PaperTradeStatus.PARTIALLY_FILLED)
            notional = trade.simulated_fill_price * trade.filled_quantity
            if side == "buy":
                paid += notional
            else:
                received += notional
            fees_paid += trade.fees
            signed = -1.0 if side == "buy" else 1.0
            # per-trade accounting: signed cash flow minus fees
            assert abs(trade.net_pnl - (signed * notional - trade.fees)) <= 2e-6
        else:
            # MISSED/EXPIRED: no cash moves at all.
            assert trade.status in (PaperTradeStatus.MISSED, PaperTradeStatus.EXPIRED)
            assert trade.fees == 0.0
            assert trade.gross_pnl == 0.0
            assert trade.net_pnl == 0.0
    # Conservation: no money created or destroyed (up to rounding).
    assert abs(cash - (initial_cash - paid + received - fees_paid)) <= 5e-6 * n


# -- 4. serialization round-trips --------------------------------------------


@st.composite
def _order_books(draw):
    nb = draw(st.integers(0, 5))
    na = draw(st.integers(0, 5))
    bid_pts = {(draw(_PRICE01), draw(_NNSIZE)) for _ in range(nb)}
    ask_pts = {(draw(_PRICE01), draw(_NNSIZE)) for _ in range(na)}
    bids = [OrderBookLevel(price=p, size=s) for p, s in sorted(bid_pts, reverse=True)]
    asks = [OrderBookLevel(price=p, size=s) for p, s in sorted(ask_pts)]
    return OrderBook(
        market_id=draw(_ID),
        venue=draw(st.sampled_from(Venue)),
        outcome=draw(st.sampled_from(Outcome)),
        bids=bids,
        asks=asks,
        venue_timestamp=draw(st.one_of(st.none(), _DT)),
        received_timestamp=draw(_DT),
    )


@st.composite
def _markets(draw):
    return Market(
        market_id=draw(_ID),
        venue=draw(st.sampled_from(Venue)),
        event_id=draw(_ID),
        question=draw(st.text(max_size=60)),
        outcome=draw(st.sampled_from(Outcome)),
        expiration=draw(st.one_of(st.none(), _DT)),
        status=draw(st.sampled_from(MarketStatus)),
        tick_size=draw(st.floats(0.0001, 1.0, allow_nan=False, allow_infinity=False)),
        taker_fee_rate=draw(
            st.one_of(st.none(), st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False))
        ),
        raw=draw(
            st.dictionaries(
                st.text(min_size=1, max_size=12),
                st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=16)),
                max_size=4,
            )
        ),
    )


@st.composite
def _cost_breakdowns(draw):
    return CostBreakdown(
        raw_edge=draw(_FINITE),
        trading_fees=draw(_FINITE),
        spread_cost=draw(_FINITE),
        slippage=draw(_FINITE),
        latency_adjustment=draw(_FINITE),
        net_edge=draw(_FINITE),
    )


_JSON_LEAF = st.one_of(st.none(), st.booleans(), _FINITE, st.text(max_size=16))


@st.composite
def _opportunities(draw):
    legs = []
    for _ in range(draw(st.integers(0, 3))):
        legs.append(
            {
                "venue": draw(st.sampled_from(["polymarket", "kalshi"])),
                "market_id": draw(_ID),
                "outcome": draw(st.sampled_from(["YES", "NO"])),
                "side": draw(st.sampled_from(["buy", "sell"])),
                "quantity": draw(_NNSIZE),
                "expected_price": draw(_PRICE01),
            }
        )
    return Opportunity(
        opportunity_id=draw(_ID),
        strategy=draw(st.sampled_from(["bundle_arbitrage", "cross_venue_arbitrage"])),
        detected_at=draw(_DT),
        legs=legs,
        costs=draw(_cost_breakdowns()),
        liquidity=draw(_NNSIZE),
        match_confidence=draw(
            st.one_of(st.none(), st.floats(0.0, 1.0, allow_nan=False, allow_infinity=False))
        ),
        explanation=draw(st.dictionaries(st.text(min_size=1, max_size=12), _JSON_LEAF, max_size=5)),
    )


@st.composite
def _paper_trades(draw):
    return PaperTrade(
        trade_id=draw(_ID),
        opportunity_id=draw(_ID),
        timestamp=draw(_DT),
        venue=draw(st.sampled_from(Venue)),
        market_id=draw(_ID),
        side=draw(st.sampled_from(["buy", "sell"])),
        quantity=draw(st.floats(0.001, 10000.0, allow_nan=False, allow_infinity=False)),
        expected_price=draw(_FINITE),
        simulated_fill_price=draw(_FINITE),
        filled_quantity=draw(_NNSIZE),
        fees=draw(_FINITE),
        slippage=draw(_FINITE),
        gross_pnl=draw(_FINITE),
        net_pnl=draw(_FINITE),
        latency_ms=draw(_FINITE),
        status=draw(st.sampled_from(PaperTradeStatus)),
    )


@given(book=_order_books())
@settings(max_examples=50, deadline=None)
def test_orderbook_json_roundtrip(book):
    assert OrderBook.model_validate_json(book.model_dump_json()) == book


@given(market=_markets())
@settings(max_examples=50, deadline=None)
def test_market_json_roundtrip(market):
    assert Market.model_validate_json(market.model_dump_json()) == market


@given(costs=_cost_breakdowns())
@settings(max_examples=50, deadline=None)
def test_costbreakdown_json_roundtrip(costs):
    assert CostBreakdown.model_validate_json(costs.model_dump_json()) == costs


@given(opp=_opportunities())
@settings(max_examples=50, deadline=None)
def test_opportunity_json_roundtrip(opp):
    assert Opportunity.model_validate_json(opp.model_dump_json()) == opp


@given(trade=_paper_trades())
@settings(max_examples=50, deadline=None)
def test_papertrade_json_roundtrip(trade):
    assert PaperTrade.model_validate_json(trade.model_dump_json()) == trade


# -- 5. determinism -----------------------------------------------------------


@given(
    bids=st.lists(st.tuples(_PRICE, _PSIZE), max_size=6, unique_by=lambda lvl: lvl[0]),
    asks=st.lists(st.tuples(_PRICE, _PSIZE), min_size=1, max_size=6, unique_by=lambda lvl: lvl[0]),
)
@settings(max_examples=50, deadline=None)
def test_polymarket_adapter_canonicalizes_level_order(bids, asks):
    """Shuffled raw level order normalizes to the same OrderBook.

    Prices are unique per side so best-first sorting is a total order;
    duplicate-price stability is covered at the detector level instead
    (test_detector_identical_under_shuffled_book_levels).
    """
    market = make_market(outcome=Outcome.YES)

    def payload(b, a):
        return {
            "bids": [{"price": repr(p), "size": repr(s)} for p, s in b],
            "asks": [{"price": repr(p), "size": repr(s)} for p, s in a],
            "timestamp": "1758206400000",
        }

    b1 = _PMA.normalize_order_book(payload(bids, asks), market)
    b2 = _PMA.normalize_order_book(payload(bids[::-1], asks[::-1]), market)

    def rot(xs):
        return xs[1:] + xs[:1] if len(xs) > 1 else xs

    b3 = _PMA.normalize_order_book(payload(rot(bids), rot(asks)), market)
    assert _canon_book(b1) == _canon_book(b2) == _canon_book(b3)


@given(
    yes=st.lists(st.tuples(st.integers(1, 99), _PSIZE), max_size=6, unique_by=lambda lvl: lvl[0]),
    no=st.lists(st.tuples(st.integers(1, 99), _PSIZE), max_size=6, unique_by=lambda lvl: lvl[0]),
)
@settings(max_examples=50, deadline=None)
def test_kalshi_adapter_canonicalizes_level_order(yes, no):
    """Shuffled ladder order normalizes to the same OrderBook, both envelopes.

    Cent prices are unique per ladder so best-first sorting is a total order.
    """
    market = make_market(venue=Venue.KALSHI, outcome=Outcome.YES, taker_fee_rate=None)

    def payload_classic(y, n):
        return {"orderbook": {"yes": [[c, s] for c, s in y], "no": [[c, s] for c, s in n]}}

    def payload_fp(y, n):
        return {
            "orderbook_fp": {
                "yes_dollars": [[f"{c / 100:.4f}", f"{s:.2f}"] for c, s in y],
                "no_dollars": [[f"{c / 100:.4f}", f"{s:.2f}"] for c, s in n],
            }
        }

    c1 = _KA.normalize_order_book(payload_classic(yes, no), market)
    c2 = _KA.normalize_order_book(payload_classic(yes[::-1], no[::-1]), market)
    assert _canon_book(c1) == _canon_book(c2)
    f1 = _KA.normalize_order_book(payload_fp(yes, no), market)
    f2 = _KA.normalize_order_book(payload_fp(yes[::-1], no[::-1]), market)
    assert _canon_book(f1) == _canon_book(f2)


@given(
    extra_yes=st.lists(st.tuples(_PRICE, _PSIZE), max_size=4),
    extra_no=st.lists(st.tuples(_PRICE, _PSIZE), max_size=4),
)
@settings(max_examples=50, deadline=None)
def test_detector_identical_under_shuffled_book_levels(extra_yes, extra_no):
    """Detector output is identical when raw book level order is shuffled."""
    ym = make_market(
        venue=Venue.POLYMARKET, outcome=Outcome.YES, market_id="pm-yes-1", event_id="evt-1"
    )
    nm = make_market(
        venue=Venue.POLYMARKET, outcome=Outcome.NO, market_id="pm-no-1", event_id="evt-1"
    )

    def payload(touch, extras):
        lvls = [(touch, 200.0)] + list(extras)
        return {
            "asks": [{"price": repr(p), "size": repr(s)} for p, s in lvls],
            "bids": [{"price": repr(touch - 0.02), "size": "200.0"}],
            "timestamp": "1758206400000",
        }

    def shuffled(pl):
        return {**pl, "asks": pl["asks"][::-1], "bids": pl["bids"][::-1]}

    y1 = _PMA.normalize_order_book(payload(0.40, extra_yes), ym)
    n1 = _PMA.normalize_order_book(payload(0.40, extra_no), nm)
    y2 = _PMA.normalize_order_book(shuffled(payload(0.40, extra_yes)), ym)
    n2 = _PMA.normalize_order_book(shuffled(payload(0.40, extra_no)), nm)
    o1 = detect_bundle_arbitrage(
        BookView(ym, y1, "simulated"),
        BookView(nm, n1, "simulated"),
        config=_CONFIG,
        fee_model=_FEES,
        now=T0,
    )
    o2 = detect_bundle_arbitrage(
        BookView(ym, y2, "simulated"),
        BookView(nm, n2, "simulated"),
        config=_CONFIG,
        fee_model=_FEES,
        now=T0,
    )
    assert (o1 is None) == (o2 is None)
    if o1 is not None:
        assert _canon_opp(o1) == _canon_opp(o2)


@given(views=_cross_venue_views())
@settings(max_examples=50, deadline=None)
def test_detect_all_invariant_to_view_order(views):
    opps1, flags1 = detect_all(views, config=_CONFIG, fee_model=_FEES, now=T0)
    opps2, flags2 = detect_all(list(reversed(views)), config=_CONFIG, fee_model=_FEES, now=T0)
    assert sorted(map(_canon_opp, opps1)) == sorted(map(_canon_opp, opps2))
    assert flags1 == flags2


@st.composite
def _snapshot_seqs(draw):
    n = draw(st.integers(1, 4))
    snaps = []
    for i in range(n):
        at = T0 + timedelta(seconds=60 * i)
        yv, nv = polymarket_pair(
            yes_ask=draw(st.floats(0.2, 0.8, allow_nan=False, allow_infinity=False)),
            no_ask=draw(st.floats(0.2, 0.8, allow_nan=False, allow_infinity=False)),
            depth=draw(st.floats(60.0, 500.0, allow_nan=False, allow_infinity=False)),
            at=at,
        )
        snaps.append(
            SnapshotInput(
                label="simulated",
                venue=Venue.POLYMARKET,
                markets=[yv.market, nv.market],
                books=[yv.book, nv.book],
                captured_at=at,
            )
        )
    return snaps


@pytest.mark.slow
@given(snaps=_snapshot_seqs())
@settings(max_examples=25, deadline=None)
def test_replay_deterministic_for_identical_snapshot_sequences(snaps):
    r1 = _canon_report(ReplayEngine(bankroll=100.0).run(snaps))
    # input order must not matter: replay sorts chronologically
    r2 = _canon_report(ReplayEngine(bankroll=100.0).run(list(reversed(snaps))))
    r3 = _canon_report(ReplayEngine(bankroll=100.0).run(list(snaps)))
    assert r1 == r2 == r3
