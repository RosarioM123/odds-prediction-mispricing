"""Polymarket market-data adapter (Phase 2 implementation).

Endpoint map (verified 2026-09-18 against official docs; see docs/api-research.md):

  Gamma API (market discovery, no auth):
    GET https://gamma-api.polymarket.com/events
    GET https://gamma-api.polymarket.com/markets
    GET https://gamma-api.polymarket.com/public-search
  Each market carries `conditionId` and `clobTokenIds` (JSON array string of
  the two outcome token ids: [yes_token_id, no_token_id]).

  CLOB API (order books and prices, public reads need no auth):
    GET https://clob.polymarket.com/book?token_id={token_id}
        -> {"bids": [{"price": "0.55", "size": "1000"}], "asks": [...]}
           NOTE: bids/asks arrive worst-first; best level is the LAST element.
    GET https://clob.polymarket.com/price?token_id=..&side=BUY|SELL
    GET https://clob.polymarket.com/midpoint?token_id=..
    GET https://clob.polymarket.com/spread?token_id=..
    GET https://clob.polymarket.com/tick-size?token_id=..
    GET https://clob.polymarket.com/fee-rate?token_id=..
        -> {"base_fee": <int basis points>}  (e.g. 0, 300, 1000)

Fee model: taker fee = C * feeRate * (p * (1 - p))^exponent, where C is share
count, p is price, and feeRate/exponent are per-market. The official docs warn
against hardcoding rates: resolve per token via /fee-rate at runtime and fall
back to configs/fees.yaml only when the endpoint is unreachable. Makers pay 0.
"""
from backend.markets.base import MarketDataAdapter
from backend.schemas import Venue


class PolymarketAdapter(MarketDataAdapter):
    venue = Venue.POLYMARKET

    GAMMA_BASE = "https://gamma-api.polymarket.com"
    CLOB_BASE = "https://clob.polymarket.com"

    def fetch_markets(self, *, status: str = "open", limit: int = 100):
        raise NotImplementedError("Phase 2: implement Gamma /events pagination")

    def fetch_order_book(self, market):
        raise NotImplementedError("Phase 2: implement CLOB /book fetch")

    def normalize_market(self, raw: dict):
        raise NotImplementedError("Phase 2: implement Gamma payload normalization")

    def normalize_order_book(self, raw: dict, market, venue_ts=None):
        raise NotImplementedError("Phase 2: implement CLOB book normalization")
