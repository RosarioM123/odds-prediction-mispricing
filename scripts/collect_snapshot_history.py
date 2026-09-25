"""Snapshot history collector: build the historical dataset.

Polls both venues every --interval seconds and appends version-1
snapshots (label "live") to a SQLite store (default
data/live/snapshots.db, gitignored runtime data). The store feeds
``ReplayEngine`` for point-in-time backtests with no lookahead.

Public reads only, paper-research use. Interrupt with Ctrl-C.
"""

from __future__ import annotations

import argparse
import sys
import time

sys.path.insert(0, ".")

from backend.errors import VenueError  # noqa: E402
from backend.markets.live import poll_venue, snapshot_payload, venue_adapters  # noqa: E402
from backend.markets.snapshot_store import SnapshotStore  # noqa: E402
from backend.schemas import Venue  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval", type=int, default=300, help="seconds between rounds")
    parser.add_argument("--rounds", type=int, default=0, help="0 = run forever")
    parser.add_argument("--poly-limit", type=int, default=25)
    parser.add_argument("--kalshi-limit", type=int, default=25)
    parser.add_argument("--store", default="data/live/snapshots.db")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--min-volume-24h",
        type=float,
        default=0.0,
        help="only store markets with >= this 24h volume in USD (0 disables)",
    )
    args = parser.parse_args()

    store = SnapshotStore(args.store)
    adapters = venue_adapters()
    print(f"store: {args.store}", flush=True)

    round_no = 0
    while True:
        round_no += 1
        for venue_name, limit in (("polymarket", args.poly_limit), ("kalshi", args.kalshi_limit)):
            venue = Venue.POLYMARKET if venue_name == "polymarket" else Venue.KALSHI
            try:
                markets, books, stats = poll_venue(
                    venue_name,
                    adapters[venue_name],
                    limit,
                    max_workers=args.workers,
                    min_volume_24h=args.min_volume_24h,
                )
            except VenueError as exc:
                print(f"round {round_no} {venue_name}: failed: {exc}", flush=True)
                continue
            payload = snapshot_payload(venue, markets, books)
            row_id = store.append(payload)
            print(
                f"round {round_no} {venue_name}: {len(books)} books stored (row {row_id})",
                flush=True,
            )
        if args.rounds and round_no >= args.rounds:
            break
        time.sleep(args.interval)
    stats = store.stats()
    print(f"done: {stats['n_snapshots']} snapshots, {stats['first']} to {stats['last']}")
    store.close()


if __name__ == "__main__":
    main()
