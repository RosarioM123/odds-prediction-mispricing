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
    r = size_position(
        net_edge_per_contract=0.08,
        cost_per_contract=0.9,
        liquidity=500.0,
        bankroll=100.0,
        fraction=fraction,
        max_position=500.0,
        p_win=0.99,
        p_win_status="assumption",
    )
    assert r.fraction_used == fraction
    assert r.quantity > 0
    # Higher fraction -> larger size, monotonically.
    assert r.stake_dollars == pytest.approx(r.quantity * 0.9)


def test_size_position_liquidity_cap():
    r = size_position(
        net_edge_per_contract=0.08,
        cost_per_contract=0.9,
        liquidity=10.0,
        bankroll=100000.0,
        fraction=1.0,
        max_position=500.0,
        p_win=0.99,
        p_win_status="assumption",
    )
    assert r.quantity == pytest.approx(10.0)
    assert r.capped_by == "liquidity"


def test_size_position_max_position_cap():
    r = size_position(
        net_edge_per_contract=0.08,
        cost_per_contract=0.9,
        liquidity=10000.0,
        bankroll=100000.0,
        fraction=1.0,
        max_position=500.0,
        p_win=0.99,
        p_win_status="assumption",
    )
    assert r.quantity == pytest.approx(500.0)
    assert "max_position" in r.capped_by


def test_size_position_no_edge_sizes_zero():
    r = size_position(
        net_edge_per_contract=-0.01,
        cost_per_contract=0.9,
        liquidity=500.0,
        bankroll=100.0,
        fraction=0.5,
        max_position=500.0,
        p_win=0.55,
    )
    assert r.quantity == 0.0
    assert r.capped_by == "kelly_zero"


def test_size_position_invalid_fraction_raises():
    with pytest.raises(ValueError):
        size_position(
            net_edge_per_contract=0.08,
            cost_per_contract=0.9,
            liquidity=500.0,
            bankroll=100.0,
            fraction=0.75,
            max_position=500.0,
            p_win=0.99,
        )


def test_size_position_placeholder_p_win_flagged():
    r = size_position(
        net_edge_per_contract=0.08,
        cost_per_contract=0.9,
        liquidity=500.0,
        bankroll=100.0,
        fraction=0.5,
        max_position=500.0,
        p_win=0.55,
        p_win_status="placeholder",
    )
    assert any("P_WIN_PLACEHOLDER" in n for n in r.notes)


def _sizing_kwargs(**overrides):
    kw = {
        "net_edge_per_contract": 0.08,
        "cost_per_contract": 0.9,
        "liquidity": 500.0,
        "bankroll": 100.0,
        "fraction": 0.5,
        "max_position": 500.0,
        "p_win": 0.99,
    }
    kw.update(overrides)
    return kw


def test_size_position_assumption_status_flagged():
    r = size_position(**_sizing_kwargs(p_win_status="assumption"))
    assert any("P_WIN_ASSUMPTION" in n for n in r.notes)


def test_size_position_preliminary_status_carries_provenance():
    r = size_position(
        **_sizing_kwargs(
            p_win_status="preliminary", p_win_n=12, p_win_period="2026-09-25 to 2026-09-25"
        )
    )
    assert any("P_WIN_PRELIMINARY" in n and "12" in n for n in r.notes)


def test_size_position_calibrated_status_carries_provenance():
    r = size_position(
        **_sizing_kwargs(
            p_win_status="calibrated", p_win_n=400, p_win_period="2026-01-01 to 2026-06-01"
        )
    )
    assert any("P_WIN_CALIBRATED" in n and "400" in n for n in r.notes)


def test_size_position_unknown_status_rejected():
    import pytest

    from backend.errors import DataValidationError

    with pytest.raises(DataValidationError):
        size_position(**_sizing_kwargs(p_win_status="bogus"))
