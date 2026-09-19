# ADR-0008: Kalshi adapter defaults to the demo environment

- **Status:** Accepted (observed 2026-09-18; re-verify before live use)
- **Date:** 2026-09-18

## Context

Kalshi offers production (`external-api.kalshi.com`) and demo
(`external-api.demo.kalshi.co`) hosts. On 2026-09-18, production returned
HTTP 403 to this datacenter IP for both `curl` and Python clients, while
the demo host worked. Public market-data endpoints need no credentials
on either host, so this is an egress/network policy, not an auth
problem. See `docs/api-research.md` for the live-verification log.

## Decision

`KalshiAdapter` defaults to `env="demo"`. A production 403 raises a
descriptive `VenueError` ("Kalshi production rejected this network
(HTTP 403). Use env='demo' or run from non-datacenter egress.") instead
of a bare status code, so the failure mode is diagnosable at a glance.
Demo markets observed on 2026-09-18 carried no liquidity (50 sampled,
zero with quotes), so adapter behavior on real ladders is covered by
fixture tests only, labeled as such.

## Consequences

- Out-of-the-box runs work without production access; anyone with
  non-datacenter egress can opt into `env="prod"` explicitly.
- Demo order books are not representative of production liquidity —
  detection results on demo data say nothing about live opportunity
  frequency (and per ADR-0007 they are labeled accordingly).
- This default must be re-verified before any live-adjacent use
  (Phase 2 open item).
