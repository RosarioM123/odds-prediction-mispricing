# Architecture

> ⚠️ **Honesty flags (kept current):** this is a **research engine** over
> **simulated and labeled data**, paper execution only. No real-money order
> path exists anywhere in the codebase. All replay snapshots in this repo
> are labeled `ALL_INPUT_SIMULATED` and replays flag `SAMPLE_TOO_SMALL`.
> Nothing here is live trading performance. The 30-day trading experiment
> has **not** been run — this is the software, not the trading.

## Pipeline

```
Venue APIs (public reads, no auth)
        |
        v
MarketDataAdapter  (backend/markets/base.py)
  PolymarketAdapter | KalshiAdapter
        |
        v
Normalized schemas  (backend/schemas.py)
  Market, OrderBook, Opportunity, PaperTrade
        |
        v
Arbitrage detection  (backend/arbitrage/opportunities.py)
  Strategy A: bundle arbitrage   (YES ask + NO ask < 1)
  Strategy B: cross-venue match  (deterministic, then NLP-assisted)
        |
        v
Transaction-cost model  (backend/arbitrage/costs.py + configs/fees.yaml)
  raw edge -> fees -> slippage -> latency -> NET EDGE
  (spread is informational only; see ADR-0005)
        |
        v
Position sizing  (backend/arbitrage/sizing.py)
  fractional Kelly, hard-capped by liquidity and max position
        |
        v
Risk controls  (backend/risk/gates.py)
  position / exposure / loss / latency / confidence gates
        |
        v
Paper execution  (backend/execution/paper.py)
  simulated fills against order-book constraints; NO real orders
        |
        v
Replay  (backend/backtesting/replay.py)
  chronological detect -> size -> gate -> execute, no look-ahead
        |
        v
Dashboard  (backend/api/ + frontend/, Phase 10; renders SIMULATED banner)
```

## Module dependency map

Dependencies point **downward only**: a module may import modules below it,
never above or beside it in a cycle. `schemas` and `config` are the
foundation; everything imports them, nothing inside `backend/` imports the
layers above.

```
                        +---------------------------+
                        |  backtesting/replay       |   orchestrator: detect -> size
                        |  (ReplayEngine)           |   -> gate -> execute, in order
                        +-------------+-------------+
                                      | imports
              +-----------------------+-----------------------+
              |                       |                       |
              v                       v                       v
   +----------------+      +----------------+      +----------------+
   | execution/paper|      |  risk/gates    |      | arbitrage/     |
   | (PaperBroker)  |      |  (RiskGate)    |      | opportunities  |
   +--------+-------+      +--------+-------+      | sizing         |
            |                       |              +--------+-------+
            |    +------------------+                       |
            |    |                                          |
            v    v                                          v
   +----------------+                            +----------------+
   | arbitrage/costs|                            |arbitrage/settings|
   | (FeeModel,     |                            | (StrategyConfig) |
   |  walk_book)    |                            +--------+---------+
   +--------+-------+                                     |
            |            +----------------+               |
            |            |  markets/      |               |
            |            |  polymarket    |               |
            v            |  kalshi        |               v
   +----------------+    |  snapshots     |      +----------------+
   |   schemas      |<---+  http, base    |      |  config        |
   | (pydantic      |    +----------------+      | (YAML loading) |
   |  boundary)     |                            +----------------+
   +----------------+
              ^
   database/  |  models/  |  api/  -- docstring-only stubs (Phase 2/10/12),
              |           |          import nothing yet
```

Allowed directions (verified against actual imports, 2026-09-18):

| Module | May import (below or beside) |
|---|---|
| `backtesting/replay` | `arbitrage/*`, `execution`, `risk`, `schemas` |
| `execution/paper` | `arbitrage/costs`, `arbitrage/settings`, `schemas` |
| `risk/gates` | `arbitrage/opportunities`, `arbitrage/settings`, `schemas` |
| `arbitrage/opportunities` | `arbitrage/costs`, `arbitrage/settings`, `schemas` |
| `arbitrage/costs`, `arbitrage/sizing` | `schemas` only |
| `arbitrage/settings` | `backend.config` only |
| `markets/*` | `markets/base`, `markets/http`, `schemas` only |
| `schemas`, `config` | nothing inside `backend/` (foundation) |
| `api`, `database`, `models` | stubs; import nothing yet |

### Dependency-direction violations

**None hard (the graph is acyclic).** One coupling is flagged as a watch
item rather than a violation:

