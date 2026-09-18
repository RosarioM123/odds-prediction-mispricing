"""Transaction-cost model: fees, spread, slippage (VWAP), latency.

Cost waterfall per opportunity (per contract, in dollars):

    raw_edge            executable edge at the touch (best quotes, zero size)
  - trading_fees        venue taker fees for the executed size
  - slippage            VWAP(size) minus touch, from walking the full book
  - latency_adjustment  expected adverse drift over the latency budget
  = net_edge            what the edge is actually worth

``spread_cost`` is reported informationally (half-spread paid per taker leg
relative to mid) but is NOT subtracted: the raw edge is already measured at
the ask, so subtracting the spread again would double-count. When a venue
does not publish bids (Kalshi public book), spread is unavailable and the
field is 0.0 with a flag.

Provisional / labeled parts (see configs/fees.yaml):
  * Polymarket ``base_fee / 10000`` conversion is provisional until Phase 7
    reconciles the endpoint, market metadata, and official docs.
  * When no live fee rate is known, a 0.05 fallback rate is used and the
    opportunity is labeled FEE_RATE_FALLBACK.
  * The latency drift rate is a placeholder until calibrated (Phase 9+).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from backend.schemas import CostBreakdown, Market, OrderBook, Venue

# Labels attached to cost breakdowns / explanations.
FEE_RATE_FALLBACK = "FEE_RATE_FALLBACK"
POLYMARKET_FEE_PROVISIONAL = "POLYMARKET_FEE_PROVISIONAL"
LATENCY_DRIFT_PLACEHOLDER = "LATENCY_DRIFT_PLACEHOLDER"
SPREAD_UNAVAILABLE = "SPREAD_UNAVAILABLE_NO_BIDS"

_FALLBACK_TAKER_RATE = 0.05
_POLYMARKET_MIN_FEE_USDC = 0.0001


@dataclass
class FeeQuote:
    """Fee for one leg at a given size."""

    venue: Venue
    contracts: float
    price: float
    fee_dollars: float
    rate_used: float | None
    notes: list[str] = field(default_factory=list)


class FeeModel:
    """Venue fee schedules from configs/fees.yaml.

    Polymarket: fee = C * fee_rate * (p * (1 - p)) ** exponent (exponent 1
    unless the market says otherwise); makers pay 0; amounts below
    $0.0001 round to zero. The ``base_fee / 10000`` conversion of the live
    rate is provisional (see module docstring).
    Kalshi: taker fee = round_UP(M * 0.07 * C * P * (1 - P)) to the cent;
    maker fee uses 0.0175. No settlement or membership fees.
    """

    def __init__(self, fallback_taker_rate: float = _FALLBACK_TAKER_RATE) -> None:
        self.fallback_taker_rate = fallback_taker_rate

    def resolve_taker_rate(self, market: Market) -> tuple[float | None, list[str]]:
        """Return (taker_rate, notes). Falls back to 0.05 with a label."""
        notes: list[str] = []
        rate = market.taker_fee_rate
        if rate is None:
            if market.venue == Venue.KALSHI:
                # Formula-based per trade; no single rate to resolve.
                return None, notes
            rate = self.fallback_taker_rate
            notes.append(FEE_RATE_FALLBACK)
        elif market.venue == Venue.POLYMARKET:
            notes.append(POLYMARKET_FEE_PROVISIONAL)
        return rate, notes

    def taker_fee(self, market: Market, contracts: float, price: float,
                  *, multiplier: float = 1.0, exponent: float = 1.0) -> FeeQuote:
        """Taker fee in dollars for buying ``contracts`` at ``price``."""
        notes: list[str] = []
        if market.venue == Venue.POLYMARKET:
            rate, rate_notes = self.resolve_taker_rate(market)
            notes.extend(rate_notes)
            raw = contracts * (rate or 0.0) * (price * (1.0 - price)) ** exponent
            fee = raw if raw >= _POLYMARKET_MIN_FEE_USDC else 0.0
            return FeeQuote(market.venue, contracts, price, round(fee, 6),
                            rate, notes)
        if market.venue == Venue.KALSHI:
            raw = multiplier * 0.07 * contracts * price * (1.0 - price)
            fee = math.ceil(raw * 100) / 100.0  # round UP to the cent
            return FeeQuote(market.venue, contracts, price, round(fee, 6),
                            None, notes)
        raise ValueError(f"unknown venue {market.venue}")

    def maker_fee(self, market: Market, contracts: float, price: float,
                  *, multiplier: float = 1.0) -> FeeQuote:
        """Maker fee in dollars (Kalshi designated series; Polymarket: 0)."""
        if market.venue == Venue.POLYMARKET:
            return FeeQuote(market.venue, contracts, price, 0.0, 0.0, [])
        if market.venue == Venue.KALSHI:
            raw = multiplier * 0.0175 * contracts * price * (1.0 - price)
            fee = math.ceil(raw * 100) / 100.0
            return FeeQuote(market.venue, contracts, price, round(fee, 6),
                            None, ["KALSHI_MAKER_SERIES_ONLY"])
        raise ValueError(f"unknown venue {market.venue}")


@dataclass
class WalkResult:
    """Result of walking one side of a book for a target size."""

    filled: float
    shortfall: float
    vwap: float | None  # average price of the filled portion
    levels_used: int


def walk_book(book: OrderBook, side: str, size: float) -> WalkResult:
    """Walk the full book for ``size`` contracts.

    side="buy" consumes asks best-first; side="sell" consumes bids
    best-first. Returns the VWAP of whatever could be filled plus any
    shortfall. An empty relevant side yields filled=0.
    """
    if side not in ("buy", "sell"):
        raise ValueError("side must be 'buy' or 'sell'")
    if size <= 0:
        return WalkResult(filled=0.0, shortfall=0.0, vwap=None, levels_used=0)
    levels = book.asks if side == "buy" else book.bids
    remaining = size
    notional = 0.0
    filled = 0.0
    used = 0
    for lvl in levels:
        if remaining <= 0:
            break
        take = min(remaining, lvl.size)
        notional += take * lvl.price
        filled += take
        remaining -= take
        used += 1
    if filled <= 0:
        return WalkResult(filled=0.0, shortfall=size, vwap=None, levels_used=0)
    return WalkResult(filled=round(filled, 6), shortfall=round(remaining, 6),
                      vwap=round(notional / filled, 6), levels_used=used)


def half_spread_cost(book: OrderBook) -> tuple[float | None, list[str]]:
    """Half-spread per contract vs mid, or (None, [flag]) when unavailable."""
    if book.best_bid is None or book.best_ask is None:
        return None, [SPREAD_UNAVAILABLE]
    return round((book.best_ask - book.best_bid) / 2.0, 6), []


def latency_adjustment(latency_seconds: float, drift_per_second: float,
                       drift_is_placeholder: bool) -> tuple[float, list[str]]:
    """Expected adverse price move over the latency budget."""
    notes = [LATENCY_DRIFT_PLACEHOLDER] if drift_is_placeholder else []
    return round(latency_seconds * drift_per_second, 6), notes


def build_cost_breakdown(*, raw_edge_per_contract: float, size: float,
                         fees_total: float, slippage_total: float,
                         spread_info_per_contract: float,
                         latency_total: float) -> CostBreakdown:
    """Assemble the waterfall. All totals are for ``size`` contracts.

    net_edge is per contract: raw - fees/size - slippage/size - latency/size.
    spread is informational only (see module docstring).
    """
    per = size if size > 0 else 1.0
    net = (raw_edge_per_contract
           - fees_total / per
           - slippage_total / per
           - latency_total / per)
    return CostBreakdown(
        raw_edge=round(raw_edge_per_contract, 6),
        trading_fees=round(fees_total / per, 6),
        spread_cost=round(spread_info_per_contract, 6),
        slippage=round(slippage_total / per, 6),
        latency_adjustment=round(latency_total / per, 6),
        net_edge=round(net, 6),
    )
