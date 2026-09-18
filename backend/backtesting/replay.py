"""Chronological replay: detect -> size -> risk-gate -> paper-execute.

Snapshots are processed in timestamp order. At step t the engine sees
only books from snapshots with captured_at <= t, and the paper broker
fills against the latest book at or before (decision + latency) -- never
a future quote. This is the no-look-ahead guarantee, enforced structurally.

Honesty rules:
  * Snapshot labels (live/simulated) propagate into the report; a replay
    mixing labels is flagged, never silently blended.
  * With fewer than 30 executed opportunities the report flags
    SAMPLE_TOO_SMALL: Sharpe ratios, win rates, and significance claims
    are not meaningful.
  * Settlement payout ($1 per locked YES+NO pair) is credited only on
    paired fills. Unpaired residual legs are reported at cost with no
    P&L claimed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from backend.arbitrage.costs import FeeModel
from backend.arbitrage.opportunities import BookView, detect_all
from backend.arbitrage.settings import StrategyConfig
from backend.arbitrage.sizing import size_position
from backend.execution.paper import PaperBroker, TimelineKey
from backend.risk.gates import PortfolioState, RiskGate
from backend.schemas import Market, OrderBook, PaperTrade, Venue

MIN_SAMPLE_FOR_STATS = 30
SETTLEMENT_PAYOUT = 1.0


@dataclass
class SnapshotInput:
    label: str
    venue: Venue
    markets: list[Market]
    books: list[OrderBook]
    captured_at: datetime


def load_labeled_snapshot(path: str | Path) -> SnapshotInput:
    """Load a snapshot file, keeping its label and capture time."""
    payload = json.loads(Path(path).read_text())
    if payload.get("odds_snapshot_version") != 1:
        raise ValueError("unsupported snapshot version")
    venue = Venue(payload["venue"])
    captured_at = datetime.fromisoformat(payload["captured_at"])
    return SnapshotInput(
        label=payload["label"],
        venue=venue,
        markets=[Market(**m) for m in payload["markets"]],
        books=[OrderBook(**b) for b in payload["order_books"]],
        captured_at=captured_at,
    )


@dataclass
class OpportunityRow:
    opportunity_id: str
    strategy: str
    label: str
    detected_at: str
    sized_quantity: float
    decision: str
    expected_net: float
    realized_net: float | None
    trade_statuses: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class ReplayReport:
    n_snapshots: int
    labels: list[str]
    n_opportunities: int
    n_executed: int
    n_rejected: int
    trades_by_status: dict[str, int]
    total_expected_net: float
    total_realized_net: float
    hit_rate: float | None
    flags: list[str]
    rows: list[OpportunityRow]
    config_notes: dict

    def to_dict(self) -> dict:
        return {
            "n_snapshots": self.n_snapshots,
            "labels": self.labels,
            "n_opportunities": self.n_opportunities,
            "n_executed": self.n_executed,
            "n_rejected": self.n_rejected,
            "trades_by_status": self.trades_by_status,
            "total_expected_net": round(self.total_expected_net, 6),
            "total_realized_net": round(self.total_realized_net, 6),
            "hit_rate": self.hit_rate,
            "flags": self.flags,
            "rows": [r.__dict__ for r in self.rows],
            "config_notes": self.config_notes,
        }


class ReplayEngine:
    """Runs labeled snapshots through the full pipeline, in order."""

    def __init__(self, config: StrategyConfig | None = None,
                 bankroll: float = 100.0) -> None:
        self.config = config or StrategyConfig.load()
        self.fee_model = FeeModel()
        self.broker = PaperBroker(self.fee_model, self.config)
        self.gate = RiskGate(self.config)
        self.bankroll = bankroll

    def run(self, snapshots: list[SnapshotInput]) -> ReplayReport:
        snaps = sorted(snapshots, key=lambda s: s.captured_at)
        labels = sorted({s.label for s in snaps})
        flags: list[str] = []
        if len(labels) > 1:
            flags.append(f"MIXED_LABELS:{','.join(labels)}: replay blends "
                         f"live and simulated quotes; treat results accordingly")
        if all(s.label == "simulated" for s in snaps):
            flags.append("ALL_INPUT_SIMULATED: no live market data in this replay")

        timelines: dict[TimelineKey, list[OrderBook]] = {}
        markets: dict[TimelineKey, Market] = {}
        portfolio = PortfolioState()
        rows: list[OpportunityRow] = []
        trades_by_status: dict[str, int] = {}
        total_expected = 0.0
        total_realized = 0.0
        n_executed = 0
        n_rejected = 0
        wins = 0

        for snap in snaps:
            for book in snap.books:
                key: TimelineKey = (book.venue.value, book.market_id, book.outcome.value)
                timelines.setdefault(key, []).append(book)
            for m in snap.markets:
                key = (m.venue.value, m.market_id, m.outcome.value)
                markets[key] = m
            views = [BookView(market=m,
                              book=next(b for b in snap.books
                                        if (b.venue.value, b.market_id, b.outcome.value)
                                        == (m.venue.value, m.market_id, m.outcome.value)),
                              label=snap.label)
                     for m in snap.markets
                     if any((b.venue.value, b.market_id, b.outcome.value)
                            == (m.venue.value, m.market_id, m.outcome.value)
                            for b in snap.books)]
            opportunities, det_flags = detect_all(
                views, config=self.config, fee_model=self.fee_model,
                now=snap.captured_at)
            flags.extend(f"{snap.label}:{f}" for f in det_flags)

            for opp in opportunities:
                # Size: Kelly fraction on the opportunity's net edge.
                cost_per_contract = max(
                    0.01, 1.0 - opp.costs.raw_edge + opp.costs.trading_fees)
                sizing = size_position(
                    net_edge_per_contract=opp.costs.net_edge,
                    cost_per_contract=cost_per_contract,
                    liquidity=opp.liquidity,
                    bankroll=self.bankroll,
                    fraction=self.config.kelly.fraction,
                    max_position=self.config.risk.max_position_per_market,
                    # Locked arbitrage: p_win ~ 1 by construction; the
                    # remaining uncertainty is execution risk. This is an
                    # assumption (see configs/strategy.yaml), not a model.
                    p_win=self.config.kelly.arbitrage_p_win,
                    p_win_is_placeholder=False,
                )
                qty = sizing.quantity
                book_ages = []
                for leg in opp.legs:
                    key = (leg["venue"], leg["market_id"], leg["outcome"])
                    tl = timelines.get(key, [])
                    if tl:
                        ts = tl[-1].venue_timestamp or tl[-1].received_timestamp
                        book_ages.append((snap.captured_at - ts).total_seconds())
                gate = self.gate.evaluate(opp, qty, portfolio,
                                          book_ages_s=book_ages or None,
                                          now=snap.captured_at)
                if not gate.allow:
                    n_rejected += 1
                    rows.append(OpportunityRow(
                        opp.opportunity_id, opp.strategy, snap.label,
                        snap.captured_at.isoformat(), qty, "REJECTED",
                        expected_net=0.0, realized_net=None,
                        notes=[gate.reason]))
                    continue

                n_executed += 1
                expected = opp.costs.net_edge * qty
                total_expected += expected
                trades = self.broker.execute(
                    opp, qty, timelines, markets, decide_at=snap.captured_at)
                statuses = [t.status.value for t in trades]
                for s in statuses:
                    trades_by_status[s] = trades_by_status.get(s, 0) + 1
                realized, note = self._settle(opp.strategy, trades)
                total_realized += realized
                if realized > 0:
                    wins += 1
                # Update paper portfolio state (notional exposure).
                for leg in opp.legs:
                    key = (leg["venue"], leg["market_id"])
                    portfolio.positions[key] = portfolio.positions.get(key, 0.0) + qty
                    portfolio.venue_exposure[leg["venue"]] = \
                        portfolio.venue_exposure.get(leg["venue"], 0.0) + qty
                portfolio.daily_pnl += realized
                rows.append(OpportunityRow(
                    opp.opportunity_id, opp.strategy, snap.label,
                    snap.captured_at.isoformat(), qty, "PAPER_EXECUTE",
                    expected_net=round(expected, 6),
                    realized_net=round(realized, 6),
                    trade_statuses=statuses, notes=note))

        hit_rate = round(wins / n_executed, 4) if n_executed else None
        if n_executed < MIN_SAMPLE_FOR_STATS:
            flags.append(
                f"SAMPLE_TOO_SMALL: n_executed={n_executed} < {MIN_SAMPLE_FOR_STATS}; "
                f"Sharpe ratio, win rate, and significance claims are not meaningful")
        return ReplayReport(
            n_snapshots=len(snaps),
            labels=labels,
            n_opportunities=len(rows),
            n_executed=n_executed,
            n_rejected=n_rejected,
            trades_by_status=trades_by_status,
            total_expected_net=total_expected,
            total_realized_net=total_realized,
            hit_rate=hit_rate,
            flags=sorted(set(flags)),
            rows=rows,
            config_notes={
                "kelly_fraction": self.config.kelly.fraction,
                "min_net_edge": self.config.detection.min_net_edge,
                "bankroll": self.bankroll,
                "p_win_placeholder": self.config.kelly.probability_is_placeholder,
            },
        )

    @staticmethod
    def _settle(strategy: str, trades: list[PaperTrade]) -> tuple[float, list[str]]:
        """Realized P&L for one executed opportunity.

        Bundle/complement: leg cash flows plus $1 settlement payout per
        paired contract. Unpaired residual legs are carried at cost with no
        P&L claimed. Direct cross-venue (buy+sell): the signed leg cash
        flows already capture proceeds minus costs.
        """
        notes: list[str] = []
        filled = [t.filled_quantity for t in trades]
        paired = min(filled) if filled else 0.0
        residual = (max(filled) - paired) if filled else 0.0
        if residual > 0:
            notes.append(f"unpaired residual {residual:.2f} contracts carried "
                         f"at cost, no P&L claimed")
        leg_net = sum(t.net_pnl for t in trades)
        if strategy == "bundle_arbitrage" or (
                strategy == "cross_venue_arbitrage" and len(trades) == 2
                and {t.side for t in trades} == {"buy"}):
            return leg_net + paired * SETTLEMENT_PAYOUT, notes
        return leg_net, notes
