"""Deterministic risk gates. Every simulated trade passes through here.

The gate is pure rules, no judgment: position, exposure, loss, latency,
and match-confidence limits from configs/strategy.yaml. It sets the
opportunity decision to PAPER_EXECUTE or REJECTED with the reason kept in
the opportunity explanation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from backend.arbitrage.settings import StrategyConfig
from backend.schemas import Opportunity, utcnow


@dataclass
class PortfolioState:
    """Running paper portfolio state, updated by the replay engine."""

    positions: dict[tuple[str, str], float] = field(default_factory=dict)
    venue_exposure: dict[str, float] = field(default_factory=dict)
    daily_pnl: float = 0.0

    @property
    def total_exposure(self) -> float:
        """Sum of per-venue exposures in contracts.

        Raises:
            None.
        """
        return round(sum(self.venue_exposure.values()), 6)


@dataclass
class GateResult:
    allow: bool
    reason: str
    checks: list[str] = field(default_factory=list)


class RiskGate:
    def __init__(self, config: StrategyConfig) -> None:
        """Create a gate bound to the given strategy configuration.

        Raises:
            None.
        """
        self.config = config

    def evaluate(
        self,
        opportunity: Opportunity,
        quantity: float,
        portfolio: PortfolioState,
        book_ages_s: list[float] | None = None,
        now: datetime | None = None,
    ) -> GateResult:
        """Apply every gate. Mutates opportunity.decision.

        Rejections are returned as ``GateResult(allow=False)`` with the
        reason recorded in the opportunity explanation -- never raised.

        Raises:
            None.
        """
        now = now or utcnow()
        risk = self.config.risk
        det = self.config.detection
        checks: list[str] = []

        def _reject(reason: str) -> GateResult:
            opportunity.decision = "REJECTED"
            expl = dict(opportunity.explanation)
            expl["risk_rejection_reason"] = reason
            flags = list(expl.get("flags", [])) + ["RISK_REJECTED"]
            expl["flags"] = sorted(set(flags))
            opportunity.explanation = expl
            return GateResult(False, reason, checks)

        net_edge = opportunity.costs.net_edge
        if net_edge < det.min_net_edge:
            return _reject(f"net_edge {net_edge:.4f} < min {det.min_net_edge}")
        checks.append(f"net_edge {net_edge:.4f} >= {det.min_net_edge}")

        if quantity <= 0:
            return _reject("sized quantity is zero")
        checks.append(f"quantity {quantity:.2f} > 0")

        if quantity > risk.max_position_per_market:
            return _reject(
                f"quantity {quantity:.2f} > max_position_per_market {risk.max_position_per_market}"
            )
        checks.append("position limit ok")

        notional = quantity * 1.0  # $1 max payout per contract
        if portfolio.total_exposure + notional > risk.max_portfolio_exposure:
            return _reject("would breach max_portfolio_exposure")
        checks.append("portfolio exposure ok")

        for leg in opportunity.legs:
            venue = leg["venue"]
            if portfolio.venue_exposure.get(venue, 0.0) + notional > risk.max_venue_exposure:
                return _reject(f"would breach max_venue_exposure on {venue}")
        checks.append("venue exposure ok")

        if portfolio.daily_pnl <= -risk.max_daily_loss:
            return _reject(f"daily loss limit hit ({portfolio.daily_pnl:.2f})")
        checks.append("daily loss ok")

        if book_ages_s:
            oldest = max(book_ages_s)
            if oldest * 1000 > risk.max_acceptable_latency_ms:
                return _reject(f"book age {oldest * 1000:.0f}ms > max acceptable latency")
            if oldest > det.max_book_age_seconds:
                return _reject(f"book stale: {oldest:.1f}s > {det.max_book_age_seconds}s")
            checks.append(f"book age {oldest:.2f}s ok")

        if opportunity.strategy == "cross_venue_arbitrage":
            conf = opportunity.match_confidence or 0.0
            if conf < self.config.cross_venue.min_match_confidence:
                return _reject(f"match_confidence {conf:.2f} below minimum")
            checks.append(f"match_confidence {conf:.2f} ok")

        opportunity.decision = "PAPER_EXECUTE"
        return GateResult(True, "all gates passed", checks)
