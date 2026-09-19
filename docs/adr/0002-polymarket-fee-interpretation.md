# ADR-0002: Polymarket fee interpretation (base_fee in basis points)

- **Status:** Accepted (verified 2026-09-18)
- **Date:** 2026-09-18

## Context

Polymarket taker fees are nonlinear in price
(`fee = C × feeRate × (p × (1−p))^exponent`, peaking at p = 0.50) and vary
by market category. The live per-token rate comes from
`GET /fee-rate?token_id=`, which returns `{"base_fee": <int>}`. The
engine needs an unambiguous conversion from that integer to a decimal
rate, plus a policy for when the live value is unavailable.

## Decision

1. **Conversion:** `base_fee / 10000` is the decimal taker fee rate. The
   official CLOB OpenAPI spec defines `FeeRate.base_fee` as "Base fee in
   basis points" (https://docs.polymarket.com/api-spec/clob-openapi.yaml),
   so this is a unit conversion, not an estimate. Verified 2026-09-18
   against the spec and a live call (`base_fee: 1000` → 0.10).
2. **Scope:** the rate is **per token, applied at match time to taker
   fills only; makers pay 0**
   (https://docs.polymarket.com/trading/fees). The engine therefore never
   charges maker fees on Polymarket (`FeeModel.maker_fee` returns 0.0).
3. **Freshness:** the adapter resolves the rate live and caches it for 6
   hours (`_FEE_CACHE_TTL_SECONDS`). On failure it falls back to the
   Gamma-provided `takerBaseFee` for the same token.
4. **Unknown rates are labeled, never silent:** when no rate is known at
   all, the cost model uses a 0.05 fallback and tags the opportunity
   `FEE_RATE_FALLBACK` (`POLYMARKET_FEE_VERIFIED` marks live-resolved
   rates).

Observed coarse values (0, 1000) can differ from the documented
per-category table — the docs warn rates drift, so **live resolution
wins over the table**, and the table is never hardcoded.

## Consequences

- Fees are evaluated at the actual VWAP execution price per leg, not as a
  flat rate, which the price-nonlinearity requires.
- A 6-hour TTL bounds staleness but does not eliminate it; a mid-window
  fee change is absorbed as fee-estimation risk (part of why
  `arbitrage_p_win` is 0.99, not 1.0).
- The `base_fee / 10000` conversion is no longer provisional; the
  backwards-compat alias `POLYMARKET_FEE_PROVISIONAL` exists only so old
  labels still parse.
