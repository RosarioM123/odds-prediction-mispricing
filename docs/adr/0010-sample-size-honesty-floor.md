# ADR-0010: Statistical honesty floor (no significance claims below n=30)

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

Replay reports compute hit rate and aggregate P&L. On tiny samples
these numbers are noise, but they *look* authoritative in a report dict
— the easiest way for a research engine to mislead its own author.

## Decision

`ReplayEngine` sets `MIN_SAMPLE_FOR_STATS = 30`. When fewer than 30
opportunities are executed, the report carries the flag:

> `SAMPLE_TOO_SMALL: n_executed=<n> < 30; Sharpe ratio, win rate, and
> significance claims are not meaningful`

`hit_rate` is still reported (as a raw fraction, or `None` when zero
executed) but the flag explicitly disclaims significance. Settlement
accounting stays conservative regardless of sample size: the $1 payout
is credited only on *paired* fills (`_settle`), and unpaired residual
legs are carried at cost with no P&L claimed.

## Consequences

- Reports are self-disclaiming; a reader cannot quote a win rate without
  also seeing the flag next to it.
- 30 is a conventional floor, not a derived one — it guards against
  noise, not against a specific bias. Raising it for noisier strategies
  is a config-level discussion, not an ADR change.
