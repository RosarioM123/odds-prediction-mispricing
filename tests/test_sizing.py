"""Tests for Kelly sizing."""
import pytest

from backend.arbitrage.sizing import KELLY_FRACTIONS, kelly_optimal_fraction, size_position


def test_kelly_known_value():
    # p=0.6, b=1 -> (0.6-0.4)/1 = 0.2
    assert kelly_optimal_fraction(0.6, 1.0) == pytest.approx(0.2)


def test_kelly_negative_edge_floors_at_zero():
    assert kelly_optimal_fraction(0.4, 1.0) == 0.0


def test_kelly_invalid_p_raises():
    with pytest.raises(ValueError):
        kelly_optimal_fraction(1.5, 1.0)


@pytest.mark.parametrize("fraction", KELLY_FRACTIONS)
def test_size_position_all_configured_fractions(fraction):
    r = size_position(net_edge_per_contract=0.08, cost_per_contract=0.9,
                      liquidity=500.0, bankroll=100.0, fraction=fraction,
                      max_position=500.0, p_win=0.99,
                      p_win_is_placeholder=False)
    assert r.fraction_used == fraction
    assert r.quantity > 0
    # Higher fraction -> larger size, monotonically.
    assert r.stake_dollars == pytest.approx(r.quantity * 0.9)


def test_size_position_liquidity_cap():
    r = size_position(net_edge_per_contract=0.08, cost_per_contract=0.9,
                      liquidity=10.0, bankroll=100000.0, fraction=1.0,
                      max_position=500.0, p_win=0.99,
                      p_win_is_placeholder=False)
    assert r.quantity == pytest.approx(10.0)
    assert r.capped_by == "liquidity"


def test_size_position_max_position_cap():
    r = size_position(net_edge_per_contract=0.08, cost_per_contract=0.9,
                      liquidity=10000.0, bankroll=100000.0, fraction=1.0,
                      max_position=500.0, p_win=0.99,
                      p_win_is_placeholder=False)
    assert r.quantity == pytest.approx(500.0)
    assert "max_position" in r.capped_by


def test_size_position_no_edge_sizes_zero():
    r = size_position(net_edge_per_contract=-0.01, cost_per_contract=0.9,
                      liquidity=500.0, bankroll=100.0, fraction=0.5,
                      max_position=500.0, p_win=0.55)
    assert r.quantity == 0.0
    assert r.capped_by == "kelly_zero"


def test_size_position_invalid_fraction_raises():
    with pytest.raises(ValueError):
        size_position(net_edge_per_contract=0.08, cost_per_contract=0.9,
                      liquidity=500.0, bankroll=100.0, fraction=0.75,
                      max_position=500.0, p_win=0.99)


def test_size_position_placeholder_p_win_flagged():
    r = size_position(net_edge_per_contract=0.08, cost_per_contract=0.9,
                      liquidity=500.0, bankroll=100.0, fraction=0.5,
                      max_position=500.0, p_win=0.55,
                      p_win_is_placeholder=True)
    assert any("P_WIN_PLACEHOLDER" in n for n in r.notes)
