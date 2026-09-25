> **Status: paused.** Active work is on [world-workstate-infrastructure](https://github.com/RosarioM123/world-workstate-infrastructure). This repo resumes after the WORLD handoff experiment is validated.

# ODDS

![CI](https://github.com/RosarioM123/odds-prediction-mispricing/actions/workflows/ci.yml/badge.svg)

## Prediction Market Mispricing & Execution Engine

**Arbitrage detection across Polymarket and Kalshi order books — full-book VWAP cost modeling, fractional-Kelly sizing, and chronological paper execution. Paper trades only: no real orders, ever.**

> ⚠️ **Simulated data.** Every replay snapshot and dashboard fixture in this repo is labeled `ALL_INPUT_SIMULATED` and flagged `SAMPLE_TOO_SMALL`. Nothing here is live performance, and simulated results are never presented as such.

A quantitative research engine that asks one question:

> When a prediction-market price discrepancy appears, is it actually executable after fees, liquidity, slippage, and latency?

The engine pulls order-book data from Polymarket and Kalshi, detects candidate arbitrage (same-market YES/NO bundles and cross-venue equivalents), models the full cost of capturing the edge, sizes positions with fractional Kelly, and paper-executes the survivors. **305 tests pass.** The 30-day trading experiment has **not** been run — this is the software, not the trading.

## Demo

🎬 *Screen recording / GIF goes here — replay a labeled demo scenario through detect → size → execute, with the simulated-data banner visible.*

Until then, generate the dashboard fixture locally (see Quickstart) — every fixture renders behind a persistent **SIMULATED DATA** banner.

## Quickstart

30 seconds, no credentials needed (public market-data endpoints only; execution is simulated):

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/python -m pytest tests/ -q          # 305 tests

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

## Live operations (paper trading only)

Everything below touches only free public endpoints and writes only to
local files. No orders are ever placed.

- **Live scan loop.** `scripts/scan_live.py` polls both venues with a
  thread pool (Polymarket Gamma + CLOB, Kalshi with automatic
  production-to-demo fallback), runs detect → cost → size → risk-gate →
  paper-execute, and persists every scan plus its opportunities to a
  SQLite ledger (`data/live/ledger.db`). One scan of 24 books completes
  in about 8 seconds:

  ```bash
  .venv/bin/python scripts/scan_live.py --once --poly-limit 12 --kalshi-limit 12 \
      --workers 4 --min-volume-24h 1000 --ledger data/live/ledger.db
  ```

  `--min-volume-24h` skips illiquid markets (24h USD volume from the
  venue-native payload). HTTP 429s honor `Retry-After` (capped) with
  exponential backoff; transient 5xx are retried the same way.
- **Latency calibration.** `scripts/collect_latency_snapshots.py`
  records best bid/ask per book across rounds;
  `scripts/fit_calibration.py` fits mean |mid move|/s into
  `configs/calibration.yaml`. First fit, 2026-09-25: 2.88e-07 $/s,
  n = 29 moves over 15 Polymarket markets, labeled **preliminary**
  (Kalshi books in the sample were empty; see the caveats). Kelly
  `p_win` is intentionally **not** fitted: the live sample contained
  zero executions, so the 0.99 assumption stands (ADRs 0012, 0013).
- **Snapshot history.** `scripts/collect_snapshot_history.py` appends
  live snapshots to an append-only SQLite store
  (`backend/markets/snapshot_store.py`); `backend/backtesting/replay.py`
  replays them chronologically. No-look-ahead is structural: the
  detector only ever sees books at or before the snapshot being
  scored (covered by an explicit future-edge test), and the replay
  report decomposes P&L into gross edge, fees, slippage, latency
  adjustment, net expected, realized, and the gap.
- **Cross-venue matching.** `backend/arbitrage/matching.py` scores
  candidate pairs by normalized title token-Jaccard plus expiration
  proximity; only matches at or above `min_match_confidence` (0.85) can
  produce opportunities, with manual force-match/force-exclude
  overrides in `configs/match_overrides.yaml`.
- **Dashboard.** The React dashboard defaults to the simulated fixture
  (`SIMULATED FIXTURE DATA` banner). `scripts/export_live_report.py`
  renders the scan ledger as `live-report.json`; when present, the
  dashboard offers a "Live paper scans" mode with a `LIVE PAPER DATA`
  banner. Both modes show the P&L attribution waterfall.

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

- **Kalshi adapter normalizes ladders to bids** (ADR-0001, verified 2026-09-18). A 2026-09-25 live sample hit illiquid parlay markets whose books were genuinely empty (no resting orders); use `--min-volume-24h` to scan liquid markets.
- **Polymarket `base_fee / 10000` verified** — the official CLOB OpenAPI spec defines `base_fee` as basis points; conversion confirmed 2026-09-18 (per-token taker rate, takers only, makers pay 0).
- **AI may propose matches but cannot authorize trades** — matching suggestions are advisory; there is no autonomous order path.
- **No real-money order path exists** — paper execution only, by design.
- **Latency drift is a preliminary fit** (n = 29 moves, 15 Polymarket markets, 2026-09-25; labeled `preliminary` everywhere it is used). Kelly `p_win` remains an *assumption* (no live executions to fit on). Empirical slippage coefficients are still placeholders. See ADRs 0012 and 0013.
- Nothing here is investment advice.

## Repository layout

```
backend/
  schemas.py        # normalized Pydantic models (venue-independent)
  config.py         # loads configs/*.yaml
  markets/          # MarketDataAdapter ABC + Polymarket/Kalshi adapters
                    # live.py: shared threaded polling, liquidity filter
                    # snapshot_store.py: append-only SQLite snapshot history
  arbitrage/        # costs.py, opportunities.py, sizing.py, settings.py
                    # calibration.py: empirical fits with provenance labels
                    # matching.py: scored cross-venue market matching
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
  calibration.yaml  # empirical fits (preliminary), written by scripts/fit_calibration.py
  match_overrides.yaml  # manual cross-venue match force/deny list
data/raw|processed/ # snapshots (gitignored, regenerable)
data/live/          # scan ledger, observations (gitignored, regenerable)
docs/               # api-research.md, architecture.md, adr/
scripts/            # scan_live.py: two-venue paper scan loop -> SQLite ledger
                    # collect_latency_snapshots.py / fit_calibration.py: calibration pipeline
                    # collect_snapshot_history.py: live snapshot collector
                    # export_live_report.py: ledger -> dashboard live-report.json
                    # make_dashboard_fixture.py: engine -> dashboard fixture
tests/              # adapter + engine tests, labeled fixtures
frontend/           # dashboard/: React + Sass replay dashboard (fixture + live-paper modes)
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
- [x] Phase 11: full test suite — 305 tests, all passing
- [ ] Phase 12: AI-assisted market matching (advisory only)
- [ ] Phase 13: documentation

## Limitations

- Live public data flows through both venue adapters; no credentials needed for market data. Kalshi production rejected this datacenter IP (HTTP 403); the adapter defaults to the demo environment (see docs/api-research.md).
- Fee schedules change; `configs/fees.yaml` is a dated snapshot and the engine resolves live rates where the venue allows.
- Cross-venue pairs can differ in settlement rules, tick sizes, and expiration semantics; matching on price alone would be wrong.
- Paper fills are simulations. Nothing here is investment advice and no real-money trading is implemented.
