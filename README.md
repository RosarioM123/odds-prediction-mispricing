# ODDS

![CI](https://github.com/RosarioM123/odds-prediction-mispricing/actions/workflows/ci.yml/badge.svg)

## Prediction Market Mispricing & Execution Engine

**Arbitrage detection across Polymarket and Kalshi order books — full-book VWAP cost modeling, fractional-Kelly sizing, and chronological paper execution. Paper trades only: no real orders, ever.**

> ⚠️ **Simulated data.** Every replay snapshot and dashboard fixture in this repo is labeled `ALL_INPUT_SIMULATED` and flagged `SAMPLE_TOO_SMALL`. Nothing here is live performance, and simulated results are never presented as such.

A quantitative research engine that asks one question:

> When a prediction-market price discrepancy appears, is it actually executable after fees, liquidity, slippage, and latency?

The engine pulls order-book data from Polymarket and Kalshi, detects candidate arbitrage (same-market YES/NO bundles and cross-venue equivalents), models the full cost of capturing the edge, sizes positions with fractional Kelly, and paper-executes the survivors. **88 tests pass.** The 30-day trading experiment has **not** been run — this is the software, not the trading.

## Demo

🎬 *Screen recording / GIF goes here — replay a labeled demo scenario through detect → size → execute, with the simulated-data banner visible.*

Until then, generate the dashboard fixture locally (see Quickstart) — every fixture renders behind a persistent **SIMULATED DATA** banner.

## Quickstart

30 seconds, no credentials needed (public market-data endpoints only; execution is simulated):

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q          # 88 tests

# Replay the labeled demo scenarios through detect -> size -> execute
.venv/bin/python scripts/make_dashboard_fixture.py

# Dashboard (reads the generated fixture; simulated data, bannered)
cd frontend/dashboard && npm install && npm run build && npm run preview
```

Copy `.env.example` to `.env` if you need it. Credentials are optional and unused by the pipeline.

## The problem

Prediction markets occasionally price the same payoff inconsistently: the YES and NO asks of one contract can sum below $1.00, or two venues can quote economically equivalent contracts at different prices. Most of these discrepancies evaporate once you account for taker fees, the spread you cross, slippage from walking the book, and the latency between seeing a quote and filling against it. This project measures the edge that survives.

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

See `docs/architecture.md` for the full pipeline and `docs/api-research.md` for the verified API investigation this design is built on.

## Quantitative methodology

- **Arbitrage condition.** Bundle: YES ask + NO ask < 1. Cross-venue: executable YES prices differ beyond costs on a high-confidence match.
- **VWAP.** Order-book walking fills a requested quantity level by level; execution price is the volume-weighted average, never the top of book.
- **Slippage.** Method 1: book-walking. Method 2: empirical impact as a function of size, depth, spread, and volatility, calibrated on snapshots.
- **Fees.** Polymarket: `C x feeRate x (p x (1-p))^exponent`, resolved live per token via `GET /fee-rate` (makers pay 0). Kalshi: taker `round_up(M x 0.07 x C x P x (1-P))`, maker `0.0175` factor on designated series. See `configs/fees.yaml`; every figure cites its source.
- **Kelly sizing.** Fractional Kelly (0.25 / 0.50 / 1.00 configurable), capped by available liquidity, per-market and portfolio exposure limits.
- **Latency.** Data, processing, and execution timestamps tracked per opportunity; expected adverse drift applied to the execution price.
- **Risk.** Maximum position per market, portfolio and venue exposure, maximum daily loss, minimum net edge, minimum liquidity, minimum match confidence, maximum acceptable latency. Violations reject the trade.

## What the venues give us (verified 2026-09-18)

| | Polymarket | Kalshi |
|---|---|---|
| Market discovery | Gamma API, no auth | REST `/markets`, no auth |
| Order books | CLOB `GET /book`, no auth | `GET /markets/{ticker}/orderbook`, no auth |
| Fee resolution | `GET /fee-rate` per token | Formula from official schedule |
| Trading auth | Wallet HMAC (unused) | API key + RSA (unused) |
| Price units | Dollars | Integer cents (normalized to dollars) |

Full notes and sources: `docs/api-research.md`.

## Standing caveats (read before trusting any number)

- **Kalshi bids remain empty** — public bid/ask semantics were not sufficiently verified, so the Kalshi side of the book is asks-only until that changes.
- **Polymarket `base_fee / 10000` is provisional** — treated as a working assumption, not a verified constant.
- **AI may propose matches but cannot authorize trades** — matching suggestions are advisory; there is no autonomous order path.
- **No real-money order path exists** — paper execution only, by design.
- Latency drift and empirical slippage coefficients are placeholders until calibrated on real snapshots (labeled as such in config).
- Nothing here is investment advice.

## Repository layout

```
backend/
  schemas.py        # normalized Pydantic models (venue-independent)
  config.py         # loads configs/*.yaml
  markets/          # MarketDataAdapter ABC + Polymarket/Kalshi adapters
  arbitrage/        # costs.py, opportunities.py, sizing.py, settings.py
  execution/        # paper.py: simulated fills, never real orders
  risk/             # gates.py: deterministic risk controls
  backtesting/      # replay.py: chronological no-look-ahead replay
  database/         # persistence
  models/           # optional NLP matching, advisory only
  api/              # FastAPI service
configs/
  fees.yaml         # fee formulas with sources
  strategy.yaml     # thresholds, Kelly, risk, latency assumptions
  venues.yaml       # endpoint map and rate limits
data/raw|processed/ # snapshots (gitignored, regenerable)
docs/               # api-research.md, architecture.md
scripts/            # make_dashboard_fixture.py: engine -> dashboard fixture
tests/              # adapter + engine tests, labeled fixtures
frontend/           # dashboard/: React + Sass replay dashboard
```

## Build phases

- [x] Phase 1: repository and architecture
- [x] Phase 2: Polymarket/Kalshi data adapters (live-verified, 28 tests; reconciled upstream)
- [x] Phase 3: quantitative engine — costs, detection, sizing, risk, paper execution, replay (60 engine tests)
- [x] Phase 4: order-book engine (VWAP, slippage) — `backend/arbitrage/costs.py`
- [x] Phase 5: bundle arbitrage detector — `backend/arbitrage/opportunities.py`
- [x] Phase 6: cross-venue market matching (deterministic; NLP advisory only)
- [x] Phase 7: transaction-cost model — `backend/arbitrage/costs.py` + `configs/fees.yaml`
- [x] Phase 8: paper execution and risk controls — `backend/execution/paper.py`, `backend/risk/gates.py`
- [x] Phase 9: historical replay and backtesting — `backend/backtesting/replay.py`
- [x] Phase 10: dashboard — `frontend/dashboard/` (React + Sass, simulated fixtures)
- [x] Phase 11: full test suite — 88 tests, all passing
- [ ] Phase 12: AI-assisted market matching (advisory only)
- [ ] Phase 13: documentation

## Limitations

- Live public data flows through both venue adapters; no credentials needed for market data. Kalshi production rejected this datacenter IP (HTTP 403); the adapter defaults to the demo environment (see docs/api-research.md).
- Fee schedules change; `configs/fees.yaml` is a dated snapshot and the engine resolves live rates where the venue allows.
- Cross-venue pairs can differ in settlement rules, tick sizes, and expiration semantics; matching on price alone would be wrong.
- Paper fills are simulations. Nothing here is investment advice and no real-money trading is implemented.
