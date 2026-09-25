"""Live scan loop: poll, detect, size, risk-gate, paper-execute.

Polls the public Polymarket and Kalshi endpoints, runs the full
detect -> cost -> size -> gate -> paper-execute pipeline over the live
books, and appends every scan to a local SQLite ledger
(``data/live/ledger.db``, gitignored runtime data).

Paper trading only: public reads, no credentials, no orders placed.
``--once`` runs a single scan and exits; otherwise the loop repeats
every ``--interval`` seconds until interrupted.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from backend.arbitrage.costs import FeeModel  # noqa: E402
from backend.arbitrage.matching import default_overrides  # noqa: E402
from backend.arbitrage.opportunities import BookView, detect_all  # noqa: E402
from backend.arbitrage.settings import StrategyConfig  # noqa: E402
from backend.arbitrage.sizing import size_position  # noqa: E402
from backend.backtesting.replay import ReplayEngine  # noqa: E402
from backend.errors import VenueError  # noqa: E402
from backend.execution.paper import PaperBroker  # noqa: E402
from backend.markets.live import poll_venue, venue_adapters  # noqa: E402
from backend.risk.gates import PortfolioState, RiskGate  # noqa: E402
from backend.schemas import Venue, utcnow  # noqa: E402

_LEDGER_SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    n_markets INTEGER NOT NULL,
    n_books INTEGER NOT NULL,
    n_opportunities INTEGER NOT NULL,
    n_executed INTEGER NOT NULL,
    n_rejected INTEGER NOT NULL,
    fetch_s REAL NOT NULL,
    detect_s REAL NOT NULL,
    total_s REAL NOT NULL,
    kalshi_env TEXT
);
CREATE TABLE IF NOT EXISTS scan_opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_id INTEGER NOT NULL REFERENCES scans(id),
    opportunity_id TEXT NOT NULL,
    detected_at TEXT NOT NULL,
    strategy TEXT NOT NULL,
    net_edge_per_contract REAL NOT NULL,
    quantity REAL NOT NULL,
    decision TEXT NOT NULL,
    expected_net REAL,
    realized_net REAL,
    trade_statuses TEXT NOT NULL,
    notes TEXT NOT NULL
);
"""


