"""Paper execution simulator. No real orders are ever placed.

For each opportunity leg the broker simulates a fill against the order
book as it stood at (decision time + execution latency), using only book
snapshots timestamped at or before that moment -- never future books.

Per-leg outcomes:
  FILLED            whole quantity filled at the walked VWAP
  PARTIALLY_FILLED  book depth covered only part of the quantity
  MISSED            book too stale at fill time, or zero depth
  EXPIRED           market expired before fill, or no quote existed yet

Per-leg P&L is cash-flow accounting: gross = -(fill_price * filled),
net = gross - fees. Settlement payout ($1 per contract for a locked
bundle) is credited by the replay engine, not here.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.arbitrage.costs import FeeModel, walk_book
from backend.arbitrage.settings import StrategyConfig
from backend.schemas import (
    Market,
    Opportunity,
    OrderBook,
    PaperTrade,
    PaperTradeStatus,
    utcnow,
)

TimelineKey = tuple[str, str, str]  # (venue, market_id, outcome)


def latest_book_at(timeline: list[OrderBook], at: datetime) -> OrderBook | None:
    """Latest book with timestamp <= ``at``. None if the timeline starts later.

    This is the no-look-ahead primitive: the broker can never see a quote
    from the future.
    """
    best: OrderBook | None = None
    for book in timeline:
        ts = book.venue_timestamp or book.received_timestamp
        if ts <= at and (best is None or ts > (best.venue_timestamp or best.received_timestamp)):
            best = book
    return best


@dataclass
class PaperBroker:
    fee_model: FeeModel
    config: StrategyConfig

    @property
    def execution_latency(self) -> timedelta:
        return timedelta(milliseconds=self.config.latency.execution_ms)

    def execute(self, opportunity: Opportunity, quantity: float,
              timelines: dict[TimelineKey, list[OrderBook]],
              markets: dict[TimelineKey, Market],
              decide_at: datetime | None = None) -> list[PaperTrade]:
        """Simulate fills for every leg. Returns one PaperTrade per leg."""
        decide_at = decide_at or utcnow()
        fill_at = decide_at + self.execution_latency
        trades: list[PaperTrade] = []
        for i, leg in enumerate(opportunity.legs):
            key: TimelineKey = (leg["venue"], leg["market_id"], leg["outcome"])
            market = markets.get(key)
            book = latest_book_at(timelines.get(key, []), fill_at)
            trades.append(self._fill_leg(
                opportunity=opportunity, leg_index=i, leg=leg,
                quantity=quantity, market=market, book=book,
                decide_at=decide_at, fill_at=fill_at))
        return trades

    def _fill_leg(self, *, opportunity: Opportunity, leg_index: int,
                  leg: dict, quantity: float, market: Market | None,
                  book: OrderBook | None, decide_at: datetime,
                  fill_at: datetime) -> PaperTrade:
        trade_id = f"{opportunity.opportunity_id}-leg{leg_index}"
        base = dict(
            trade_id=trade_id,
            opportunity_id=opportunity.opportunity_id,
            timestamp=fill_at,
            venue=leg["venue"],
            market_id=leg["market_id"],
            side=leg["side"],
            quantity=quantity,
            expected_price=leg["expected_price"],
            latency_ms=round((fill_at - decide_at).total_seconds() * 1000, 1),
        )
        if market is None or book is None:
            return PaperTrade(status=PaperTradeStatus.EXPIRED,
                              simulated_fill_price=leg["expected_price"], **base)
        if market.expiration is not None and fill_at >= market.expiration:
            return PaperTrade(status=PaperTradeStatus.EXPIRED,
                              simulated_fill_price=leg["expected_price"], **base)
        book_ts = book.venue_timestamp or book.received_timestamp
        if (fill_at - book_ts).total_seconds() > self.config.detection.max_book_age_seconds:
            return PaperTrade(status=PaperTradeStatus.MISSED,
                              simulated_fill_price=leg["expected_price"], **base)

        side = "buy" if leg["side"] == "buy" else "sell"
        walked = walk_book(book, side, quantity)
        if walked.filled <= 0:
            return PaperTrade(status=PaperTradeStatus.MISSED,
                              simulated_fill_price=leg["expected_price"], **base)
        fee = self.fee_model.taker_fee(market, walked.filled, walked.vwap or 0.0)
        # Cash-flow accounting signed by side: buys are outflows, sells inflows.
        signed = -1.0 if side == "buy" else 1.0
        gross = signed * walked.vwap * walked.filled
        status = (PaperTradeStatus.FILLED if walked.shortfall <= 0
                  else PaperTradeStatus.PARTIALLY_FILLED)
        return PaperTrade(
            simulated_fill_price=walked.vwap,
            filled_quantity=walked.filled,
            fees=round(fee.fee_dollars, 6),
            slippage=round((walked.vwap - leg["expected_price"])
                           * walked.filled, 6),
            gross_pnl=round(gross, 6),
            net_pnl=round(gross - fee.fee_dollars, 6),
            status=status,
            **base,
        )
