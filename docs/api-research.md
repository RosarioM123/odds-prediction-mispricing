# API Research: Polymarket and Kalshi

Researched 2026-09-18 from official documentation. This file records what was
verified, what requires credentials, and what the engine assumes. Re-verify
before trusting any number for live decisions.

## Polymarket

Official docs: https://docs.polymarket.com
Machine-readable specs: https://docs.polymarket.com/api-spec/clob-openapi.yaml
and https://docs.polymarket.com/api-spec/gamma-openapi.yaml

### Gamma API (market discovery)

- Base: `https://gamma-api.polymarket.com`
- No authentication required.
- Key endpoints: `GET /events`, `GET /markets`, `GET /public-search`,
  `GET /tags`, `GET /series`, `GET /sports`.
- Rate limits are generous for reads (roughly 4,000 requests per 10 seconds
  overall; `/events` about 500/10s, `/markets` about 300/10s).
- Response model: events are top-level questions; markets are tradable binary
  outcomes nested inside events. Each market carries `conditionId` and
  `clobTokenIds`, a JSON array string `[yes_token_id, no_token_id]` that joins
  into the CLOB API.

### CLOB API (order books, prices, trading)

- Base: `https://clob.polymarket.com`
- Public market-data endpoints need no auth: order book, price, midpoint,
  spread, tick size, fee rate, price history.
- Authenticated endpoints (order placement, account reads) use L1/L2
  HMAC wallet signing. This project never calls them: all execution is
  simulated.
- Key endpoints:
  - `GET /book?token_id={id}` returns `{"bids": [...], "asks": [...]}` with
    levels as `{"price": "0.55", "size": "1000"}`. Levels arrive worst-first;
    the best level is the LAST element of each array.
  - `GET /fee-rate?token_id={id}` returns `{"base_fee": <basis points>}`.
- WebSocket market channel: `wss://ws-subscriptions-clob.polymarket.com/ws/market`
  (public; user channel requires auth).
- Real-time tick-size changes occur when price crosses 0.96 or 0.04.

### Polymarket fees

- Fee page: https://docs.polymarket.com/trading/fees
- Formula: `fee = C x feeRate x (p x (1 - p))^exponent`, where C is share
  count and p is price. Fees peak at p = 0.50 and decay toward the extremes.
- Most markets are fee-free; taker fees apply to specific categories and are
  redistributed to makers as rebates. Makers pay 0.
- Minimum fee 0.0001 USDC; smaller amounts round to zero.
- Documented category snapshot (mid-2026): Crypto 0.07, Sports/Economics/
  Culture/Weather/Other 0.05, Finance/Politics/Mentions/Tech 0.04,
  Geopolitics 0. The docs explicitly warn against hardcoding rates: resolve
  per token via `GET /fee-rate` at runtime.

## Kalshi

Official docs: https://docs.kalshi.com (the old trading-api.readme.io site is
deprecated). OpenAPI spec for event contracts is downloadable from the docs.

### REST API

- Production: `https://external-api.kalshi.com/trade-api/v2`
- Demo: `https://external-api.demo.kalshi.co/trade-api/v2`
- Public endpoints (no auth): series, events, markets, order books.
  Confirmed by the official "Quick Start: Market Data" guide.
- Key endpoints:
  - `GET /trade-api/v2/markets?status=open`
  - `GET /trade-api/v2/markets/{ticker}/orderbook` returns yes/no arrays of
    `[price_cents, count]` pairs. Prices are integer cents (1-99).
- Authenticated endpoints use API key id plus RSA signature headers
  (`KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-SIGNATURE`, `KALSHI-ACCESS-TIMESTAMP`).
  Not used by this project.
- Rate limits are tiered token buckets (Basic tier: 200 reads/sec).

### Kalshi fees

- Fee schedule (effective 2026-07-07): https://kalshi.com/docs/kalshi-fee-schedule.pdf
- Taker: `fee = round_up(M x 0.07 x C x P x (1 - P))`
- Maker: `fee = round_up(M x 0.0175 x C x P x (1 - P))` on designated series
  only, charged when the resting order executes. Cancelling is free.
