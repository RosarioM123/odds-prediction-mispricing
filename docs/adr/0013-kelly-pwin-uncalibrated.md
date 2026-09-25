# ADR-0013: Kelly p_win stays an assumption (no executions to fit on)

- **Status:** Accepted
- **Date:** 2026-09-25

## Context

Kelly sizing for locked arbitrage uses `arbitrage_p_win = 0.99`: the
YES+NO pair settles to $1 by construction, so p_win is ~1 minus
execution risk (stale quotes, partial fills, fee mis-estimation). It was
always documented as an assumption, not a calibrated model (ADR-0011,
`configs/strategy.yaml`).

The 2026-09-25 live collection (three rounds, 15 markets per venue, plus
a two-venue paper scan) was the first chance to fit it empirically:
`scripts/fit_calibration.py` replays the live snapshots through
`ReplayEngine` and fits p_win as the fraction of paper-executed locked
arbs with all legs FILLED and no unpaired residual.

## Decision

**Do not fit.** The live sample contained zero paper-executed
opportunities (the scan found no mispricings at the configured
thresholds: a legitimate market outcome, not a bug), so there is
nothing to fit on. Fitting on n = 0 would be fabrication.

- `configs/calibration.yaml` carries no `kelly` section; `p_win_status`
  remains `assumption`, `p_win_n = 0`, and the 0.99 value is unchanged.
- The fitting code path exists and is tested
  (`fit_kelly_pwin`, floored at 0.5 and capped at 0.999 for small
  samples); it activates automatically once the snapshot history
  accumulates real paper executions.
- `scripts/collect_snapshot_history.py` appends live snapshots to SQLite
  for exactly this purpose; a future session re-runs
  `scripts/fit_calibration.py` when executions exist and records the
  graduation in a follow-up ADR.

## Consequences

- Sizing behavior is unchanged from the placeholder era; reports
  continue to say `assumption`, which is the honest label.
- The honest failure mode to watch: if the engine starts executing
  paper trades regularly and nobody re-runs the fit, the assumption
  silently ages. The snapshot collector plus this ADR are the
  mitigation; the fit itself is one command.
