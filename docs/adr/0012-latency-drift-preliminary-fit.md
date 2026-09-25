# ADR-0012: Latency drift is a preliminary empirical fit, not a placeholder

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

The latency cost (`latency_adjustment`) multiplies the execution latency
budget by an adverse-drift rate. Until now the rate was the labeled
placeholder 0.002 $/s (ADR-0011). On 2026-09-25 a three-round live
collection (`scripts/collect_latency_snapshots.py`, 90s between rounds,
15 markets per venue) produced the first empirical observations:
`data/live/latency_observations.jsonl`, 89 rows.

A wrinkle found during fitting: all 45 Kalshi books in the sample were
empty (the venue's top-of-listing markets were illiquid parlay markets
with no resting orders; the adapter parses the `orderbook_fp` envelope
correctly, the ladders are genuinely empty). Drift therefore fits on
Polymarket books only.

## Decision

Fit mean |mid move| per second between consecutive rounds and ship it as
`configs/calibration.yaml` with status **preliminary**: the label, not
just a comment:

- drift = 2.883269574310241e-07 $/s (median 0.0), n = 29 moves over 15
  Polymarket markets, 2026-09-25 to 2026-09-25.
- 27 of 29 moves were exactly zero; the two non-zero moves were 0.0005
  over ~120s. Short-horizon mids on these markets barely move, so the
  fitted rate is near zero, but with n = 29 over a single 6-minute
  window, treating it as a production parameter would be overconfident.
- `StrategyConfig.load()` applies it automatically; every
  `latency_adjustment` call now carries `LATENCY_DRIFT_PRELIMINARY`
  instead of `LATENCY_DRIFT_PLACEHOLDER`.
- The YAML records n, date range, venues, and method so any reader can
  judge the fit. `preliminary: false` is only set by a future fit on a
  longer window (ADR to follow when that happens).

The fitting pipeline is deterministic and re-runnable:
`scripts/fit_calibration.py --live-dir data/live --out
configs/calibration.yaml`.

## Consequences

- Paper scans now use a measured (tiny) drift instead of the
  conservative placeholder; net edges on live scans rise slightly.
  Reports label the provenance, so nobody mistakes the fit for a
  production calibration.
- Unit tests are pinned to the placeholder path via
  `ODDS_NO_CALIBRATION=1` in `tests/conftest.py`; the calibration path
  is covered separately in `tests/test_calibration.py` with synthetic
  fixtures. Ambient `configs/calibration.yaml` can no longer make the
  suite non-deterministic.
- Next step: longer collection windows (and liquid Kalshi markets via
  `--min-volume-24h`) before any claim stronger than "preliminary".
