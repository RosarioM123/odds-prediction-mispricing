# ADR-0005: Spread cost is informational, never subtracted

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

The cost waterfall is `raw_edge − fees − slippage − latency = net_edge`,
and the schema also carries a `spread_cost` field (half-spread per taker
leg versus mid). The naive implementation subtracts every listed cost,
but that double-counts: `raw_edge` is measured **at the touch** — e.g.
bundle arb uses `1 − (yes_ask + no_ask)`, and cross-venue direct uses
`bid_B − ask_A`. The half-spread paid to cross from mid to the touch is
already inside those ask/bid prices.

## Decision

`build_cost_breakdown` (`backend/arbitrage/costs.py`) reports
`spread_cost` for explainability but **excludes it from the net-edge
arithmetic**. `net_edge = raw − fees/size − slippage/size − latency/size`,
full stop. When a venue publishes no bids (making mid unknowable), the
field is 0.0 with the `SPREAD_UNAVAILABLE_NO_BIDS` flag rather than an
invented number.

## Consequences

- Net edge is not understated by the spread; subtracting it would have
  rejected genuinely profitable opportunities at the gate.
- Consumers of `CostBreakdown` must read the docstring: `spread_cost` is
  context, not a term. Any future cost added to the waterfall needs an
  explicit ADR arguing it isn't already captured by the touch prices.
