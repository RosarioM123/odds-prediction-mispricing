# ADR-0006: No-look-ahead enforced structurally, not by convention

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

A backtester that can see future quotes will manufacture phantom
profitability — the classic way simulated results lie. Discipline-by-
convention ("remember to only pass old books") rots the moment a new
caller passes the wrong list.

## Decision

The no-look-ahead rule is a **structural property of the code**, in two
places:

1. `PaperBroker.execute` fills each leg against
   `latest_book_at(timeline, fill_at)` where
   `fill_at = decide_at + execution_latency`. The primitive returns the
   latest book with `timestamp ≤ fill_at`, or `None` if the timeline
   starts later. The broker *cannot* express "give me a future book" —
   the function signature has no way to select one.
2. `ReplayEngine.run` processes snapshots sorted by `captured_at` and
   only appends a snapshot's books to the timelines *before* running
   detection at that step, so detection at step *t* sees exactly the
   books with `captured_at ≤ t`.

## Consequences

- Look-ahead bugs would require changing the primitive itself, which is
  small, pure, and directly unit-tested — a much narrower attack surface
  than caller discipline.
- The cost: the broker needs full per-market book timelines, not just
  the latest book, so memory grows with replay length. Acceptable for a
  research engine; a production system would window the timelines.
- `MISSED` fills (stale book at fill time) are the honest price of this
  discipline — they appear in `trades_by_status` instead of being
  papered over with the decision-time quote.
