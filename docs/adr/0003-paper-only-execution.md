# ADR-0003: Paper-only execution (no real-money order path, by design)

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

This engine detects arbitrage on real venue APIs. The natural next step
— submitting orders — would require authenticated endpoints (Polymarket
L1/L2 HMAC wallet signing; Kalshi `KALSHI-ACCESS-*` RSA headers),
credential management, and real-money risk. The project's scope is
*research*: mispricing detection, cost modeling, and simulated execution.

## Decision

- **The codebase contains no real-money order path.** The HTTP client
  (`backend/markets/http.py`) never attaches credentials; the adapters
  call only public market-data endpoints. Authenticated endpoints
  (order placement, account reads) are never called.
- `backend/execution/paper.py` simulates fills against book snapshots;
  credentials in `.env` are optional and unused by the pipeline.
- Opportunity `decision` is one of `PAPER_EXECUTE | REJECTED | PENDING` —
  there is no `EXECUTE` state that could be misread as a live order.
- **AI proposes, never authorizes.** The future NLP market matcher
  (Phase 12, `backend/models/`) may *propose* candidate cross-venue pairs
  with confidence scores, but only pairs passing the deterministic gate
  (≥ `min_match_confidence`) reach the risk engine, and the risk engine
  and paper broker stay fully rule-based. No model output can place,
  approve, or alter a trade.

## Consequences

- Adding live trading is not a configuration change; it would require a
  new execution module, credential flows, and explicit re-architecture —
  deliberately a large, reviewable diff rather than a flag flip.
- All P&L figures in reports are simulated; the README, architecture doc,
  and every replay report carry honesty flags to that effect, and the
  30-day trading experiment has not been run.
