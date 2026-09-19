# ADR-0001: Kalshi bid normalization (bids-only book, asks derived)

- **Status:** Accepted (verified 2026-09-18)
- **Date:** 2026-09-18

## Context

The Kalshi public order-book endpoint does not return a symmetric
bids/asks ladder. Per the official OpenAPI spec
(https://docs.kalshi.com/openapi.yaml, `GET /markets/{ticker}/orderbook`):

> "The order book shows all active bid orders for both yes and no sides
> of a binary market. It returns **yes bids and no bids only (no asks
> are returned)**."

The spec also documents the pricing equivalence:

> "a bid for yes at price X is equivalent to an ask for no at price
> (100-X) ... with identical contract sizes", because a yes order and a
> no order whose prices sum to $1 form a trade.

An earlier version of the adapter mislabeled the ladders as asks and left
bids empty, which made `spread`/`mid_price` unavailable and misrepresented
executable quotes.

## Decision

The Kalshi adapter (`backend/markets/kalshi.py::normalize_order_book`)
normalizes each side's ladder to **BIDS** — the YES ladder becomes the
YES bids, the NO ladder becomes the NO bids. The ask side is **derived**,
never fetched: for a given outcome, each opposite-side bid at price `p`
becomes an ask at `round(1.0 - p, 4)` with the identical size, matching
Kalshi's fixed-point dollar precision. Buying YES immediately means
crossing the resting NO bids at `(1 − p)`, which is exactly what the
derived ask represents.

Both envelope variants are accepted with the same bids-only semantics:
the classic cents envelope (`{"orderbook": {"yes": [[cents, count]]}}`,
cents divided by 100) and the floating-point dollars envelope observed on
the demo host (`{"orderbook_fp": {"yes_dollars": [...]}}`).

## Consequences

- Kalshi books now carry real bids, so `spread` and `mid_price` are
  available whenever a side has liquidity, and cross-venue direct legs
  can find genuine sell-side quotes.
- The derived asks are *implied* liquidity, not resting ask orders; any
  consumer that needs to distinguish them must check the venue. The
  schema does not currently tag implied levels (open item).
- Cents are divided by 100 once, at the adapter boundary; everything
  downstream works in dollars and never sees integer cents.
