"""Targeted tests for defensive branches and error paths (coverage gate).

Each test here exists to pin a specific hard-to-reach branch: HTTP retry
logic, adapter error mapping, risk-gate rejections, and fee-model defensive
raises. Nothing here touches a live network.
"""

import dataclasses
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from backend import config as config_mod
from backend.arbitrage.costs import FeeModel
from backend.arbitrage.opportunities import (
    _IdFactory,
    detect_bundle_arbitrage,
    detect_cross_venue_direct,
    deterministic_match,
)
from backend.arbitrage.settings import StrategyConfig
from backend.errors import (
    AdapterParseError,
    ConfigurationError,
    DataValidationError,
    RateLimitError,
    VenueError,
)
from backend.markets import http as http_mod
from backend.markets.base import MarketDataAdapter
from backend.markets.kalshi import KalshiAdapter
from backend.markets.polymarket import PolymarketAdapter, _parse_json_array
from backend.risk.gates import PortfolioState, RiskGate
from backend.schemas import LatencyBreakdown, MarketStatus, Outcome, Venue
from tests.fixtures import T0, make_book, make_market, polymarket_pair

# ---------------------------------------------------------------------------
# backend/markets/http.py
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text="", bad_json=False, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self._bad_json = bad_json
        self.headers = headers or {}

    def json(self):
        if self._bad_json:
            raise ValueError("not json")
        return self._payload


class _FakeClient:
    """Stand-in for httpx.Client with a scripted get() sequence."""

    def __init__(self, script):
        self._script = list(script)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params, timeout))
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _patch_client(monkeypatch, script):
    fake = _FakeClient(script)
    monkeypatch.setattr(http_mod, "_client", lambda: fake)
    monkeypatch.setattr(http_mod, "_sleep", lambda s: None)
    return fake


def test_get_json_returns_dict_payload(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(200, {"a": 1})])
    assert http_mod.get_json("https://x.example/m") == {"a": 1}


def test_get_json_returns_list_payload(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(200, [1, 2])])
    assert http_mod.get_json("https://x.example/m") == [1, 2]


def test_get_json_rate_limit_raises(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(429, text="slow down")])
    with pytest.raises(RateLimitError, match="429"):
        http_mod.get_json("https://x.example/m", retries=0)


def test_get_json_http_error_raises_venue_error(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(500, text="boom")])
    with pytest.raises(VenueError, match="HTTP 500"):
        http_mod.get_json("https://x.example/m", retries=0)


def test_get_json_bad_json_raises_venue_error(monkeypatch):
    _patch_client(monkeypatch, [_FakeResponse(200, bad_json=True)])
    with pytest.raises(VenueError, match="non-JSON"):
        http_mod.get_json("https://x.example/m")


def test_get_json_retries_transient_failure_then_succeeds(monkeypatch):
    sleeps = []
    monkeypatch.setattr(http_mod, "_sleep", sleeps.append)
    fake = _FakeClient([httpx.TimeoutException("boom"), _FakeResponse(200, {"ok": True})])
    monkeypatch.setattr(http_mod, "_client", lambda: fake)
    assert http_mod.get_json("https://x.example/m") == {"ok": True}
    assert len(fake.calls) == 2
    assert sleeps == [0.5]


def test_get_json_exhausts_retries(monkeypatch):
    sleeps = []
    monkeypatch.setattr(http_mod, "_sleep", sleeps.append)
    script = [httpx.TimeoutException("down")] * 3
    monkeypatch.setattr(http_mod, "_client", lambda: _FakeClient(script))
    with pytest.raises(VenueError, match="failed after 3 attempts"):
        http_mod.get_json("https://x.example/m", retries=2)
    # No sleep after the final attempt: a scan loop must not stall.
    assert sleeps == [0.5, 1.0]


def test_client_honors_proxy_and_cert_env(monkeypatch):
    captured = {}

    class _RecordingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(http_mod.httpx, "Client", _RecordingClient)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("SSL_CERT_FILE", "/tmp/ca.pem")
    http_mod._client()
    assert captured["proxy"] == "http://proxy:8080"
    assert captured["verify"] == "/tmp/ca.pem"
    assert captured["headers"]["User-Agent"] == http_mod.USER_AGENT


def test_client_defaults_without_env(monkeypatch):
    captured = {}

    class _RecordingClient:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(http_mod.httpx, "Client", _RecordingClient)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("SSL_CERT_FILE", raising=False)
    http_mod._client()
    assert captured["proxy"] is None
    assert captured["verify"] is True


