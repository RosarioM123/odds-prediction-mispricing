"""Export the live scan ledger as a dashboard report JSON.

Reads ``data/live/ledger.db`` (written by ``scripts/scan_live.py``) and
writes ``frontend/dashboard/public/live-report.json`` in the same shape
as the replay fixture, so the dashboard can render it in "live paper"
mode. The banner switches from SIMULATED FIXTURE DATA to LIVE PAPER
DATA; the simulated fixture remains the default view.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, ".")

from backend.arbitrage.settings import StrategyConfig  # noqa: E402

MIN_SAMPLE_FOR_STATS = 30


def build_report(ledger_path: str | Path) -> dict | None:
    """Build the report dict, or None when the ledger has no scans."""
    conn = sqlite3.connect(str(ledger_path))
    scans = conn.execute(
        "SELECT id, started_at, finished_at, n_markets, n_books,"
        " n_opportunities, n_executed, n_rejected, total_s, kalshi_env"
        " FROM scans ORDER BY id ASC"
    ).fetchall()
    if not scans:
        return None
    rows = []
    for s in scans:
        scan_id = s[0]
        for r in conn.execute(
            "SELECT opportunity_id, detected_at, strategy,"
            " net_edge_per_contract, quantity, decision, expected_net,"
            " realized_net, trade_statuses, notes"
            " FROM scan_opportunities WHERE scan_id = ? ORDER BY id ASC",
            (scan_id,),
        ):
            rows.append(
                {
                    "opportunity_id": r[0],
                    "strategy": r[2],
                    "label": "live",
                    "detected_at": r[1],
                    "sized_quantity": r[4],
                    "decision": r[5],
                    "expected_net": r[6],
                    "realized_net": r[7],
                    "trade_statuses": json.loads(r[8]),
                    "notes": json.loads(r[9]),
                    "net_edge_per_contract": r[3],
                }
            )
    executed = [r for r in rows if r["decision"] == "PAPER_EXECUTE"]
    rejected = [r for r in rows if r["decision"] == "REJECTED"]
    wins = sum(1 for r in executed if (r["realized_net"] or 0) > 0)
    total_expected = sum(r["expected_net"] or 0.0 for r in executed)
    total_realized = sum(r["realized_net"] or 0.0 for r in executed)
    flags = [
        f"LIVE_PAPER_DATA: {len(scans)} live scans, "
        f"{scans[0][1][:16]} to {scans[-1][2][:16]}; paper trading only, "
        "no real orders"
    ]
    if len(executed) < MIN_SAMPLE_FOR_STATS:
        flags.append(
            f"SAMPLE_TOO_SMALL: n_executed={len(executed)} < {MIN_SAMPLE_FOR_STATS}; "
            "win rate and significance claims are not meaningful"
        )
    config = StrategyConfig.load()
    return {
        "mode": "live",
        "n_scans": len(scans),
        "scan_range": [scans[0][1], scans[-1][2]],
        "n_snapshots": len(scans),
        "labels": ["live"],
        "n_opportunities": len(rows),
        "n_executed": len(executed),
        "n_rejected": len(rejected),
        "trades_by_status": {},
        "total_expected_net": round(total_expected, 6),
        "total_realized_net": round(total_realized, 6),
        "hit_rate": round(wins / len(executed), 4) if executed else None,
        "flags": flags,
        "rows": rows,
        "config_notes": {
            "kelly_fraction": config.kelly.fraction,
            "bankroll": 100.0,
            "p_win_status": config.kelly.p_win_status,
            "drift_status": config.latency.drift_status,
        },
        "attribution": {},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", default="data/live/ledger.db")
    parser.add_argument("--out", default="frontend/dashboard/public/live-report.json")
    args = parser.parse_args()

    if not Path(args.ledger).exists():
        print(f"no ledger at {args.ledger}; run scripts/scan_live.py first")
        raise SystemExit(1)
    report = build_report(args.ledger)
    if report is None:
        print("ledger has no scans yet")
        raise SystemExit(1)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(
        f"wrote {out}: {report['n_scans']} scans, "
        f"{report['n_opportunities']} opportunities, "
        f"{report['n_executed']} paper-executed"
    )


if __name__ == "__main__":
    main()
