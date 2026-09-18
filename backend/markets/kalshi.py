"""Kalshi market-data adapter (Phase 2 implementation).

Endpoint map (verified 2026-09-18 against official docs; see docs/api-research.md):

  REST base (production): https://external-api.kalshi.com/trade-api/v2
  REST base (demo):        https://external-api.demo.kalshi.co/trade-api/v2

  Public endpoints (no auth required):
    GET /trade-api/v2/series
    GET /trade-api/v2/events
    GET /trade-api/v2/markets?status=open
    GET /trade-api/v2/markets/{ticker}/orderbook
        -> {"orderbook": {"yes": [[price_cents, count], ...],
                          "no":  [[price_cents, count], ...]}}
           Prices are in integer cents (1-99); counts are contract counts.

  Authenticated endpoints (API key + RSA signature headers
  KALSHI-ACCESS-KEY / KALSHI-ACCESS-SIGNATURE / KALSHI-ACCESS-TIMESTAMP)
  are only needed for order placement, which this project never performs.

Fee model (official fee schedule, effective 2026-07-07):
  taker_fee = round_up(M * 0.07 * C * P * (1 - P))
  maker_fee = round_up(M * 0.0175 * C * P * (1 - P))  (designated series only)
where M is the series multiplier (default 1), C is contract count, P is price
in dollars, and round_up rounds up to the next cent. No settlement fee.
"""
from backend.markets.base import MarketDataAdapter
from backend.schemas import Venue


class KalshiAdapter(MarketDataAdapter):
    venue = Venue.KALSHI

    PROD_BASE = "https://external-api.kalshi.com/trade-api/v2"
    DEMO_BASE = "https://external-api.demo.kalshi.co/trade-api/v2"

    def __init__(self, env: str = "demo"):
        self.base_url = self.DEMO_BASE if env == "demo" else self.PROD_BASE

    def fetch_markets(self, *, status: str = "open", limit: int = 100):
        raise NotImplementedError("Phase 2: implement /markets pagination")

    def fetch_order_book(self, market):
        raise NotImplementedError("Phase 2: implement /markets/{ticker}/orderbook")

    def normalize_market(self, raw: dict):
        raise NotImplementedError("Phase 2: implement market payload normalization")

    def normalize_order_book(self, raw: dict, market, venue_ts=None):
        raise NotImplementedError("Phase 2: implement orderbook normalization")
