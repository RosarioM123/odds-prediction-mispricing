"""Collect timestamped live order-book observations for calibration.

Polls the public read-only Polymarket (Gamma + CLOB) and Kalshi endpoints
for several rounds, recording per-book fetch latency and quote state. Two
outputs:

  data/live/latency_observations.jsonl  one row per book fetch, used to fit
                                        the empirical latency-drift model
  data/live/snapshots/round{R}-{venue}.json
                                        odds_snapshot_version 1 files, labeled
                                        "live", replayed to calibrate the
                                        Kelly win probability from real
                                        paper-execution outcomes

No credentials, no orders. Public market-data reads only, well under venue
rate limits (Polymarket ~4000 reads/10s, Kalshi ~200 reads/s per the
documented tiers; this script issues roughly one request per second).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, ".")

from backend.errors import VenueError  # noqa: E402
from backend.markets.kalshi import KalshiAdapter  # noqa: E402
from backend.markets.polymarket import PolymarketAdapter  # noqa: E402
from backend.schemas import OrderBook, Venue  # noqa: E402

FETCH_GAP_SECONDS = 0.4  # politeness gap between book fetches


def _book_stats(book: OrderBook) -> dict:
    asks = book.asks[:5]
    bids = book.bids[:5]
    depth = sum(lvl.size for lvl in asks) + sum(lvl.size for lvl in bids)
    return {
        "best_bid": book.best_bid,
        "best_ask": book.best_ask,
        "n_ask_levels": len(book.asks),
        "n_bid_levels": len(book.bids),
        "top5_depth_contracts": round(depth, 4),
    }


def _kalshi_adapter() -> KalshiAdapter:
    """Production Kalshi if reachable from this network, else demo."""
    try:
        adapter = KalshiAdapter(env="prod")
        adapter.fetch_markets(limit=1)
        print("kalshi: using production host", flush=True)
        return adapter
    except VenueError as exc:
        print(f"kalshi: prod unreachable ({exc}); falling back to demo", flush=True)
        return KalshiAdapter(env="demo")


def collect_round(
    round_no: int,
    poly: PolymarketAdapter,
    kalshi: KalshiAdapter,
    poly_limit: int,
    kalshi_limit: int,
    out_dir: Path,
    obs_fh,
) -> None:
    """One collection round for both venues."""
    for venue_name, adapter, limit in (
        ("polymarket", poly, poly_limit),
        ("kalshi", kalshi, kalshi_limit),
    ):
        venue = Venue.POLYMARKET if venue_name == "polymarket" else Venue.KALSHI
        try:
            markets = adapter.fetch_markets(limit=limit)
        except VenueError as exc:
            print(f"round {round_no} {venue_name}: market list failed: {exc}", flush=True)
            continue
        books: list[OrderBook] = []
        captured = datetime.now(UTC)
        for market in markets:
            t0 = time.perf_counter()
            try:
                book = adapter.fetch_order_book(market)
            except VenueError as exc:
                print(f"round {round_no} {venue_name}: book failed: {exc}", flush=True)
                continue
            latency_s = time.perf_counter() - t0
            books.append(book)
            obs = {
                "round": round_no,
                "venue": venue_name,
                "market_id": market.market_id,
                "question": market.question,
                "fetch_latency_s": round(latency_s, 4),
                "received_at": book.received_timestamp.isoformat(),
                **_book_stats(book),
            }
            obs_fh.write(json.dumps(obs) + "\n")
            obs_fh.flush()
            time.sleep(FETCH_GAP_SECONDS)
        snap = {
            "odds_snapshot_version": 1,
            "label": "live",
            "venue": venue.value,
            "captured_at": captured.isoformat(),
            "markets": [m.model_dump(mode="json") for m in markets],
            "order_books": [b.model_dump(mode="json") for b in books],
        }
        path = out_dir / "snapshots" / f"round{round_no}-{venue_name}.json"
        path.write_text(json.dumps(snap))
        print(
            f"round {round_no} {venue_name}: {len(markets)} markets, "
            f"{len(books)} books -> {path.name}",
            flush=True,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--gap", type=int, default=60, help="seconds between rounds")
    parser.add_argument("--poly-limit", type=int, default=15)
    parser.add_argument("--kalshi-limit", type=int, default=15)
    parser.add_argument("--out", default="data/live")
    args = parser.parse_args()

    out_dir = Path(args.out)
    (out_dir / "snapshots").mkdir(parents=True, exist_ok=True)

    poly = PolymarketAdapter()
    kalshi = _kalshi_adapter()

    with open(out_dir / "latency_observations.jsonl", "a") as obs_fh:
        for r in range(1, args.rounds + 1):
            collect_round(r, poly, kalshi, args.poly_limit, args.kalshi_limit, out_dir, obs_fh)
            if r < args.rounds:
                print(f"sleeping {args.gap}s before round {r + 1}", flush=True)
                time.sleep(args.gap)
    print("done", flush=True)


if __name__ == "__main__":
    main()