- M is the series multiplier (default 1), C is contract count, P is price in
  dollars. Rounding is UP to the next cent per executed order.
- No settlement fee, no membership fee.

## Implications for the engine

1. Both venues expose full order books without credentials, so detection and
   paper execution need no API keys at all.
2. Fees are nonlinear in price on both venues (peak at 0.50), so the cost
   model must evaluate fees at the actual execution price, not a flat rate.
3. Polymarket fee rates are per-token and dynamic: cache with a TTL and label
   any opportunity that used a fallback rate.
4. Kalshi prices are integer cents; normalize to dollars (divide by 100)
   before comparing with Polymarket.
5. Settlement and market-rule differences between venues are a real
   cross-venue risk and must be documented per matched pair, not assumed away.

## Open verification items (Phase 2)

- Confirm the exact Kalshi order-book response envelope with one live call.
- Confirm Polymarket `/fee-rate` behavior for a fee-free token.
- Measure real round-trip latency to both venues for the latency budget.

## Live verification log (2026-09-18)

All calls below were public, unauthenticated GETs.

### Polymarket (production)

- `GET gamma-api.polymarket.com/markets?active=true&closed=false&limit=1`
  returned a market with `outcomes: '["Yes", "No"]'`,
  `clobTokenIds: '["<yes_token>", "<no_token>"]'` (JSON-encoded strings),
  `orderPriceMinTickSize: 0.001`, `orderMinSize: 5`, `takerBaseFee: 1000`,
  `acceptingOrders: true`.
- `GET clob.polymarket.com/book?token_id=<yes_token>` returned
  `{"market", "asset_id", "timestamp": "1789690457621" (ms, string), "hash",
  "bids": [{"price": "0.001", "size": "10465150.31"}, ...]}`.
  Levels arrive **worst-first** (bids ascending, asks descending); the
  adapter reverses them to best-first. A real book had 41 bids / 132 asks.
- `GET /fee-rate?token_id=` returned `{"base_fee": 1000}`.
- `GET /tick-size?token_id=` returned `{"minimum_tick_size": 0.001}`.
- `GET /price?token_id=&side=BUY` returned `{"price": "0.042"}`.
- Caveat: `base_fee` values observed (0, 1000) are coarse relative to the
  documented per-category formula rates. The adapter resolves and caches
  the live value; Phase 7 must pin down exactly how it enters the cost
  model. Do not treat `base_fee / 10000` as the final fee without that work.

### Kalshi

- Production (`external-api.kalshi.com`) returned **HTTP 403** to this
  datacenter IP on 2026-09-18, for both `curl` and Python clients.
  The demo host worked. Implication: production reads may require
  non-datacenter egress; the adapter defaults to `env="demo"` and raises
  a descriptive error on production 403s.
- Demo (`external-api.demo.kalshi.co`) `GET /markets?status=open` returned
  `{"markets": [{ticker, event_ticker, title, yes_sub_title, no_sub_title,
  status: "active", yes_bid/yes_ask (cents or null), expiration_time, ...}],
  "cursor": ...}`. Note the query filter uses `status=open` while market
  objects report `status: "active"`; both are treated as tradable.
- Demo `GET /markets/{ticker}/orderbook` returned the envelope
  `{"orderbook_fp": {"yes_dollars": [], "no_dollars": []}}`
  (dollar floats). The documented classic envelope
  `{"orderbook": {"yes": [[cents, count]], "no": [...]}}` is also accepted;
  cents are divided by 100.
- Demo markets carried **no liquidity** (50 sampled, zero with quotes),
  so a non-empty Kalshi book could not be captured live. Adapter behavior
  on real ladders is covered by fixture tests only, labeled as such.
- **Documented limitation:** the public orderbook envelope does not
  separate bids from asks, so Kalshi books normalize with asks populated
  and bids empty. Spread and mid-price are unavailable for Kalshi until
  bid semantics are verified. Bundle arbitrage (YES ask + NO ask) is
  unaffected.

### Remaining open items

- Confirm Polymarket `/fee-rate` behavior for a fee-free token.
- Measure real round-trip latency to both venues for the latency budget.
- Capture a non-empty Kalshi order book from production or a liquid demo
  market to confirm ladder semantics.
