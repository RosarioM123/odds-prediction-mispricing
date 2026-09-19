# Reproducibility (SEED CONTRACT)

> **Honesty flags.** This engine is a *research* engine: all market data in
> tests, demos, and bundled snapshots is **simulated or hand-built**, every
> execution is **paper-only** (no real orders are ever placed), and snapshot
> labels (`live` / `simulated`) propagate through every report. This document
> describes determinism of the *computation*, not of the data itself.

## The contract

**Same seed + same inputs → identical outputs, including IDs when seeded.**

"Identical" means byte-identical `ReplayReport.to_dict()` JSON (sorted keys).
The guarantee covers the full pipeline: detection → Kelly sizing → risk gates
→ paper execution → settlement accounting.

### Which entry points accept a seed

| Entry point | Seed parameter | Default |
|---|---|---|
| `backend.backtesting.replay.ReplayEngine(...)` | `seed: int \| None` | `DEFAULT_SEED` |
| `backend.arbitrage.opportunities.detect_all(...)` | `seed: int \| None` | `DEFAULT_SEED` |
| `detect_bundle_arbitrage`, `detect_cross_venue_direct`, `detect_cross_venue_complement` | `seed: int \| None` | `DEFAULT_SEED` |

`DEFAULT_SEED = 12345` is defined in `backend/arbitrage/opportunities.py`.

### What the seed controls

When seeded (any `int`), opportunity IDs are deterministic:

```
{strategy}-{venue_tag}-s{seed}-{counter:04d}
```

e.g. `bundle_arbitrage-polymarket-s12345-0001`. The counter is per
`detect_all` call and shared across the bundle and cross-venue loops, so
IDs are unique *within one call* and repeat identically across runs with
the same inputs. `PaperTrade.trade_id` is derived from the opportunity ID
(`{opportunity_id}-leg{i}`), so trades are deterministic too.

When `seed=None`, the engine keeps the pre-2026-09-18 behavior: opportunity
IDs embed the wall-clock time (`strftime("%Y%m%d%H%M%S%f")`). This is the
mode for live runs, where IDs must never repeat across runs.

### What is guaranteed deterministic

- **Detection math** (raw edge, cost waterfall, sizing, gates, settlement
  P&L): pure functions of the inputs. There is **no RNG anywhere in
  `backend/`** — no `random`, `numpy.random`, `secrets`, `uuid`, or
  `os.urandom`. Book level lists are canonicalized (bids/asks sorted
  best-first) and all flag sets are emitted `sorted(...)`, so output never
  depends on set or dict iteration order (dicts iterate in insertion order,
  which follows the input view order).
- **Timestamps that are *quoted* data**: `detected_at` follows the `now`
  argument when callers pass one (the replay engine passes each snapshot's
  `captured_at`), wall clock otherwise. `PaperTrade.timestamp` is the
  deterministic `decide_at + execution_latency`.
- **Replay ordering**: snapshots are sorted by `captured_at` before
  processing; per-leg fills use only books with `timestamp <= fill_at`
  (no-look-ahead).

### What is intentionally NOT deterministic

- **Wall-clock IDs** when `seed=None` (opt-in legacy mode).
- **`received_timestamp` on order books** (`utcnow()` at adapter
  normalization time) and **`captured_at`** on `save_snapshot`: these record
  *when data was captured*, a physical measurement, not a computation. They
  are excluded from equality comparisons in the test suite by design.
- **Network-fetched data**: live venue payloads, fee-rate resolution, and
  the 6h fee cache TTL (`time.time()` in `backend/markets/polymarket.py`)
  are inherently wall-clock/network dependent. A replay from a saved
  snapshot is deterministic; a *live capture* is not.

## How to reproduce a run

1. Start from a fixed snapshot file (or the same in-memory snapshots):
   `load_labeled_snapshot(path)` in `backend/backtesting/replay.py`.
2. Run with an explicit seed (the default already applies):
   ```python
   report = ReplayEngine(bankroll=100.0, seed=12345).run(snapshots)
   ```
   or set `PYTHONHASHSEED` to any value — the suite passes under
   `PYTHONHASHSEED=1` and `PYTHONHASHSEED=2` (verified 2026-09-18).
3. Serialize with `report.to_dict()` and `json.dumps(..., sort_keys=True)`;
   re-running yields byte-identical output.

Changing the seed changes only the ID suffixes (`-s{seed}-`),
never the economics. To get fresh unique IDs per live run, pass
`seed=None`.

## Audit notes (2026-09-18)

Nondeterminism sites found and their disposition:

| Site | Disposition |
|---|---|
| `arbitrage/opportunities.py` `_finalize` ID from `utcnow()` | **Fixed**: seeded per-call counter via `seed` (default `DEFAULT_SEED`); `None` → wall-clock |
| `_finalize` never passed `detected_at` (defaulted to wall clock) | **Fixed**: now forwards `now` when callers provide it |
| `ReplayEngine.run` → `detect_all` (IDs non-deterministic in every replay) | **Fixed**: `ReplayEngine(seed=...)` threads into `detect_all` |
| `execution/paper.py` `decide_at or utcnow()` | Left: test seam; replay passes `decide_at` explicitly |
| `risk/gates.py` `now or utcnow()` | Left: `now` is currently unused by the gate body; harmless |
| `schemas.py` `received_timestamp` / `PaperTrade.timestamp` defaults | Left: receipt-time measurements; callers pass explicit values in deterministic paths |
| Adapters `received_timestamp=utcnow()`, `snapshots.py` `captured_at`, fee-cache `time.time()` | Left: physical capture time / cache TTL, not computation outputs |
| `hash(`, set iteration, dict order | None found: flags are `sorted(set(...))`, config glob is `sorted(...)` |

`detect_*` / `detect_all` / `ReplayEngine` signatures are backward
compatible (new keyword-only params with defaults); `mypy --strict`
reports zero errors and the full pytest suite (188 tests) is green.
