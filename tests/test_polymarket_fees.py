"""Tests for the Polymarket fee interpretation (base_fee in basis points).

ALL_INPUT_SIMULATED: every market, order book, and fee-endpoint response
below is synthetic. No live venue data is fetched or asserted on.

Verified 2026-09-18 against official Polymarket documentation:
  * https://docs.polymarket.com/api-spec/clob-openapi.yaml
    (FeeRate.base_fee: "Base fee in basis points")
  * https://docs.polymarket.com/trading/fees
    (fee tables: e.g. crypto rate 0.07, 100 shares @ $0.50 -> $1.75)
"""
import pytest

import backend.markets.polymarket as pm
from backend.arbitrage.costs import (
    POLYMARKET_FEE_PROVISIONAL,
    POLYMARKET_FEE_VERIFIED,
    FeeModel,
    build_cost_breakdown,
    latency_adjustment,
)
from backend.arbitrage.opportunities import detect_bundle_arbitrage
from backend.arbitrage.settings import StrategyConfig
from backend.markets.polymarket import PolymarketAdapter
from tests.fixtures import T0, make_market, polymarket_pair

GAMMA_MARKET_BPS = {
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
    "takerBaseFee": 700,  # crypto category: 700 bps = 0.07 (official docs)
    "acceptingOrders": True,
}


def fake_fee_endpoint(base_fee):
    def fake_get(url, params=None, **kwargs):
        assert url.endswith("/fee-rate")
        return {"base_fee": base_fee}

    return fake_get


# -- base_fee is basis points: /10000 conversion --------------------------


def test_base_fee_bps_converts_to_decimal_rate(monkeypatch):
    monkeypatch.setattr(pm, "get_json", fake_fee_endpoint(700))
    adapter = PolymarketAdapter()
    market = adapter._markets_from_gamma(GAMMA_MARKET_BPS)[0]
    assert adapter.resolve_taker_fee_rate(market) == pytest.approx(0.07)
    # Cached: a second resolve issues no further HTTP.
    assert adapter.resolve_taker_fee_rate(market) == pytest.approx(0.07)


def test_base_fee_zero_means_fee_free(monkeypatch):
    monkeypatch.setattr(pm, "get_json", fake_fee_endpoint(0))
    adapter = PolymarketAdapter()
    market = adapter._markets_from_gamma(GAMMA_MARKET_BPS)[0]
    assert adapter.resolve_taker_fee_rate(market) == 0.0


def test_base_fee_example_from_official_spec(monkeypatch):
    # The official CLOB spec's FeeRate example is base_fee: 30 -> 0.003.
    monkeypatch.setattr(pm, "get_json", fake_fee_endpoint(30))
    adapter = PolymarketAdapter()
    market = adapter._markets_from_gamma(GAMMA_MARKET_BPS)[0]
    assert adapter.resolve_taker_fee_rate(market) == pytest.approx(0.003)


def test_gamma_taker_base_fee_is_basis_points():
    adapter = PolymarketAdapter()
    yes, no = adapter._markets_from_gamma(GAMMA_MARKET_BPS)
    assert yes.taker_fee_rate == pytest.approx(0.07)  # 700 bps
    assert no.taker_fee_rate == pytest.approx(0.07)  # same market, both tokens


# -- fee math matches the official docs fee tables ------------------------


def test_fee_matches_official_docs_crypto_table():
    # Official fee page (crypto, feeRate 0.07): 100 shares @ $0.50 -> $1.75.
    fees = FeeModel()
    market = make_market(taker_fee_rate=700 / 10000.0)
    q = fees.taker_fee(market, contracts=100.0, price=0.50)
    assert q.fee_dollars == pytest.approx(1.75)
    assert POLYMARKET_FEE_VERIFIED in q.notes


def test_fee_matches_official_docs_at_10_cents():
    # Official fee page: 100 shares @ $0.10 -> $0.63 (100*0.07*0.1*0.9).
    fees = FeeModel()
    market = make_market(taker_fee_rate=0.07)
    q = fees.taker_fee(market, contracts=100.0, price=0.10)
    assert q.fee_dollars == pytest.approx(0.63)