- `risk/gates.py` imports `book_age_seconds` from
  `arbitrage/opportunities.py` — the *detection* module, not a neutral
  utility. Logically, risk gates and detectors are peer layers; the gate
  reaching into the detector's module means a refactor of detection
  helpers can break risk. If more shared book-timestamp utilities appear,
  extract them to a neutral `arbitrage/book_utils.py` (or a `core/`
  package) so `risk` and `arbitrage` depend on a common leaf instead of
  on each other. `execution/paper.py` importing `arbitrage/costs` and
  `arbitrage/settings` is the same shape and is acceptable: costs and
  settings are library-style modules with no upward imports.

## Modules: responsibility and invariants

Each module owns one invariant — the property that must hold no matter
what the inputs look like. Tests pin these; the invariant is the
contract, not the implementation.

| Module | Responsibility | Invariant it guarantees |
|---|---|---|
| `backend/schemas.py` | Pydantic boundary: every venue payload enters the engine as `Market`/`OrderBook`/`Opportunity`/`PaperTrade`. | **Best-first sort order is enforced at the boundary** (bids descending, asks ascending — construction raises otherwise); **no non-finite level size** (`allow_inf_nan=False`); prices in [0, 1]. Downstream code never re-sorts and never guards NaN. |
| `backend/config.py` | Loads `configs/*.yaml`; `APP__SECTION__KEY` env overrides. | **Every quantitative assumption is visible in YAML**, never buried in code; a missing config file fails loudly at load. |
| `backend/markets/base.py` | `MarketDataAdapter` ABC: `fetch_markets`, `fetch_order_book`, `normalize_*`, `resolve_taker_fee_rate`. | **Downstream code depends only on this interface + schemas** — adding a venue means writing one adapter, touching nothing else. |
| `backend/markets/http.py` | Shared GET/JSON client: timeouts, retries with backoff, typed `VenueError`/`RateLimitError`. | **No credentials are ever attached** (all endpoints used are public); 429 raises a distinct error type so callers can back off. |
| `backend/markets/polymarket.py` | Gamma discovery + CLOB books, per-token fee resolution. | **Worst-first CLOB ladders are re-sorted to best-first** before schema construction; **taker fee rate = `base_fee / 10000`** resolved live with a 6h TTL cache, Gamma value as labeled fallback (see ADR-0002). |
| `backend/markets/kalshi.py` | Demo/prod discovery + order books; per-trade formula fees. | **Ladders normalize to BIDS only; asks are the documented `1 - bid` complement of the opposite side's bids** at Kalshi fixed-point precision (see ADR-0001). Never invents an ask ladder from nothing. |
| `backend/markets/snapshots.py` | Save/load labeled market snapshots for offline replay. | **Every snapshot carries a label** (`live` | `simulated`); an invalid label raises. Simulated data can never be mistaken for live data downstream. |
| `backend/arbitrage/costs.py` | Cost waterfall: fees (venue formulas), VWAP slippage via `walk_book`, latency drift. | **Every cost dollar traces to config + book arithmetic**; **spread is informational only, never subtracted** (raw edge is measured at the ask — subtracting would double-count; see ADR-0005). Unknown fee rates fall back to 0.05 with a `FEE_RATE_FALLBACK` label, never silently. |
| `backend/arbitrage/opportunities.py` | Three detectors: bundle arb, cross-venue direct, cross-venue complement; deterministic question matching. | **Never emits a negative net edge** (detectors return `None` below `min_net_edge`); **never invents a quote** — a direct leg with no real sell-side bids is skipped (`NO_BIDS_FOR_DIRECT_LEG`), not priced; duplicates deduped to newest; stale books rejected. |
| `backend/arbitrage/sizing.py` | Fractional Kelly sizing (`f* = (b·p − q)/b`, floored at 0), capped by liquidity and max position. | **Quantity is 0 whenever Kelly says don't bet** (non-positive fraction → `kelly_zero`); Kelly fraction is restricted to the configured set {0.25, 0.50, 1.00}; caps apply in a fixed order (liquidity, then max position). |
| `backend/arbitrage/settings.py` | Typed `StrategyConfig` from `configs/strategy.yaml` — the single source of truth for thresholds. | **Detection, sizing, risk, and replay all read the same parsed config**; no module hardcodes a threshold. |
| `backend/risk/gates.py` | Pure rule gates: net edge, quantity, position, portfolio/venue exposure, daily loss, book age/latency, match confidence. | **A gate rejects by mutating `opportunity.decision` to `REJECTED` with the reason recorded** — no silent passes; every check that passed is also recorded (`checks`). `PAPER_EXECUTE` is only set when all gates pass. |
| `backend/execution/paper.py` | Simulates per-leg fills against book timelines. | **No-look-ahead, structurally**: fills use `latest_book_at(t, fill_at)` — the latest book with timestamp ≤ decision + execution latency (see ADR-0006). **No real-money order path exists** (see ADR-0003). Settlement payout is credited by replay, never here. |
| `backend/backtesting/replay.py` | Chronological `detect → size → gate → execute` over labeled snapshots; builds the `ReplayReport`. | **Snapshots process in timestamp order and labels propagate** — mixed labels are flagged (`MIXED_LABELS`), never blended; **statistics below 30 executed opportunities are refused** (`SAMPLE_TOO_SMALL` — no Sharpe/win-rate claims); unpaired residual legs are carried at cost with no P&L claimed. |
| `backend/api/` | FastAPI service (Phase 10) — stub. | No imports yet; when built, reads only replay reports and labeled fixtures. |
| `backend/database/` | Persistence layer (Phase 2) — stub. | Reserved for snapshots, opportunities, paper trades. |
| `backend/models/` | NLP match models (Phase 12) — stub. | **Advisory only**: may propose candidate pairs with confidence, never authorize a trade (see ADR-0003). |

