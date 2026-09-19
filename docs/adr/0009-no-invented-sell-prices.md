# ADR-0009: Direct cross-venue legs require real sell-side bids

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

Cross-venue *direct* arbitrage buys on venue A's ask and sells on venue
B's bid when `bid_B > ask_A`. The sell leg needs a genuine resting bid —
unlike the complement strategy (buy YES on A + buy NO on B), which only
needs asks. Before the 2026-09-18 adapter fix, Kalshi books carried no
bids at all, so a naive detector would have had to invent a sell price
(e.g. mid, or the complement of the ask) to run this strategy.

## Decision

`detect_cross_venue_direct` returns `None` (flag
`NO_BIDS_FOR_DIRECT_LEG`) when the sell venue publishes no bids. It never
synthesizes a sell price from mids, complements, or models. Since the
Kalshi adapter fix (ADR-0001) Kalshi books do carry bids when liquidity
exists, so the strategy works on real quotes where they exist and stays
silent where they don't.

## Consequences

- The engine misses direct opportunities on venues that genuinely
  publish no bids, rather than trading against imagined liquidity. The
  complement strategy still covers the YES+NO < $1 case on asks alone.
- Any future "implied bid" model would need its own ADR and explicit
  labeling — invented quotes are a data-integrity boundary, not a
  convenience.