def open_ledger(path: str | Path) -> sqlite3.Connection:
    """Open (creating) the scan ledger. Raises: None."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(_LEDGER_SCHEMA)
    return conn


def run_scan(
    config: StrategyConfig,
    fee_model: FeeModel,
    poly_limit: int,
    kalshi_limit: int,
    max_workers: int,
    min_volume_24h: float = 0.0,
) -> dict:
    """One full scan. Returns a summary dict; raises VenueError on venue failure."""
    t_start = time.perf_counter()
    started_at = utcnow().isoformat()
    adapters = venue_adapters()
    views: list[BookView] = []
    timelines: dict = {}
    markets: dict = {}
    n_markets = 0
    kalshi_env = None

    t_fetch = time.perf_counter()
    for venue_name, limit in (("polymarket", poly_limit), ("kalshi", kalshi_limit)):
        adapter = adapters[venue_name]
        venue = Venue.POLYMARKET if venue_name == "polymarket" else Venue.KALSHI
        fetched_markets, books, stats = poll_venue(
            venue_name,
            adapter,
            limit,
            max_workers=max_workers,
            min_volume_24h=min_volume_24h,
        )
        if venue_name == "kalshi":
            kalshi_env = stats.get("kalshi_env")
        print(
            f"  {venue_name}: {stats['n_fetched']}/{stats['n_requested']} books "
            f"({stats['n_liquid']}/{stats['n_listed']} liquid), "
            f"poll {stats['poll_s']}s",
            flush=True,
        )
        n_markets += len(fetched_markets)
        for m in fetched_markets:
            markets[(venue.value, m.market_id, m.outcome.value)] = m
        for b in books:
            key = (venue.value, b.market_id, b.outcome.value)
            timelines.setdefault(key, []).append(b)
            m = markets.get(key)
            if m is not None:
                views.append(BookView(market=m, book=b, label="live"))
    fetch_s = time.perf_counter() - t_fetch

    t_detect = time.perf_counter()
    now = utcnow()
    opportunities, det_flags = detect_all(
        views,
        config=config,
        fee_model=fee_model,
        now=now,
        seed=None,
        overrides=default_overrides(),
    )
    broker = PaperBroker(fee_model, config)
    gate = RiskGate(config)
    portfolio = PortfolioState()
    rows: list[dict] = []
    n_executed = 0
    n_rejected = 0
    for opp in opportunities:
        # Same cost-per-contract convention as the replay engine.
        cost_per_contract = max(0.01, 1.0 - opp.costs.raw_edge + opp.costs.trading_fees)
        sizing = size_position(
            net_edge_per_contract=opp.costs.net_edge,
            cost_per_contract=cost_per_contract,
            liquidity=opp.liquidity,
            bankroll=100.0,
            fraction=config.kelly.fraction,
            max_position=config.risk.max_position_per_market,
            p_win=config.kelly.arbitrage_p_win,
            p_win_status=config.kelly.p_win_status,
            p_win_n=config.kelly.p_win_n,
            p_win_period=config.kelly.p_win_period,
        )
        qty = sizing.quantity
        book_ages = []
        for leg in opp.legs:
            tl = timelines.get((leg["venue"], leg["market_id"], leg["outcome"]), [])
            if tl:
                ts = tl[-1].venue_timestamp or tl[-1].received_timestamp
                book_ages.append((now - ts).total_seconds())
        result = gate.evaluate(opp, qty, portfolio, book_ages_s=book_ages or None, now=now)
        row = {
            "opportunity_id": opp.opportunity_id,
            "detected_at": now.isoformat(),
            "strategy": opp.strategy,
            "net_edge_per_contract": opp.costs.net_edge,
            "quantity": qty,
            "decision": "PAPER_EXECUTE" if result.allow else "REJECTED",
            "expected_net": None,
            "realized_net": None,
            "trade_statuses": [],
            "notes": [result.reason] if not result.allow else list(sizing.notes),
        }
        if result.allow:
            n_executed += 1
            row["expected_net"] = round(opp.costs.net_edge * qty, 6)
            trades = broker.execute(opp, qty, timelines, markets, decide_at=now)
            row["trade_statuses"] = [tr.status.value for tr in trades]
            realized, notes = ReplayEngine._settle(opp.strategy, trades)
            row["realized_net"] = round(realized, 6)
            row["notes"].extend(notes)
            for leg in opp.legs:
                pos_key = (leg["venue"], leg["market_id"])
                portfolio.positions[pos_key] = portfolio.positions.get(pos_key, 0.0) + qty
                portfolio.venue_exposure[leg["venue"]] = (
                    portfolio.venue_exposure.get(leg["venue"], 0.0) + qty
                )
            portfolio.daily_pnl += realized
        else:
            n_rejected += 1
        rows.append(row)
    detect_s = time.perf_counter() - t_detect
    total_s = time.perf_counter() - t_start
    return {
        "started_at": started_at,
        "finished_at": utcnow().isoformat(),
        "n_markets": n_markets,
        "n_books": sum(len(v) for v in timelines.values()),
        "n_opportunities": len(opportunities),
        "n_executed": n_executed,
        "n_rejected": n_rejected,
        "fetch_s": round(fetch_s, 2),
        "detect_s": round(detect_s, 2),
        "total_s": round(total_s, 2),
        "kalshi_env": kalshi_env,
        "detector_flags": det_flags,
        "rows": rows,
    }


def record_scan(conn: sqlite3.Connection, summary: dict) -> int:
    """Append a scan summary and its opportunities. Returns the scan id."""
    cur = conn.execute(
        """INSERT INTO scans (started_at, finished_at, n_markets, n_books,
           n_opportunities, n_executed, n_rejected, fetch_s, detect_s,
           total_s, kalshi_env)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            summary["started_at"],
            summary["finished_at"],
            summary["n_markets"],
            summary["n_books"],
            summary["n_opportunities"],
            summary["n_executed"],
            summary["n_rejected"],
            summary["fetch_s"],
            summary["detect_s"],
            summary["total_s"],
            summary["kalshi_env"],
        ),
    )
    scan_id = int(cur.lastrowid)
    for r in summary["rows"]:
        conn.execute(
            """INSERT INTO scan_opportunities (scan_id, opportunity_id,
               detected_at, strategy, net_edge_per_contract, quantity,
               decision, expected_net, realized_net, trade_statuses, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                scan_id,
                r["opportunity_id"],
                r["detected_at"],
                r["strategy"],
                r["net_edge_per_contract"],
                r["quantity"],
                r["decision"],
                r["expected_net"],
                r["realized_net"],
                json.dumps(r["trade_statuses"]),
                json.dumps(r["notes"]),
            ),
        )
    conn.commit()
    return scan_id


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="single scan, then exit")
    parser.add_argument("--interval", type=int, default=300, help="seconds between scans")
    parser.add_argument("--poly-limit", type=int, default=20)
    parser.add_argument("--kalshi-limit", type=int, default=20)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--min-volume-24h",
        type=float,
        default=0.0,
        help="only scan markets with >= this 24h volume in USD "
        "(venue-native field; 0 disables the filter)",
    )
    parser.add_argument("--ledger", default="data/live/ledger.db")
    args = parser.parse_args()

    config = StrategyConfig.load()
    fee_model = FeeModel()
    conn = open_ledger(args.ledger)
    print(f"ledger: {args.ledger} (paper trading only)", flush=True)

    scan_no = 0
    while True:
        scan_no += 1
        print(f"scan {scan_no} starting", flush=True)
        try:
            summary = run_scan(
                config,
                fee_model,
                args.poly_limit,
                args.kalshi_limit,
                args.workers,
                min_volume_24h=args.min_volume_24h,
            )
        except VenueError as exc:
            print(f"scan {scan_no} failed: {exc}", flush=True)
            if args.once:
                raise SystemExit(1) from exc
            time.sleep(args.interval)
            continue
        scan_id = record_scan(conn, summary)
        print(
            f"scan {scan_no} (id {scan_id}): {summary['n_opportunities']} opportunities, "
            f"{summary['n_executed']} paper-executed, {summary['n_rejected']} rejected; "
            f"fetch {summary['fetch_s']}s, detect {summary['detect_s']}s, "
            f"total {summary['total_s']}s",
            flush=True,
        )
        for flag in summary["detector_flags"]:
            print(f"  flag: {flag}", flush=True)
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
