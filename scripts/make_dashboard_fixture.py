"""Generate a labeled SIMULATED replay report fixture for the dashboard.

Runs the real ReplayEngine over hand-built scenarios: a clean bundle arb,
a no-edge snapshot, a cross-venue complement (Polymarket YES + Kalshi NO),
a stale snapshot, and a second bundle. Output is committed as the
dashboard's fixture data and is labeled simulated everywhere.
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, ".")
sys.path.insert(0, "tests")
from fixtures import T0, kalshi_yes_no, polymarket_pair  # noqa: E402
from test_replay import make_snapshot  # noqa: E402

from backend.backtesting.replay import (  # noqa: E402
    ReplayEngine,
    SnapshotInput,
)
from backend.schemas import Venue  # noqa: E402

snaps = [
    # 1. Clean Polymarket bundle arb.
    make_snapshot(at=T0, yes_ask=0.45, no_ask=0.45, depth=200.0, event_id="evt-1"),
    # 2. No edge: asks sum above $1.
    make_snapshot(
        at=T0 + timedelta(seconds=60), yes_ask=0.55, no_ask=0.55, depth=200.0, event_id="evt-1"
    ),
    # 3. Cross-venue complement: Polymarket YES + Kalshi NO (asks only).
    # 4. Stale snapshot: books 30s older than capture -> rejected.
    make_snapshot(
        at=T0 + timedelta(seconds=180), yes_ask=0.45, no_ask=0.45, depth=200.0, event_id="evt-1"
    ),
]

# Snapshot 3 built manually for the cross-venue pair.
yv, _ = polymarket_pair(yes_ask=0.46, depth=150.0, at=T0 + timedelta(seconds=120), event_id="evt-x")
_, knv = kalshi_yes_no(no_ask=0.44, depth=150.0, at=T0 + timedelta(seconds=120), event_id="evt-x")
# Same normalized question so the deterministic matcher pairs them.
knv.market.question = yv.market.question
cross = SnapshotInput(
    label="simulated",
    venue=Venue.POLYMARKET,
    markets=[yv.market, knv.market],
    books=[yv.book, knv.book],
    captured_at=T0 + timedelta(seconds=120),
)
snaps.insert(2, cross)

# Snapshot 4: stale books.
stale = make_snapshot(
    at=T0 + timedelta(seconds=240),
    yes_ask=0.44,
    no_ask=0.44,
    depth=200.0,
    event_id="evt-1",
    book_at=T0 + timedelta(seconds=240) - timedelta(seconds=30),
)
snaps.append(stale)

report = ReplayEngine(bankroll=100.0).run(snaps)
d = report.to_dict()
print(
    f"executed={d['n_executed']} rejected={d['n_rejected']} realized={d['total_realized_net']:.4f}"
)
print("flags:", d["flags"])

out = Path("frontend/dashboard/src/data/replay-report.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(d, indent=2))
print("wrote", out)