# ---------------------------------------------------------------------------
# backend/markets/base.py
# ---------------------------------------------------------------------------


class _ConcreteAdapter(MarketDataAdapter):
    venue = Venue.POLYMARKET

    def fetch_markets(self, *, status="open", limit=100):
        return []

    def fetch_order_book(self, market):
        raise AssertionError("not implemented in test double")

    def normalize_market(self, raw):
        raise AssertionError("not implemented in test double")

    def normalize_order_book(self, raw, market, venue_ts=None):
        raise AssertionError("not implemented in test double")


def test_abstract_adapter_methods_raise_not_implemented():
    adapter = _ConcreteAdapter()
    market = make_market()
    with pytest.raises(NotImplementedError):
        MarketDataAdapter.fetch_markets(adapter)
    with pytest.raises(NotImplementedError):
        MarketDataAdapter.fetch_order_book(adapter, market)
    with pytest.raises(NotImplementedError):
        MarketDataAdapter.normalize_market(adapter, {})
    with pytest.raises(NotImplementedError):
        MarketDataAdapter.normalize_order_book(adapter, {}, market)


def test_default_resolve_taker_fee_rate():
    adapter = _ConcreteAdapter()
    assert adapter.resolve_taker_fee_rate(make_market(taker_fee_rate=0.07)) == 0.07
    assert adapter.resolve_taker_fee_rate(make_market(taker_fee_rate=None)) is None


# ---------------------------------------------------------------------------
# backend/config.py
# ---------------------------------------------------------------------------


def test_load_yaml_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    with pytest.raises(ConfigurationError, match="Missing config file"):
        config_mod.load_yaml("nope.yaml")


def test_load_yaml_empty_file_returns_empty_dict(monkeypatch, tmp_path):
    (tmp_path / "empty.yaml").write_text("")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    assert config_mod.load_yaml("empty.yaml") == {}


def test_load_yaml_non_mapping_raises(monkeypatch, tmp_path):
    (tmp_path / "lst.yaml").write_text("- a\n- b\n")
    monkeypatch.setattr(config_mod, "CONFIG_DIR", tmp_path)
    with pytest.raises(DataValidationError, match="must contain a YAML mapping"):
        config_mod.load_yaml("lst.yaml")


# ---------------------------------------------------------------------------
# backend/schemas.py latency properties
# ---------------------------------------------------------------------------


def test_latency_breakdown_individual_properties():
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    lb = LatencyBreakdown(
        market_data_timestamp=t0,
        detection_timestamp=t0 + timedelta(milliseconds=100),
        decision_timestamp=t0 + timedelta(milliseconds=150),
        simulated_execution_timestamp=t0 + timedelta(milliseconds=400),
    )
    assert lb.data_latency_ms == pytest.approx(100.0)
    assert lb.processing_latency_ms == pytest.approx(50.0)
    assert lb.execution_latency_ms == pytest.approx(250.0)


# ---------------------------------------------------------------------------
# backend/risk/gates.py rejection branches
# ---------------------------------------------------------------------------


@pytest.fixture()
def gate_config():
    return StrategyConfig.load()


@pytest.fixture()
def gate(gate_config):
    return RiskGate(gate_config)


@pytest.fixture()
def bundle_opp(gate_config):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=200.0)
    opp = detect_bundle_arbitrage(yv, nv, config=gate_config, fee_model=FeeModel(), now=T0)
    assert opp is not None
    return opp


def test_gate_rejects_negative_net_edge(gate, bundle_opp):
    bundle_opp.costs.net_edge = -0.01
    res = gate.evaluate(bundle_opp, 10.0, PortfolioState(), now=T0)
    assert not res.allow
    assert "net_edge" in res.reason


def test_gate_rejects_venue_exposure_breach(gate, bundle_opp, gate_config):
    pf = PortfolioState(venue_exposure={"polymarket": gate_config.risk.max_venue_exposure})
    res = gate.evaluate(bundle_opp, 10.0, pf, now=T0)
    assert not res.allow
    assert "max_venue_exposure" in res.reason


def test_gate_rejects_stale_book_below_latency_threshold(gate, bundle_opp, gate_config):
    relaxed = dataclasses.replace(gate_config.risk, max_acceptable_latency_ms=60_000.0)
    relaxed_config = dataclasses.replace(gate_config, risk=relaxed)
    relaxed_gate = RiskGate(relaxed_config)
    res = relaxed_gate.evaluate(bundle_opp, 10.0, PortfolioState(), book_ages_s=[20.0], now=T0)
    assert not res.allow
    assert "stale" in res.reason


