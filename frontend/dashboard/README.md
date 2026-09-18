# ODDS Dashboard (React + Sass)

Paper-trading replay dashboard for the ODDS quantitative engine. It renders
a **replay report fixture** produced by the real engine
(`scripts/make_dashboard_fixture.py`) — every number on screen comes from
`src/data/replay-report.json`, which is labeled simulated.

## What's shown

- Summary cards: snapshots, opportunities, executed/rejected, expected vs
  realized net P&L, hit rate (flagged "n too small" below 30 samples)
- Honesty flags panel: mixed-label and sample-size warnings from the engine
- Opportunities table: click a row to expand per-opportunity detail

A persistent banner states the data is simulated. **Paper trading only —
no real money, no live orders, ever.**

## Develop

```bash
cd frontend/dashboard
npm install
npm run dev      # local dev server
npm run build    # production build -> dist/
```

## Regenerating the fixture

```bash
# from the repo root, with the Python venv active
.venv/bin/python scripts/make_dashboard_fixture.py
```

This runs `ReplayEngine` over hand-built labeled scenarios and rewrites
`src/data/replay-report.json`. Rebuild afterwards.
