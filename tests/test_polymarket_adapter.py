"""Tests for the Polymarket adapter. Fixtures mirror verified API shapes."""
import pytest

import backend.markets.polymarket as pm
from backend.markets.polymarket import PolymarketAdapter
from backend.schemas import MarketStatus, Outcome, Venue

GAMMA_MARKET = {
    "id": "559651",
    "question": "Xi Jinping out before 2027?",
    "conditionId": "0xa467b14d51f01b957109d9cbb1d6c124fab2a089d52ed8f471d23c2812e743b7",
    "endDate": "2027-01-01T04:59:00Z",
    "outcomes": '["Yes", "No"]',
    "clobTokenIds": '["32338220190071351435772801779725302244575775216413325951443816017994629993401",'
                    ' "25659310674993675562345759665114759892400026242514633218387667107987341231962"]',
    "active": True,
    "closed": False,
    "orderPriceMinTickSize": 0.001,
    "orderMinSize": 5,
    "takerBaseFee": 1000,
    "acceptingOrders": True,
}

# CLOB /book returns levels worst-first.
CLOB_BOOK = {
    "market": "0xa467",
    "asset_id": "32338220190071351435772801779725302244575775216413325951443816017994629993401",
    "timestamp": "1789690457621",
    "hash": "abc123",
    "bids": [
        {"price": "0.040", "size": "100.0"},
        {"price": "0.041", "size": "200.0"},
        {"price": "0.042", "size": "50.0"},
    ],
    "asks": [
        {"price": "0.050", "size": "80.0"},
        {"price": "0.044", "size": "120.0"},
        {"price": "0.043", "size": "60.0"},
    ],
}


def make_adapter() -> PolymarketAdapter:
    return PolymarketAdapter()


def test_gamma_market_splits_into_yes_and_no():
    adapter = make_adapter()
    markets = adapter._markets_from_gamma(GAMMA_MARKET)
    assert len(markets) == 2
    yes, no = markets
    assert yes.outcome == Outcome.YES
    assert no.outcome == Outcome.NO
    assert yes.market_id == "32338220190071351435772801779725302244575775216413325951443816017994629993401"
    assert no.market_id == "25659310674993675562345759665114759892400026242514633218387667107987341231962"
    assert yes.venue == Venue.POLYMARKET
    assert yes.event_id == GAMMA_MARKET["conditionId"]
    assert yes.question == GAMMA_MARKET["question"]
    assert yes.status == MarketStatus.OPEN
    assert yes.tick_size == pytest.approx(0.001)
    assert yes.taker_fee_rate == pytest.approx(0.1)  # 1000 bps


def test_gamma_market_without_tokens_is_skipped():
    adapter = make_adapter()
    assert adapter._markets_from_gamma({"id": "1", "question": "?"}) == []


def test_book_levels_reversed_to_best_first():
    adapter = make_adapter()
    market = adapter._markets_from_gamma(GAMMA_MARKET)[0]
    book = adapter.normalize_order_book(CLOB_BOOK, market)
    assert [l.price for l in book.bids] == [0.042, 0.041, 0.040]
    assert [l.price for l in book.asks] == [0.043, 0.044, 0.050]
    assert book.best_bid == pytest.approx(0.042)
    assert book.best_ask == pytest.approx(0.043)
    assert book.venue_timestamp is not None
    assert book.venue_timestamp.year == 2026


def test_empty_book_is_valid():
    adapter = make_adapter()
    market = adapter._markets_from_gamma(GAMMA_MARKET)[0]
    book = adapter.normalize_order_book({"bids": [], "asks": []}, market)
    assert book.best_bid is None and book.best_ask is None


def test_resolve_taker_fee_rate_uses_live_endpoint(monkeypatch):
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append(url)
        return {"base_fee": 0}

    monkeypatch.setattr(pm, "get_json", fake_get)
    adapter = make_adapter()
    market = adapter._markets_from_gamma(GAMMA_MARKET)[0]
    assert adapter.resolve_taker_fee_rate(market) == 0.0
    # Second call served from cache: no extra HTTP.
    assert adapter.resolve_taker_fee_rate(market) == 0.0
    assert len(calls) == 1


def test_resolve_taker_fee_rate_falls_back_on_error(monkeypatch):
    def boom(url, params=None, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(pm, "get_json", boom)
    adapter = make_adapter()
    market = adapter._markets_from_gamma(GAMMA_MARKET)[0]
    assert adapter.resolve_taker_fee_rate(market) == pytest.approx(0.1)


def test_fetch_markets_paginates(monkeypatch):
    pages = [[GAMMA_MARKET], []]

    def fake_get(url, params=None, **kwargs):
        return pages.pop(0)

    monkeypatch.setattr(pm, "get_json", fake_get)
    markets = make_adapter().fetch_markets(limit=10)
    assert len(markets) == 2