def test_gate_passes_match_confidence_check(gate, bundle_opp):
    bundle_opp.strategy = "cross_venue_arbitrage"
    bundle_opp.match_confidence = 0.95
    res = gate.evaluate(bundle_opp, 10.0, PortfolioState(), now=T0)
    assert res.allow


# ---------------------------------------------------------------------------
# backend/arbitrage/costs.py fee-model branches
# ---------------------------------------------------------------------------


def test_resolve_taker_rate_kalshi_returns_none():
    market = make_market(venue=Venue.KALSHI, taker_fee_rate=None)
    rate, notes = FeeModel().resolve_taker_rate(market)
    assert rate is None
    assert notes == []


def test_taker_fee_unknown_venue_raises():
    market = make_market()
    market.venue = "unknown-venue"  # defensive branch: Venue has only 2 members
    with pytest.raises(DataValidationError, match="unknown venue"):
        FeeModel().taker_fee(market, 10.0, 0.5)


def test_maker_fee_kalshi_uses_series_formula():
    market = make_market(venue=Venue.KALSHI)
    quote = FeeModel().maker_fee(market, 10.0, 0.5)
    assert quote.fee_dollars > 0
    assert quote.notes == ["KALSHI_MAKER_SERIES_ONLY"]


def test_maker_fee_unknown_venue_raises():
    market = make_market()
    market.venue = "unknown-venue"  # defensive branch: Venue has only 2 members
    with pytest.raises(DataValidationError, match="unknown venue"):
        FeeModel().maker_fee(market, 10.0, 0.5)


# ---------------------------------------------------------------------------
# backend/arbitrage/opportunities.py detection branches
# ---------------------------------------------------------------------------


def test_id_factory_without_seed_uses_timestamp():
    factory = _IdFactory(None)
    first = factory.next("bundle", "pm")
    second = factory.next("bundle", "pm")
    assert first.startswith("bundle-pm-")
    assert first != second


def test_deterministic_match_expiration_bonus_within_24h():
    a = make_market(expiration=T0)
    b = make_market(venue=Venue.KALSHI, expiration=T0 + timedelta(hours=2))
    result = deterministic_match(a, b)
    assert result.matched
    assert any("within 24h" in reason for reason in result.reasons)


def test_deterministic_match_expiration_penalty_beyond_24h():
    a = make_market(expiration=T0)
    b = make_market(venue=Venue.KALSHI, expiration=T0 + timedelta(hours=30))
    result = deterministic_match(a, b)
    assert result.matched
    assert any(">24h" in reason for reason in result.reasons)


def test_bundle_rejects_mismatched_event_ids(gate_config):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=200.0)
    nv.market.event_id = "different-event"
    assert detect_bundle_arbitrage(yv, nv, config=gate_config, fee_model=FeeModel(), now=T0) is None


def test_bundle_rejects_stale_books(gate_config):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=200.0)
    late = T0 + timedelta(seconds=3600)
    assert (
        detect_bundle_arbitrage(yv, nv, config=gate_config, fee_model=FeeModel(), now=late) is None
    )


def test_bundle_rejects_nonpositive_edge(gate_config):
    yv, nv = polymarket_pair(yes_ask=0.55, no_ask=0.55, depth=200.0)
    assert detect_bundle_arbitrage(yv, nv, config=gate_config, fee_model=FeeModel(), now=T0) is None


def test_bundle_rejects_insufficient_liquidity(gate_config):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=1.0)
    assert detect_bundle_arbitrage(yv, nv, config=gate_config, fee_model=FeeModel(), now=T0) is None


def test_cross_venue_direct_rejects_unmatched_markets(gate_config):
    from backend.arbitrage.opportunities import BookView

    buy_market = make_market(market_id="pm-a", question="Will it rain?")
    sell_market = make_market(market_id="k-b", venue=Venue.KALSHI, question="Will it snow?")
    buy_view = BookView(
        market=buy_market, book=make_book(buy_market, asks=[(0.40, 100.0)]), label="simulated"
    )
    sell_view = BookView(
        market=sell_market,
        book=make_book(sell_market, bids=[(0.50, 100.0)]),
        label="simulated",
    )
    assert (
        detect_cross_venue_direct(
            buy_view, sell_view, config=gate_config, fee_model=FeeModel(), now=T0
        )
        is None
    )


