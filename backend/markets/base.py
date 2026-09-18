"""Abstract market-data adapter interface.

Every venue adapter implements this contract. The arbitrage engine, cost
model, and execution simulator depend only on this interface and on
backend.schemas, never on venue-specific payloads.
"""
from abc import ABC, abstractmethod
from datetime import datetime

from backend.schemas import Market, OrderBook, Venue


class MarketDataAdapter(ABC):
    """Common interface for Polymarket and Kalshi market-data access."""

    venue: Venue

    @abstractmethod
    def fetch_markets(self, *, status: str = "open", limit: int = 100) -> list[Market]:
        """Return normalized, currently tradable markets."""
        raise NotImplementedError

    @abstractmethod
    def fetch_order_book(self, market: Market) -> OrderBook:
        """Return the normalized live order book for one market outcome."""
        raise NotImplementedError

    @abstractmethod
    def normalize_market(self, raw: dict) -> Market:
        """Convert one venue-native market payload to the normalized schema."""
        raise NotImplementedError

    @abstractmethod
    def normalize_order_book(self, raw: dict, market: Market, venue_ts: datetime | None = None) -> OrderBook:
        """Convert one venue-native order-book payload to the normalized schema."""
        raise NotImplementedError

    # -- fee resolution -----------------------------------------------------
    def resolve_taker_fee_rate(self, market: Market) -> float | None:
        """Return the taker fee rate for a market, or None if unknown.

        Default implementation returns the rate already attached to the
        market by normalize_market. Venues with dynamic per-market fees
        (Polymarket) override this to query the live endpoint.
        """
        return market.taker_fee_rate
