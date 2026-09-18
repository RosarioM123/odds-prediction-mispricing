"""Tests for the Kalshi adapter and snapshot I/O. Fixtures mirror verified shapes."""
import pytest

import backend.markets.kalshi as kal
from backend.markets import (
    LABEL_LIVE,
    LABEL_SIMULATED,
    KalshiAdapter,
    load_snapshot,
    save_snapshot,
)
from backend.schemas import Market, MarketStatus, OrderBook, OrderBookLevel, Outcome, Venue

KALSHI_MARKET = {
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

# Classic envelope: integer cents.
CLASSIC_BOOK = {
    "orderbook": {
        "yes": [[52, 100], [53, 200]],
        "no": [[48, 150], [47, 250]],
    }
}

# Floating-point dollars envelope observed on the demo host.
FP_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [[0.52, 100.0], [0.53, 200.0]],
        "no_dollars": [[0.48, 150.0], [0.47, 250.0]],
    }
}


def make_adapter() -> KalshiAdapter:
    return KalshiAdapter(env="demo")


def test_kalshi_market_splits_into_two_sides():
    markets = make_adapter()._markets_from_kalshi(KALSHI_MARKET)
    assert len(markets) == 2
    yes, no = markets
    assert (yes.outcome, no.outcome) == (Outcome.YES, Outcome.NO)
    assert yes.market_id == "FED-25DEC-CUT" == no.market_id
    assert yes.event_id == "FED-25DEC"
    assert yes.status == MarketStatus.OPEN
    assert yes.tick_size == pytest.approx(0.01)
    assert yes.taker_fee_rate is None  # formula-based


def test_non_open_markets_skipped():
    raw = {**KALSHI_MARKET, "status": "settled"}
    assert make_adapter()._markets_from_kalshi(raw) == []


def _yes_market() -> Market:
    return make_adapter()._markets_from_kalshi(KALSHI_MARKET)[0]


def test_classic_envelope_cents_to_dollars():
    book = make_adapter().normalize_order_book(CLASSIC_BOOK, _yes_market())
    assert [l.price for l in book.asks] == pytest.approx([0.52, 0.53])
    assert book.asks[0].size == pytest.approx(100)
    # Bids are unavailable from the Kalshi envelope (documented limitation).
    assert book.bids == []


def test_fp_envelope_dollars():
    book = make_adapter().normalize_order_book(FP_BOOK, _yes_market())
    assert [l.price for l in book.asks] == pytest.approx([0.52, 0.53])


def test_unknown_envelope_raises():
    with pytest.raises(ValueError, match="unrecognized"):
        make_adapter().normalize_order_book({"weird": {}}, _yes_market())


def test_no_side_book():
    adapter = make_adapter()
    no_market = adapter._markets_from_kalshi(KALSHI_MARKET)[1]
    book = adapter.normalize_order_book(CLASSIC_BOOK, no_market)
    assert [l.price for l in book.asks] == pytest.approx([0.47, 0.48])
    assert book.bids == []


def test_fetch_markets_follows_cursor(monkeypatch):
    pages = [
        {"markets": [KALSHI_MARKET], "cursor": "abc"},
        {"markets": [], "cursor": ""},
    ]

    def fake_get(url, params=None, **kwargs):
        return pages.pop(0)

    monkeypatch.setattr(kal, "get_json", fake_get)
    markets = make_adapter().fetch_markets(limit=10)
    assert len(markets) == 2


def _sample_book() -> OrderBook:
    return OrderBook(
        market_id="FED-25DEC-CUT",
        venue=Venue.KALSHI,
        outcome=Outcome.YES,
        bids=[OrderBookLevel(price=0.51, size=10)],
        asks=[OrderBookLevel(price=0.52, size=20)],
    )


def test_snapshot_round_trip(tmp_path):
    market = _yes_market()
    path = save_snapshot(tmp_path / "snap.json", Venue.KALSHI,
                         [market], [_sample_book()], label=LABEL_SIMULATED)
    label, venue, markets, books = load_snapshot(path)
    assert label == LABEL_SIMULATED
    assert venue == Venue.KALSHI
    assert markets[0].market_id == "FED-25DEC-CUT"
    assert books[0].asks[0].price == pytest.approx(0.52)


def test_snapshot_rejects_bad_label(tmp_path):
    with pytest.raises(ValueError, match="label"):
        save_snapshot(tmp_path / "x.json", Venue.KALSHI, [], [], label="real")


def test_snapshot_rejects_bad_version(tmp_path):
    p = tmp_path / "x.json"
    p.write_text('{"odds_snapshot_version": 999}')
    with pytest.raises(ValueError, match="version"):
        load_snapshot(p)
