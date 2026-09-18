# ODDS

## Prediction Market Mispricing and Execution Engine

*Find the mispriced odd. Price the cost of catching it.*

A quantitative research and execution engine that asks one question:

> When a prediction-market price discrepancy appears, is it actually
> executable after fees, liquidity, slippage, and latency?

The engine pulls order-book data from Polymarket and Kalshi, detects
candidate arbitrage (same-market YES/NO bundles and cross-venue equivalents),
models the full cost of capturing the edge, sizes positions with fractional
Kelly, and paper-executes the survivors. No real orders are ever placed.

## Status

**Phase 2 complete.** Repository structure, normalized schemas, live
Polymarket and Kalshi adapters, labeled snapshot I/O, traceable cost
configuration, and API research are in place. Phases 3-13 are tracked below.
Nothing here fabricates results: adapter tests run on labeled fixtures, and
live smoke tests hit the public APIs directly.

## The problem

Prediction markets occasionally price the same payoff inconsistently:
the YES and NO asks of one contract can sum below $1.00, or two venues can
quote economically equivalent contracts at different prices. Most of these
discrepancies evaporate once you account for taker fees, the spread you
cross, slippage from walking the book, and the latency between seeing a
quote and filling against it. This project measures the edge that survives.

## Approach

```
Venue APIs (public reads, no auth)
  -> MarketDataAdapter (Polymarket, Kalshi)
  -> Normalized schemas (Market, OrderBook, Opportunity, PaperTrade)
  -> Arbitrage detection (bundle + cross-venue matching)
  -> Transaction-cost model (fees, spread, slippage, latency)
  -> Risk controls (position, exposure, loss, latency, confidence gates)
  -> Paper execution (simulated fills, never real orders)
  -> Persistence, historical replay, dashboard
```

See `docs/architecture.md` for the full pipeline and `docs/api-research.md`
for the verified API investigation that this design is built on.

## Quantitative methodology (planned)

- **Arbitrage condition.** Bundle: YES ask + NO ask < 1. Cross-venue:
  executable YES prices differ beyond costs on a high-confidence match.
- **VWAP.** Order-book walking fills a requested quantity level by level;
  execution price is the volume-weighted average, never the top of book.
- **Slippage.** Method 1: book-walking. Method 2: empirical impact as a
  function of size, depth, spread, and volatility, calibrated on snapshots.
- **Fees.** Polymarket: `C x feeRate x (p x (1-p))^exponent`, resolved live
  per token via `GET /fee-rate` (makers pay 0). Kalshi: taker
  `round_up(M x 0.07 x C x P x (1-P))`, maker `0.0175` factor on designated
  series. See `configs/fees.yaml`; every figure cites its source.
- **Kelly sizing.** Fractional Kelly (0.25 / 0.50 / 1.00 configurable),
  capped by available liquidity, per-market and portfolio exposure limits.
- **Latency.** Data, processing, and execution timestamps tracked per
  opportunity; expected adverse drift applied to the execution price.
- **Risk.** Maximum position per market, portfolio and venue exposure,
  maximum daily loss, minimum net edge, minimum liquidity, minimum match
  confidence, maximum acceptable latency. Violations reject the trade.

## What the venues give us (verified 2026-09-18)

| | Polymarket | Kalshi |
|---|---|---|
| Market discovery | Gamma API, no auth | REST `/markets`, no auth |
| Order books | CLOB `GET /book`, no auth | `GET /markets/{ticker}/orderbook`, no auth |
| Fee resolution | `GET /fee-rate` per token | Formula from official schedule |
| Trading auth | Wallet HMAC (unused) | API key + RSA (unused) |
| Price units | Dollars | Integer cents (normalized to dollars) |

Full notes and sources: `docs/api-research.md`.

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
pytest -q
```

Copy `.env.example` to `.env` if you need it. No credentials are required:
public market-data endpoints need none, and execution is simulated.

## Repository layout

```
backend/
  schemas.py        # normalized Pydantic models (venue-independent)
  config.py         # loads configs/*.yaml
  markets/          # MarketDataAdapter ABC + Polymarket/Kalshi adapters
  arbitrage/        # bundle + cross-venue detectors (Phase 5-6)
  execution/        # paper execution engine (Phase 8)
  risk/             # risk controls (Phase 8)
  backtesting/      # event-driven historical replay (Phase 9)
  database/         # persistence (Phase 2)
  models/           # optional NLP matching, advisory only (Phase 12)
  api/              # FastAPI service (Phase 10)
configs/
  fees.yaml         # fee formulas with sources
  strategy.yaml     # thresholds, Kelly, risk, latency assumptions
  venues.yaml       # endpoint map and rate limits
data/raw|processed/ # snapshots (gitignored, regenerable)
docs/               # api-research.md, architecture.md
tests/
frontend/           # React + TypeScript dashboard (Phase 10)
```

## Build phases

- [x] Phase 1: repository and architecture
- [x] Phase 2: Polymarket/Kalshi data adapters (live-verified, 28 tests)
- [ ] Phase 3: normalized market schema (schemas defined; adapters pending)
- [ ] Phase 4: order-book engine (VWAP, slippage)
- [ ] Phase 5: bundle arbitrage detector
- [ ] Phase 6: cross-venue market matching
- [ ] Phase 7: transaction-cost model
- [ ] Phase 8: paper execution and risk controls
- [ ] Phase 9: historical replay and backtesting
- [ ] Phase 10: dashboard
- [ ] Phase 11: full test suite
- [ ] Phase 12: AI-assisted market matching (advisory only)
- [ ] Phase 13: documentation

## Limitations

- Live public data flows through both venue adapters; no credentials needed
  for market data. Kalshi production rejected this datacenter IP (HTTP 403);
  the adapter defaults to the demo environment (see docs/api-research.md).
- Fee schedules change; `configs/fees.yaml` is a dated snapshot and the
  engine resolves live rates where the venue allows.
- Cross-venue pairs can differ in settlement rules, tick sizes, and
  expiration semantics; matching on price alone would be wrong.
- Latency drift and empirical slippage coefficients are placeholders until
  calibrated on real snapshots (labeled as such in config).
- Paper fills are simulations. Nothing here is investment advice and no
  real-money trading is implemented.