## Data flow

One opportunity's journey through the engine, with the schema each stage
speaks:

```
venue payload (JSON)
  -- normalize_market / normalize_order_book (adapters) -->
Market, OrderBook                       (schemas.py; best-first, finite)
  -- BookView(market, book, label) -->
dedupe_views, staleness check            (opportunities.py)
  -- detect_bundle_arbitrage /
     detect_cross_venue_direct /
     detect_cross_venue_complement -->
Opportunity { legs, costs: CostBreakdown, liquidity,
              match_confidence, explanation{flags} }
  |  costs built by: FeeModel.taker_fee + walk_book VWAP +
  |                  latency_adjustment   (costs.py)
  -- size_position (fractional Kelly) -->
quantity (0 when Kelly says don't bet)  (sizing.py)
  -- RiskGate.evaluate -->
Opportunity.decision = PAPER_EXECUTE | REJECTED   (risk/gates.py)
  -- PaperBroker.execute (latest_book_at <= decide+latency) -->
PaperTrade[] { FILLED | PARTIALLY_FILLED | MISSED | EXPIRED,
               simulated_fill_price, fees, slippage, net_pnl }
  -- ReplayEngine._settle -->
realized P&L (+$1/contract settlement for paired locked legs;
               residuals at cost, no P&L claimed)
  -- ReplayReport.to_dict -->
report { rows, flags, hit_rate, config_notes }
```

**Label propagation:** the snapshot label (`live` | `simulated`) rides on
`BookView` into each opportunity's `flags` (`LABEL_SIMULATED`), into every
replay row, and into the report. A replay that mixes labels raises
`MIXED_LABELS`; one with no live data raises `ALL_INPUT_SIMULATED`.
Simulated inputs stay labeled simulated all the way through — the engine
cannot silently launder them into live-looking results.

## Design principles

1. **Venue independence.** Everything downstream of the adapters works on
   normalized schemas. Adding a third venue means writing one adapter.
2. **Traceable costs.** Every dollar in a cost breakdown resolves to a config
   value plus order-book arithmetic. Placeholders are labeled as placeholders
   (`FEE_RATE_FALLBACK`, `LATENCY_DRIFT_PLACEHOLDER`, `P_WIN_PLACEHOLDER`).
3. **No look-ahead.** The backtester only exposes information timestamped at
   or before the decision point; the broker's `latest_book_at` enforces this
   structurally, not by convention.
4. **Paper only.** There is no code path that submits a real order. The
   execution engine simulates fills; credentials in `.env` are optional and
   unused by the pipeline.
5. **AI as advisor, never authority.** The NLP matcher (Phase 12) proposes
   candidate pairs with confidence scores. Deterministic validation, risk
   controls, and paper execution stay fully rule-based.
6. **Statistical honesty.** Reports refuse significance claims below the
   sample floor (`MIN_SAMPLE_FOR_STATS = 30`); settlement is credited only
   on paired fills; residuals are carried at cost.

## Further reading

- `docs/api-research.md` — verified venue API behavior (2026-09-18), fee
  formulas, and the live-verification log with remaining open items.
- `docs/adr/` — Architecture Decision Records for the non-obvious choices
  (Kalshi bid normalization, Polymarket fee interpretation, paper-only
  execution, and more).
- `configs/strategy.yaml`, `configs/fees.yaml` — every quantitative
  assumption, version-controlled and human-editable.
