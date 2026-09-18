# Frontend

Planned for Phase 10: a React + TypeScript dashboard served behind the
FastAPI backend in `backend/api/`.

Target sections:

- Overview: opportunities detected/executable, average net edge, simulated
  P&L, fill rate, missed-opportunity rate, average latency, capital deployed.
- Live opportunities table with full cost waterfall per row.
- Order-book viewer: bid/ask depth, spread, VWAP for a chosen size.
- Opportunity detail: raw edge down through fees, spread, slippage, and
  latency to net executable edge.
- Performance: cumulative/daily P&L, win rate, fill rate, opportunity decay.

Nothing here yet. The quantitative engine comes first (Phases 2-9).