def test_cross_venue_direct_rejects_stale_sell_book(gate_config):
    buy_market = make_market(market_id="pm-a", question="Same question?")
    sell_market = make_market(market_id="k-b", venue=Venue.KALSHI, question="Same question?")
    buy = make_book(buy_market, asks=[(0.40, 100.0)])
    sell = make_book(sell_market, bids=[(0.60, 100.0)], at=T0 - timedelta(seconds=3600))
    from backend.arbitrage.opportunities import BookView

    buy_view = BookView(market=buy_market, book=buy, label="simulated")
    sell_view = BookView(market=sell_market, book=sell, label="simulated")
    assert (
        detect_cross_venue_direct(
            buy_view, sell_view, config=gate_config, fee_model=FeeModel(), now=T0
        )
        is None
    )


# ---------------------------------------------------------------------------
# backend/markets/kalshi.py adapter branches
# ---------------------------------------------------------------------------


def test_kalshi_adapter_rejects_bad_env():
    with pytest.raises(DataValidationError, match="env must be"):
        KalshiAdapter(env="staging")


def test_kalshi_fetch_markets_403_prod_hint(monkeypatch):
    def boom(url, params=None, **kwargs):
        raise VenueError("HTTP 403 from https://api.elections.kalshi.com: forbidden")

    monkeypatch.setattr("backend.markets.kalshi.get_json", boom)
    adapter = KalshiAdapter(env="prod")
    with pytest.raises(VenueError, match="production rejected"):
        adapter.fetch_markets(limit=1)


def test_kalshi_fetch_markets_reraises_non_403(monkeypatch):
    def boom(url, params=None, **kwargs):
        raise VenueError("HTTP 500 from kalshi")

    monkeypatch.setattr("backend.markets.kalshi.get_json", boom)
    adapter = KalshiAdapter(env="demo")
    with pytest.raises(VenueError, match="HTTP 500"):
        adapter.fetch_markets(limit=1)


def test_kalshi_fetch_markets_stops_on_non_dict_page(monkeypatch):
    monkeypatch.setattr("backend.markets.kalshi.get_json", lambda *a, **k: [1, 2, 3])
    adapter = KalshiAdapter(env="demo")
    assert adapter.fetch_markets(limit=5) == []


def test_kalshi_fetch_order_book_rejects_non_dict(monkeypatch):
    monkeypatch.setattr("backend.markets.kalshi.get_json", lambda *a, **k: [1, 2])
    adapter = KalshiAdapter(env="demo")
    with pytest.raises(AdapterParseError, match="unexpected orderbook"):
        adapter.fetch_order_book(make_market(venue=Venue.KALSHI))


def test_kalshi_parse_dt_returns_none_on_garbage():
    assert KalshiAdapter._parse_dt("not-a-date") is None
    assert KalshiAdapter._parse_dt("") is None


def test_kalshi_resolve_taker_fee_rate_is_none():
    adapter = KalshiAdapter(env="demo")
    assert adapter.resolve_taker_fee_rate(make_market(venue=Venue.KALSHI)) is None


# ---------------------------------------------------------------------------
# backend/markets/polymarket.py adapter branches
# ---------------------------------------------------------------------------


def test_parse_json_array_rejects_garbage_string():
    assert _parse_json_array("definitely not json") == []
    assert _parse_json_array(["a"]) == ["a"]


def test_polymarket_fetch_markets_non_open_status(monkeypatch):
    seen = {}

    def fake_get(url, params=None, **kwargs):
        seen.update(params or {})
        return []

    monkeypatch.setattr("backend.markets.polymarket.get_json", fake_get)
    adapter = PolymarketAdapter()
    assert adapter.fetch_markets(status="closed", limit=5) == []
    assert "active" not in seen


def test_polymarket_fetch_markets_paginates(monkeypatch):
    def gamma_raw(token):
        return {"clobTokenIds": f'["{token}-yes", "{token}-no"]', "outcomes": '["Yes", "No"]'}

    pages = [[gamma_raw("t1")], [gamma_raw("t2")], []]
    monkeypatch.setattr("backend.markets.polymarket.get_json", lambda *a, **k: pages.pop(0))
    adapter = PolymarketAdapter()
    markets = adapter.fetch_markets(limit=10)
    assert len(markets) == 4
    assert {m.outcome for m in markets} == {Outcome.YES, Outcome.NO}


