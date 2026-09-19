"""Tests for deterministic risk gates."""

import pytest

from backend.arbitrage.costs import FeeModel
from backend.arbitrage.opportunities import detect_bundle_arbitrage
from backend.arbitrage.settings import StrategyConfig
from backend.risk.gates import PortfolioState, RiskGate
from tests.fixtures import T0, polymarket_pair


@pytest.fixture()
def config():
    return StrategyConfig.load()


@pytest.fixture()
def gate(config):
    return RiskGate(config)


@pytest.fixture()
def opportunity(config):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=200.0)
    opp = detect_bundle_arbitrage(yv, nv, config=config, fee_model=FeeModel(), now=T0)
    assert opp is not None
    return opp


def test_gate_allows_clean_trade(gate, opportunity):
    res = gate.evaluate(opportunity, 50.0, PortfolioState(), now=T0)
    assert res.allow
    assert opportunity.decision == "PAPER_EXECUTE"


def test_gate_rejects_zero_quantity(gate, opportunity):
    res = gate.evaluate(opportunity, 0.0, PortfolioState(), now=T0)
    assert not res.allow
    assert opportunity.decision == "REJECTED"


def test_gate_rejects_over_max_position(gate, opportunity, config):
    res = gate.evaluate(
        opportunity, config.risk.max_position_per_market + 1, PortfolioState(), now=T0
    )
    assert not res.allow
    assert "max_position_per_market" in res.reason


def test_gate_rejects_portfolio_exposure_breach(gate, opportunity, config):
    pf = PortfolioState(venue_exposure={"polymarket": config.risk.max_portfolio_exposure})
    res = gate.evaluate(opportunity, 10.0, pf, now=T0)
    assert not res.allow


def test_gate_rejects_daily_loss_limit(gate, opportunity, config):
    pf = PortfolioState(daily_pnl=-config.risk.max_daily_loss)
    res = gate.evaluate(opportunity, 10.0, pf, now=T0)
    assert not res.allow
    assert "daily loss" in res.reason


def test_gate_rejects_stale_books(gate, opportunity):
    res = gate.evaluate(opportunity, 10.0, PortfolioState(), book_ages_s=[30.0], now=T0)
    assert not res.allow
    assert "stale" in res.reason or "latency" in res.reason


def test_gate_rejects_weak_match_confidence(gate, opportunity, config):
    opportunity.strategy = "cross_venue_arbitrage"
    opportunity.match_confidence = 0.5
    res = gate.evaluate(opportunity, 10.0, PortfolioState(), now=T0)
    assert not res.allow
    assert "match_confidence" in res.reason
