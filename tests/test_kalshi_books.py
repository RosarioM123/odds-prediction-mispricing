"""Kalshi order-book semantics regression tests.

ALL INPUTS SIMULATED: every book below is a hand-built fixture mirroring the
official envelope shapes (https://docs.kalshi.com/openapi.yaml,
GET /markets/{ticker}/orderbook). No live Kalshi data is used, and no real
order is ever submitted (paper-trading-only engine).

Resolved 2026-09-18: the public orderbook endpoint "returns yes bids and no
bids only (no asks are returned)". Each side's ladder therefore normalizes to
BIDS, and asks are the documented complement: "a bid for yes at price X is
equivalent to an ask for no at price (100-X) ... with identical contract
sizes" (a yes order and a no order whose prices sum to $1 form a trade).
"""

import pytest

import backend.markets.kalshi as kal
from backend.markets import KalshiAdapter
from backend.schemas import MarketStatus, Outcome

MARKET = {
    "ticker": "FED-25DEC-CUT",
    "event_ticker": "FED-25DEC",
    "title": "Will the Fed cut rates in December?",
    "yes_sub_title": "Yes",
    "no_sub_title": "No",
    "status": "active",
    "yes_bid": 47,
    "yes_ask": 52,
    "expiration_time": "2025-12-31T23:59:00Z",
}

# Classic envelope, integer cents -- the legacy shape the adapter accepts.
CLASSIC_BOOK = {
    "orderbook": {
        "yes": [[52, 100], [53, 200]],
        "no": [[48, 150], [47, 250]],
    }
}

# Fixed-point envelope as the official spec shows it: dollar/count values are
# decimal STRINGS, e.g. ["0.1500", "100.00"].
FP_BOOK_STRINGS = {
    "orderbook_fp": {
        "yes_dollars": [["0.1500", "100.00"], ["0.1400", "50.00"]],
        "no_dollars": [["0.8600", "75.00"]],
    }
}

# Official docs example shape: a single yes bid at 7c.
DOCS_EXAMPLE_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.07", "100.00"]],
        "no_dollars": [],
    }
}

# What the demo host actually returned on 2026-09-18 (no liquidity): empty
# ladders. Books may be legitimately empty; that must not raise.
EMPTY_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [],
        "no_dollars": [],
    }
}


def make_adapter() -> KalshiAdapter:
    return KalshiAdapter(env="demo")


def yes_market():
    return make_adapter()._markets_from_kalshi(MARKET)[0]


def no_market():
    return make_adapter()._markets_from_kalshi(MARKET)[1]


def test_yes_ladder_normalizes_to_bids_best_first():
    """Regression: the yes ladder IS the bid side (official semantics)."""
    book = make_adapter().normalize_order_book(CLASSIC_BOOK, yes_market())
    assert book.outcome == Outcome.YES
    assert [level.price for level in book.bids] == pytest.approx([0.53, 0.52])
    assert [level.size for level in book.bids] == pytest.approx([200, 100])


def test_no_ladder_normalizes_to_bids_best_first():
    book = make_adapter().normalize_order_book(CLASSIC_BOOK, no_market())
    assert book.outcome == Outcome.NO
    assert [level.price for level in book.bids] == pytest.approx([0.48, 0.47])
    assert [level.size for level in book.bids] == pytest.approx([150, 250])


def test_asks_are_opposite_side_complement():
    """YES ask = 1 - NO bid; NO ask = 1 - YES bid (documented equivalence)."""
    yes_book = make_adapter().normalize_order_book(CLASSIC_BOOK, yes_market())
    assert [level.price for level in yes_book.asks] == pytest.approx([0.52, 0.53])
    assert [level.size for level in yes_book.asks] == pytest.approx([150, 250])

    no_book = make_adapter().normalize_order_book(CLASSIC_BOOK, no_market())
    assert [level.price for level in no_book.asks] == pytest.approx([0.47, 0.48])
    assert [level.size for level in no_book.asks] == pytest.approx([200, 100])


def test_docs_example_yes_bid_7c_implies_no_ask_93c():
    """The official spec's example: yes bid 7c == no ask 93c, same size."""
    yes_book = make_adapter().normalize_order_book(DOCS_EXAMPLE_BOOK, yes_market())
    assert [level.price for level in yes_book.bids] == pytest.approx([0.07])
    assert yes_book.asks == []  # no NO bids to derive from

    no_book = make_adapter().normalize_order_book(DOCS_EXAMPLE_BOOK, no_market())
    assert no_book.bids == []
    assert [level.price for level in no_book.asks] == pytest.approx([0.93])
    assert [level.size for level in no_book.asks] == pytest.approx([100.0])


def test_fp_string_values_parse():
    """Official spec encodes fp levels as decimal strings."""
    book = make_adapter().normalize_order_book(FP_BOOK_STRINGS, yes_market())
    assert [level.price for level in book.bids] == pytest.approx([0.15, 0.14])
    assert [level.size for level in book.bids] == pytest.approx([100.0, 50.0])
    # YES ask derives from the single NO bid at 0.86 -> 0.14
    assert [level.price for level in book.asks] == pytest.approx([0.14])
    assert [level.size for level in book.asks] == pytest.approx([75.0])


def test_empty_book_is_graceful():
    """Empty ladders (observed live on demo) -> empty book, no error."""
    for market in (yes_market(), no_market()):
        book = make_adapter().normalize_order_book(EMPTY_BOOK, market)
        assert book.bids == []
        assert book.asks == []
        assert book.best_bid is None
        assert book.best_ask is None
        assert book.spread is None
        assert book.mid_price is None


def test_spread_and_mid_now_available():
    """With real bids (plus derived asks), spread/mid work for Kalshi."""
    book = make_adapter().normalize_order_book(CLASSIC_BOOK, yes_market())
    assert book.spread == pytest.approx(0.52 - 0.53)
    assert book.mid_price == pytest.approx((0.53 + 0.52) / 2)


def test_bundle_edge_uses_correct_side_prices():
    """YES ask + NO ask from the same envelope gives the bundle raw edge."""
    yes_book = make_adapter().normalize_order_book(CLASSIC_BOOK, yes_market())
    no_book = make_adapter().normalize_order_book(CLASSIC_BOOK, no_market())
    raw_edge = 1.0 - (yes_book.best_ask + no_book.best_ask)
    assert raw_edge == pytest.approx(0.01)


def test_fetch_order_book_end_to_end(monkeypatch):
    """fetch_order_book -> normalize path carries the bid semantics."""

    def fake_get(url, params=None, **kwargs):
        assert url.endswith("/markets/FED-25DEC-CUT/orderbook")
        return FP_BOOK_STRINGS

    monkeypatch.setattr(kal, "get_json", fake_get)
    book = make_adapter().fetch_order_book(yes_market())
    assert [level.price for level in book.bids] == pytest.approx([0.15, 0.14])


def test_yes_market_status_open():
    assert yes_market().status == MarketStatus.OPEN