def test_polymarket_fetch_order_book_rejects_non_dict(monkeypatch):
    monkeypatch.setattr("backend.markets.polymarket.get_json", lambda *a, **k: [1])
    adapter = PolymarketAdapter()
    with pytest.raises(AdapterParseError, match="unexpected /book"):
        adapter.fetch_order_book(make_market())


def test_polymarket_parse_venue_ts_rejects_garbage():
    assert PolymarketAdapter._parse_venue_ts("not-a-number") is None


def test_polymarket_normalize_market_unknown_status():
    adapter = PolymarketAdapter()
    market = adapter.normalize_market({"_token_id": "tok-1", "_outcome": "Yes"})
    assert market.status == MarketStatus.UNKNOWN


def test_polymarket_fee_rate_cache_hit(monkeypatch):
    calls = []

    def fake_get(url, params=None, **kwargs):
        calls.append(url)
        return {"base_fee": "250"}

    monkeypatch.setattr("backend.markets.polymarket.get_json", fake_get)
    adapter = PolymarketAdapter()
    market = make_market()
    first = adapter.resolve_taker_fee_rate(market)
    second = adapter.resolve_taker_fee_rate(market)
    assert first == pytest.approx(0.025)
    assert second == first
    assert len(calls) == 1


def test_polymarket_fee_rate_falls_back_on_error(monkeypatch):
    def boom(url, params=None, **kwargs):
        raise VenueError("throttled")

    monkeypatch.setattr("backend.markets.polymarket.get_json", boom)
    adapter = PolymarketAdapter()
    market = make_market(taker_fee_rate=0.05)
    assert adapter.resolve_taker_fee_rate(market) == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# Round 2: pagination exits, fee-cache paths, complement detection, snapshots
# ---------------------------------------------------------------------------


def _kalshi_raw(ticker="T"):
    return {"ticker": ticker, "status": "open", "title": "Q?"}


def test_kalshi_fetch_markets_exits_on_page_limit(monkeypatch):
    pages = [{"markets": [_kalshi_raw(f"T{i}")], "cursor": f"c{i}"} for i in range(12)]
    monkeypatch.setattr("backend.markets.kalshi.get_json", lambda *a, **k: pages.pop(0))
    adapter = KalshiAdapter(env="demo")
    markets = adapter.fetch_markets(limit=1000)
    assert len(markets) == 20  # 10 pages x 2 outcomes, loop exits via condition


def test_kalshi_fetch_markets_inner_break_on_limit(monkeypatch):
    monkeypatch.setattr(
        "backend.markets.kalshi.get_json",
        lambda *a, **k: {"markets": [_kalshi_raw("T1"), _kalshi_raw("T2")]},
    )
    adapter = KalshiAdapter(env="demo")
    assert len(adapter.fetch_markets(limit=2)) == 2


def test_kalshi_normalize_market_accepts_outcome_member():
    adapter = KalshiAdapter(env="demo")
    market = adapter.normalize_market({**_kalshi_raw("T"), "_outcome": Outcome.YES})
    assert market.outcome == Outcome.YES


def test_polymarket_fetch_markets_exits_on_page_limit(monkeypatch):
    def gamma_raw(token):
        return {"clobTokenIds": f'["{token}-yes", "{token}-no"]', "outcomes": '["Yes", "No"]'}

    pages = [[gamma_raw(f"t{i}")] for i in range(12)]
    monkeypatch.setattr("backend.markets.polymarket.get_json", lambda *a, **k: pages.pop(0))
    adapter = PolymarketAdapter()
    assert len(adapter.fetch_markets(limit=1000)) == 20


def test_polymarket_fetch_markets_inner_break_on_limit(monkeypatch):
    def gamma_raw(token):
        return {"clobTokenIds": f'["{token}-yes", "{token}-no"]', "outcomes": '["Yes", "No"]'}

    monkeypatch.setattr("backend.markets.polymarket.get_json", lambda *a, **k: [gamma_raw("t1")])
    adapter = PolymarketAdapter()
    assert len(adapter.fetch_markets(limit=2)) == 2


def test_polymarket_fetch_order_book_success(monkeypatch):
    monkeypatch.setattr(
        "backend.markets.polymarket.get_json",
        lambda *a, **k: {"bids": [], "asks": [{"price": "0.55", "size": "10"}], "timestamp": "123"},
    )
    adapter = PolymarketAdapter()
    book = adapter.fetch_order_book(make_market())
    assert book.best_ask == pytest.approx(0.55)


