"""Tests for shared live polling helpers in backend.markets.live.

ALL INPUTS SYNTHETIC: markets are hand-built with fabricated raw
payloads. No test here touches the network.
"""

from backend.markets.live import select_liquid, volume_24h_usd
from tests.fixtures import make_market


def _market(volume_raw):
    m = make_market()
    object.__setattr__(m, "raw", volume_raw)
    return m


def test_volume_24h_polymarket_field():
    m = _market({"volume24hr": 1234.5})
    assert volume_24h_usd(m) == 1234.5


def test_volume_24h_kalshi_field():
    m = _market({"volume_24h_fp": "67.89"})
    assert volume_24h_usd(m) == 67.89


def test_volume_24h_missing_is_zero():
    assert volume_24h_usd(make_market()) == 0.0


def test_volume_24h_unparseable_is_zero():
    assert volume_24h_usd(_market({"volume24hr": "n/a"})) == 0.0


def test_select_liquid_filters_and_sorts():
    low = _market({"volume24hr": 10.0})
    high = _market({"volume24hr": 500.0})
    mid = _market({"volume_24h_fp": "100.0"})
    out = select_liquid([low, mid, high], 50.0)
    assert [volume_24h_usd(m) for m in out] == [500.0, 100.0]


def test_select_liquid_zero_disables_filter():
    markets = [make_market(), make_market()]
    assert select_liquid(markets, 0.0) == markets
