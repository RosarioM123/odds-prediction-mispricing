# Architecture Decision Records

Non-obvious decisions in this codebase, recorded with context so future
readers know *why*, not just *what*. Format: Status / Context / Decision /
Consequences. New decisions get the next sequential number; superseded
ADRs are marked as such, never deleted.

| ADR | Decision (one line) |
|---|---|
| [0001](0001-kalshi-bid-normalization.md) | Kalshi book returns bids only — asks are derived via the documented `1 − bid` complement of the opposite side. |
| [0002](0002-polymarket-fee-interpretation.md) | Polymarket `base_fee` is basis points → `/10000`; taker-only at match time, makers pay 0; 6h TTL cache with labeled fallback. |
| [0003](0003-paper-only-execution.md) | No real-money order path exists by design; AI proposes, never authorizes. |
| [0004](0004-reject-non-finite-sizes.md) | Non-finite order-book sizes are rejected at the Pydantic schema boundary. |
| [0005](0005-spread-informational-only.md) | Spread cost is reported for explainability, never subtracted from net edge (avoids double-counting the touch). |
| [0006](0006-no-look-ahead-structural.md) | No-look-ahead is enforced by the `latest_book_at` primitive, not by caller discipline. |
| [0007](0007-snapshot-label-propagation.md) | Snapshot labels (`live`/`simulated`) propagate end-to-end; mixed labels are flagged, never blended. |
| [0008](0008-kalshi-demo-default.md) | Kalshi adapter defaults to the demo environment (production 403s datacenter egress, observed 2026-09-18). |
| [0009](0009-no-invented-sell-prices.md) | Direct cross-venue legs require real sell-side bids; the engine never invents a sell price. |
| [0010](0010-sample-size-honesty-floor.md) | No Sharpe/win-rate significance claims below 30 executed opportunities (`SAMPLE_TOO_SMALL`). |
| [0011](0011-placeholder-labeling.md) | Uncalibrated assumptions (fee fallback, latency drift, Kelly p_win) are labeled at point of use, never silent. |
