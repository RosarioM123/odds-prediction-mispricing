# ADR-0004: Reject non-finite order-book sizes at the schema boundary

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

Order-book levels arrive as venue JSON strings parsed to floats. A
malformed venue payload (or a hand-built fixture) can produce `NaN` or
`Infinity` sizes. Because the cost model sums sizes (`bid_depth`,
`ask_depth`, `walk_book` notional) and divides by them (VWAP), a single
non-finite size would silently poison every downstream number — depth,
sizing, slippage, and net edge — without raising.

## Decision

`OrderBookLevel.size` is declared `Field(ge=0.0, allow_inf_nan=False)` in
`backend/schemas.py`. Construction of a level with a NaN or infinite size
raises a Pydantic `ValidationError` at the adapter boundary, before any
arithmetic runs. Price is likewise bounded (`ge=0.0, le=1.0`).

## Consequences

- Corrupt books fail fast and loudly at ingestion instead of producing
  plausible-looking wrong edges — a fail-closed data-quality posture.
- Adapters must sanitize before constructing levels if they want to
  tolerate bad venue data; currently they don't, so one bad level drops
  the whole book (a deliberate strictness; a future `SKIP_BAD_LEVELS`
  policy would need its own ADR).
- The 50 edge-case tests added with this change pin the behavior.
