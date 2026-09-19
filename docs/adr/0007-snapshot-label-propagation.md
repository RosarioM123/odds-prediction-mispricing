# ADR-0007: Snapshot labels propagate; mixed labels are flagged, never blended

- **Status:** Accepted
- **Date:** 2026-09-18

## Context

The engine runs on two kinds of data: quotes captured from venue APIs
(`live`) and hand-built or generated fixtures (`simulated`) used for
tests and demos. Blending them silently would let simulated results be
presented — accidentally or otherwise — as if they came from real
markets.

## Decision

1. Every snapshot file carries a mandatory `label` (`live` |
   `simulated`); `save_snapshot` raises on any other value.
2. The label rides `BookView` → each opportunity's `flags`
   (`LABEL_LIVE` / `LABEL_SIMULATED`) → each replay row → the report's
   `labels` list. Simulated inputs stay labeled simulated all the way
   through.
3. A replay over mixed labels emits `MIXED_LABELS:live,simulated` in the
   report flags; an all-simulated replay emits `ALL_INPUT_SIMULATED`.
   Both are loud, sorted into `flags`, and never opt-out.

## Consequences

- Any dashboard or writeup built on a replay report inherits the label
  trail; the Phase 10 dashboard renders a persistent SIMULATED DATA
  banner from it.
- Strictness costs convenience: a single simulated fixture accidentally
  included in a live replay flags the whole report. That is the intended
  behavior — the flag is cheap, the misrepresentation is not.
