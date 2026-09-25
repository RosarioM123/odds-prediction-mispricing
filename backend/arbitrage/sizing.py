"""Position sizing: fractional Kelly with hard caps.

Kelly criterion for a bet with win probability p and net odds b
(profit per dollar staked):  f* = (b*p - q) / b, floored at 0.

For an arbitrage leg set, b = net_edge_per_contract / cost_per_contract.
The win probability for a *locked* arbitrage is close to 1 in theory, but
execution risk (stale quotes, partial fills, fee mis-estimation) is real,
so the engine uses the configured assumed probability -- currently 0.55
and explicitly labeled a placeholder until a calibrated model exists --
and then applies the configured Kelly fraction (0.25 | 0.50 | 1.00).

Quantity is then capped by available liquidity and max_position_per_market.
A non-positive Kelly fraction sizes to zero: the opportunity is not taken.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.errors import DataValidationError

KELLY_FRACTIONS = (0.25, 0.50, 1.00)

P_WIN_PLACEHOLDER_NOTE = "P_WIN_PLACEHOLDER: assumed edge probability not calibrated"
P_WIN_ASSUMPTION_NOTE = (
    "P_WIN_ASSUMPTION: locked-arbitrage execution-risk assumption, not a calibrated model"
)


def kelly_optimal_fraction(p_win: float, odds_b: float) -> float:
    """Full-Kelly fraction of bankroll. Floored at 0 (never bet).

    Raises:
        DataValidationError: if ``p_win`` is not in the open interval (0, 1).
    """
    if not 0.0 < p_win < 1.0:
        raise DataValidationError("p_win must be in (0, 1)")
    if odds_b <= 0:
        return 0.0
    q = 1.0 - p_win
    return max(0.0, (odds_b * p_win - q) / odds_b)


@dataclass
class SizingResult:
    quantity: float
    stake_dollars: float
    kelly_full: float
    fraction_used: float
    capped_by: str | None
    notes: list[str] = field(default_factory=list)


def size_position(
    *,
    net_edge_per_contract: float,
    cost_per_contract: float,
    liquidity: float,
    bankroll: float,
    fraction: float,
    max_position: float,
    p_win: float,
    p_win_status: str = "placeholder",
    p_win_n: int = 0,
    p_win_period: str = "",
) -> SizingResult:
    """Size one opportunity. Returns quantity 0 when Kelly says don't bet.

    ``p_win_status`` is one of "placeholder", "assumption" (locked
    arbitrage execution-risk assumption), "preliminary", or "calibrated"
    (see backend/arbitrage/calibration.py); the matching note propagates
    into the result.

    Raises:
        DataValidationError: if ``fraction`` is not a supported Kelly
            fraction, if ``p_win`` is not in (0, 1) when Kelly sizing
            is reached, or if ``p_win_status`` is not a known status.
    """
    notes: list[str] = []
    if fraction not in KELLY_FRACTIONS:
        raise DataValidationError(f"fraction must be one of {KELLY_FRACTIONS}")
    if p_win_status == "placeholder":
        notes.append(P_WIN_PLACEHOLDER_NOTE)
    elif p_win_status == "assumption":
        notes.append(P_WIN_ASSUMPTION_NOTE)
    elif p_win_status in ("preliminary", "calibrated"):
        tag = "P_WIN_PRELIMINARY" if p_win_status == "preliminary" else "P_WIN_CALIBRATED"
        notes.append(
            f"{tag}: p_win fitted from {p_win_n} paper executions on live "
            f"snapshots ({p_win_period})"
        )
    else:
        raise DataValidationError(f"unknown p_win_status {p_win_status!r}")
    if bankroll <= 0 or cost_per_contract <= 0:
        return SizingResult(0.0, 0.0, 0.0, fraction, "no_bankroll_or_cost", notes)

    odds_b = net_edge_per_contract / cost_per_contract
    f_star = kelly_optimal_fraction(p_win, odds_b)
    if f_star <= 0:
        notes.append("kelly_fraction_non_positive: edge does not justify a bet")
        return SizingResult(0.0, 0.0, round(f_star, 6), fraction, "kelly_zero", notes)

    stake = bankroll * f_star * fraction
    quantity = stake / cost_per_contract
    capped_by: str | None = None
    if quantity > liquidity:
        quantity = liquidity
        capped_by = "liquidity"
    if quantity > max_position:
        quantity = max_position
        capped_by = "max_position_per_market" if capped_by is None else capped_by + "+max_position"
    quantity = max(0.0, quantity)
    return SizingResult(
        quantity=round(quantity, 6),
        stake_dollars=round(quantity * cost_per_contract, 6),
        kelly_full=round(f_star, 6),
        fraction_used=fraction,
        capped_by=capped_by,
        notes=notes,
    )