def test_fee_symmetric_around_50_percent():
    # Official docs: a trade at 30c incurs the same fee as at 70c.
    fees = FeeModel()
    market = make_market(taker_fee_rate=0.05)
    low = fees.taker_fee(market, contracts=100.0, price=0.30)
    high = fees.taker_fee(market, contracts=100.0, price=0.70)
    assert low.fee_dollars == pytest.approx(high.fee_dollars)


# -- fee boundaries in cost math ------------------------------------------


def test_zero_rate_zero_fee_at_any_size():
    fees = FeeModel()
    market = make_market(taker_fee_rate=0.0)
    q = fees.taker_fee(market, contracts=10000.0, price=0.50)
    assert q.fee_dollars == 0.0


def test_dust_fee_rounds_to_zero():
    fees = FeeModel()
    market = make_market(taker_fee_rate=0.05)
    q = fees.taker_fee(market, contracts=0.001, price=0.5)
    assert q.fee_dollars == 0.0


def test_fee_applies_to_taker_fill_regardless_of_side():
    # Official docs: fees apply to taker fills at match time; makers pay 0.
    # The cost model charges the taker fee per executed leg, not per
    # direction, so a buy leg and a sell leg at the same price pay the same.
    fees = FeeModel()
    buy_market = make_market(taker_fee_rate=0.05, market_id="pm-buy")
    sell_market = make_market(taker_fee_rate=0.05, market_id="pm-sell")
    fee_buy = fees.taker_fee(buy_market, contracts=100.0, price=0.45)
    fee_sell = fees.taker_fee(sell_market, contracts=100.0, price=0.45)
    assert fee_buy.fee_dollars > 0.0
    assert fee_sell.fee_dollars == pytest.approx(fee_buy.fee_dollars)


def test_maker_pays_zero():
    fees = FeeModel()
    market = make_market(taker_fee_rate=0.07)
    q = fees.maker_fee(market, contracts=100.0, price=0.50)
    assert q.fee_dollars == 0.0


def test_fee_larger_than_edge_rejects_trade():
    # Touch-level edge exists (1.0 - 0.495 - 0.495 = $0.01/contract) but the
    # taker fee on both legs (~$0.025/contract) turns it negative.
    config = StrategyConfig.load()
    fees = FeeModel()
    yv, nv = polymarket_pair(yes_ask=0.495, no_ask=0.495, depth=100.0,
                             taker_fee_rate=0.05)
    assert yv.label == "simulated" and nv.label == "simulated"  # ALL_INPUT_SIMULATED

    fee_y = fees.taker_fee(yv.market, 100.0, 0.495)
    fee_n = fees.taker_fee(nv.market, 100.0, 0.495)
    fees_total = fee_y.fee_dollars + fee_n.fee_dollars
    lat, _ = latency_adjustment(config.total_latency_seconds,
                                config.latency.adverse_drift_per_second,
                                config.latency.drift_is_placeholder)
    cb = build_cost_breakdown(raw_edge_per_contract=0.01, size=100.0,
                              fees_total=fees_total, slippage_total=0.0,
                              spread_info_per_contract=0.0,
                              latency_total=lat * 100.0)
    assert cb.net_edge < config.detection.min_net_edge
    # The engine must reject the trade, not paper-execute a loser.
    assert detect_bundle_arbitrage(yv, nv, config=config, fee_model=fees,
                                   now=T0) is None


def test_verified_label_replaces_provisional():
    # The old label name stays importable (backwards compat) and resolves
    # to the verified label now attached to Polymarket fee quotes.
    assert POLYMARKET_FEE_PROVISIONAL == POLYMARKET_FEE_VERIFIED
    fees = FeeModel()
    market = make_market(taker_fee_rate=0.05)
    q = fees.taker_fee(market, contracts=100.0, price=0.45)
    assert POLYMARKET_FEE_VERIFIED in q.notes