def test_polymarket_normalize_market_closed_status():
    adapter = PolymarketAdapter()
    market = adapter.normalize_market({"_token_id": "tok-1", "_outcome": "Yes", "closed": True})
    assert market.status == MarketStatus.CLOSED


def test_polymarket_normalize_market_bad_end_date():
    adapter = PolymarketAdapter()
    market = adapter.normalize_market(
        {"_token_id": "tok-1", "_outcome": "Yes", "endDate": "not-a-date"}
    )
    assert market.expiration is None


def test_polymarket_fee_rate_non_dict_response_falls_back(monkeypatch):
    monkeypatch.setattr("backend.markets.polymarket.get_json", lambda *a, **k: [1, 2])
    adapter = PolymarketAdapter()
    market = make_market(taker_fee_rate=0.05)
    assert adapter.resolve_taker_fee_rate(market) == pytest.approx(0.05)


def test_polymarket_fee_rate_none_when_unknown_and_failing(monkeypatch):
    def boom(url, params=None, **kwargs):
        raise VenueError("down")

    monkeypatch.setattr("backend.markets.polymarket.get_json", boom)
    adapter = PolymarketAdapter()
    market = make_market(taker_fee_rate=None)
    assert adapter.resolve_taker_fee_rate(market) is None


def _complement_views(yes_ask=0.45, no_ask=0.45, at=T0):
    from backend.arbitrage.opportunities import BookView

    yes_market = make_market(market_id="pm-yes", question="Same?")
    no_market = make_market(market_id="k-no", venue=Venue.KALSHI, question="Same?")
    yv = BookView(
        market=yes_market,
        book=make_book(yes_market, asks=[(yes_ask, 200.0)], at=at),
        label="simulated",
    )
    nv = BookView(
        market=no_market,
        book=make_book(no_market, asks=[(no_ask, 200.0)], at=at),
        label="simulated",
    )
    return yv, nv


def test_complement_rejects_stale_books(gate_config):
    from backend.arbitrage.opportunities import detect_cross_venue_complement

    yv, nv = _complement_views(at=T0 - timedelta(seconds=3600))
    assert (
        detect_cross_venue_complement(yv, nv, config=gate_config, fee_model=FeeModel(), now=T0)
        is None
    )


def test_complement_rejects_empty_asks(gate_config):
    from backend.arbitrage.opportunities import BookView, detect_cross_venue_complement

    yes_market = make_market(market_id="pm-yes", question="Same?")
    no_market = make_market(market_id="k-no", venue=Venue.KALSHI, question="Same?")
    yv = BookView(market=yes_market, book=make_book(yes_market, asks=[]), label="simulated")
    nv = BookView(
        market=no_market, book=make_book(no_market, asks=[(0.45, 200.0)]), label="simulated"
    )
    assert (
        detect_cross_venue_complement(yv, nv, config=gate_config, fee_model=FeeModel(), now=T0)
        is None
    )


def test_complement_rejects_nonpositive_edge(gate_config):
    from backend.arbitrage.opportunities import detect_cross_venue_complement

    yv, nv = _complement_views(yes_ask=0.55, no_ask=0.55)
    assert (
        detect_cross_venue_complement(yv, nv, config=gate_config, fee_model=FeeModel(), now=T0)
        is None
    )


def test_load_labeled_snapshot_rejects_bad_version(tmp_path):
    from backend.backtesting.replay import load_labeled_snapshot

    path = tmp_path / "snap.json"
    path.write_text('{"odds_snapshot_version": 2, "venue": "polymarket"}')
    with pytest.raises(DataValidationError, match="unsupported snapshot version"):
        load_labeled_snapshot(path)


def test_strategy_config_rejects_non_numeric_value():
    with pytest.raises(DataValidationError, match="must be numeric"):
        StrategyConfig.from_dict({"detection": {"min_net_edge": "not-a-number"}})


def test_complement_rejects_insufficient_liquidity(gate_config):
    from backend.arbitrage.opportunities import BookView, detect_cross_venue_complement

    yes_market = make_market(market_id="pm-yes", question="Same?")
    no_market = make_market(market_id="k-no", venue=Venue.KALSHI, question="Same?")
    yv = BookView(
        market=yes_market, book=make_book(yes_market, asks=[(0.45, 1.0)]), label="simulated"
    )
    nv = BookView(
        market=no_market, book=make_book(no_market, asks=[(0.45, 1.0)]), label="simulated"
    )
    assert (
        detect_cross_venue_complement(yv, nv, config=gate_config, fee_model=FeeModel(), now=T0)
        is None
    )
