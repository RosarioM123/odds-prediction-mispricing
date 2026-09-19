# ADR-0011: Placeholder assumptions are labeled, never silent

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

Several model inputs have no calibrated value yet: the Polymarket taker
fee rate when the live endpoint is unreachable, the adverse price drift
used for the latency cost, and the win probability fed to Kelly sizing.
The dangerous failure is not a wrong placeholder — it's a placeholder
that later readers mistake for a measured parameter.

## Decision

Every uncalibrated assumption is **labeled at the point of use** and its
placeholder status lives in `configs/strategy.yaml` next to the value:

| Assumption | Value | Label | Notes |
|---|---|---|---|
| Unknown taker fee rate | 0.05 | `FEE_RATE_FALLBACK` | per opportunity leg (`FeeModel.resolve_taker_rate`) |
| Latency adverse drift | 0.002 $/s | `LATENCY_DRIFT_PLACEHOLDER` | `drift_is_placeholder: true` in strategy.yaml |
| Kelly edge probability | 0.55 | `P_WIN_PLACEHOLDER` | `size_position(..., p_win_is_placeholder=True)`; replay uses `arbitrage_p_win: 0.99` for locked pairs — documented in strategy.yaml as an *assumption, not a calibrated model* |

Labels propagate into opportunity explanations and replay
`config_notes`, so any report built on placeholders says so.

## Consequences

- Sensitivity analysis is the intended next step (Phase 9+): sweep the
  placeholders and watch net edge move, rather than trusting any single
  value.
- A placeholder can only graduate to a real parameter by replacing the
  label with a calibration reference — the label's presence in reports
  makes silent graduation impossible.
