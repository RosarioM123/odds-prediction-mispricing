"""Normalized data schemas for the prediction-mispricing engine.

Every venue adapter (Polymarket, Kalshi) converts its native API payloads into
these models. Downstream code (arbitrage detection, cost modeling, execution
simulation) only ever sees these types and never touches venue-specific JSON.
"""
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, field_validator


class Venue(str, Enum):
    POLYMARKET = "polymarket"
    KALSHI = "kalshi"


class MarketStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class Outcome(str, Enum):
    YES = "YES"
    NO = "NO"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class OrderBookLevel(BaseModel):
    """One price level. Price in dollars per contract/share, size in contracts."""

    price: float = Field(ge=0.0, le=1.0)
    size: float = Field(ge=0.0)


class OrderBook(BaseModel):
    """Normalized limit order book for a single outcome token.

    Bids are sorted best-first (highest price first); asks best-first
    (lowest price first). All prices are executable quotes in dollars.
    """

    market_id: str
    venue: Venue
    outcome: Outcome
    bids: list[OrderBookLevel] = Field(default_factory=list)
    asks: list[OrderBookLevel] = Field(default_factory=list)
    venue_timestamp: datetime | None = None
    received_timestamp: datetime = Field(default_factory=utcnow)

    @field_validator("bids")
    @classmethod
    def _bids_sorted(cls, v: list[OrderBookLevel]) -> list[OrderBookLevel]:
        prices = [lvl.price for lvl in v]
        if prices != sorted(prices, reverse=True):
            raise ValueError("bids must be sorted best-first (descending price)")
        return v

    @field_validator("asks")
    @classmethod
    def _asks_sorted(cls, v: list[OrderBookLevel]) -> list[OrderBookLevel]:
        prices = [lvl.price for lvl in v]
        if prices != sorted(prices):
            raise ValueError("asks must be sorted best-first (ascending price)")
        return v

    @property
    def best_bid(self) -> float | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> float | None:
        return self.asks[0].price if self.asks else None

    @property
    def spread(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round(self.best_ask - self.best_bid, 6)

    @property
    def mid_price(self) -> float | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return round((self.best_bid + self.best_ask) / 2, 6)

    @property
    def bid_depth(self) -> float:
        return round(sum(lvl.size for lvl in self.bids), 6)

    @property
    def ask_depth(self) -> float:
        return round(sum(lvl.size for lvl in self.asks), 6)


class Market(BaseModel):
    """Normalized market metadata for one tradable outcome."""

    market_id: str
    venue: Venue
    event_id: str
    question: str
    outcome: Outcome
    expiration: datetime | None = None
    status: MarketStatus = MarketStatus.UNKNOWN
    tick_size: float = Field(default=0.01, gt=0.0)
    taker_fee_rate: float | None = Field(
        default=None,
        ge=0.0,
        description="Resolved taker fee rate for this market, if known. "
        "Polymarket: from GET /fee-rate per token. Kalshi: formula-based.",
    )
    raw: dict = Field(
        default_factory=dict,
        description="Venue-native payload preserved for auditability.",
    )


class CostBreakdown(BaseModel):
    """Traceable transaction-cost decomposition for one opportunity leg set.

    All values in dollars per contract/share unless noted. Every component
    must be computable from the order book and configs/fees.yaml.
    """

    raw_edge: float
    trading_fees: float = 0.0
    spread_cost: float = 0.0
    slippage: float = 0.0
    latency_adjustment: float = 0.0
    net_edge: float = 0.0


class LatencyBreakdown(BaseModel):
    """Timestamps tracing an opportunity from quote to simulated fill."""

    market_data_timestamp: datetime
    detection_timestamp: datetime
    decision_timestamp: datetime
    simulated_execution_timestamp: datetime

    @property
    def data_latency_ms(self) -> float:
        return (self.detection_timestamp - self.market_data_timestamp).total_seconds() * 1000

    @property
    def processing_latency_ms(self) -> float:
        return (self.decision_timestamp - self.detection_timestamp).total_seconds() * 1000

    @property
    def execution_latency_ms(self) -> float:
        return (self.simulated_execution_timestamp - self.decision_timestamp).total_seconds() * 1000

    @property
    def total_latency_ms(self) -> float:
        return (self.simulated_execution_timestamp - self.market_data_timestamp).total_seconds() * 1000


class Opportunity(BaseModel):
    """A detected mispricing with full explainability attached."""

    opportunity_id: str
    strategy: str  # "bundle_arbitrage" | "cross_venue_arbitrage"
    detected_at: datetime = Field(default_factory=utcnow)
    legs: list[dict] = Field(
        default_factory=list,
        description="Each leg: venue, market_id, outcome, side, quantity, expected_price.",
    )
    costs: CostBreakdown
    liquidity: float = Field(ge=0.0)
    match_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    decision: str = "PENDING"  # PAPER_EXECUTE | REJECTED | PENDING
    explanation: dict = Field(default_factory=dict)


class PaperTradeStatus(str, Enum):
    FILLED = "FILLED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    MISSED = "MISSED"
    EXPIRED = "EXPIRED"


class PaperTrade(BaseModel):
    """Record of one simulated execution. No real orders are ever placed."""

    trade_id: str
    opportunity_id: str
    timestamp: datetime = Field(default_factory=utcnow)
    venue: Venue
    market_id: str
    side: str
    quantity: float = Field(gt=0.0)
    expected_price: float
    simulated_fill_price: float
    fees: float = 0.0
    slippage: float = 0.0
    gross_pnl: float = 0.0
    net_pnl: float = 0.0
    latency_ms: float = 0.0
    status: PaperTradeStatus = PaperTradeStatus.FILLED
