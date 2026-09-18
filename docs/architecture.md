# Architecture

## Pipeline

```
Venue APIs (public reads, no auth)
        |
        v
MarketDataAdapter  (backend/markets/base.py)
  PolymarketAdapter | KalshiAdapter
        |
        v
Normalized schemas  (backend/schemas.py)
  Market, OrderBook, Opportunity, PaperTrade
        |
        v
Arbitrage detection  (backend/arbitrage/)
  Strategy A: bundle arbitrage   (YES ask + NO ask < 1)
  Strategy B: cross-venue match  (deterministic, then NLP-assisted)
        |
        v
Transaction-cost model  (configs/fees.yaml + strategy.yaml)
  raw edge -> fees -> spread -> slippage -> latency -> NET EDGE
        |
        v
Risk controls  (backend/risk/)
  position / exposure / loss / latency / confidence gates
        |
        v
Paper execution  (backend/execution/)
  simulated fills against order-book constraints; NO real orders
        |
        v
Persistence + replay  (backend/database/, backend/backtesting/)
        |
        v
Dashboard  (backend/api/ + frontend/, Phase 10)
```

## Design principles

1. **Venue independence.** Everything downstream of the adapters works on
   normalized schemas. Adding a third venue means writing one adapter.
2. **Traceable costs.** Every dollar in a cost breakdown resolves to a config
   value plus order-book arithmetic. Placeholders are labeled as placeholders.
3. **No look-ahead.** The backtester only exposes information timestamped at
   or before the decision point.
4. **Paper only.** There is no code path that submits a real order. The
   execution engine simulates fills; credentials in `.env` are optional and
   unused by the pipeline.
5. **AI as advisor, never authority.** The NLP matcher (Phase 12) proposes
   candidate pairs with confidence scores. Deterministic validation, risk
   controls, and paper execution stay fully rule-based.
