"""Fit empirical calibrations from live observations.

Reads ``data/live/latency_observations.jsonl`` (written by
``scripts/collect_latency_snapshots.py``) and writes
``configs/calibration.yaml``:

* latency drift: mean |mid move| per second between consecutive
  collection rounds, per market. This is a proxy for the expected
  adverse price move over the latency budget.
* Kelly p_win: fraction of paper-executed locked-arbitrage opportunities
  (replayed over the collected live snapshots) whose legs all filled
  with no unpaired residual. Execution risk is the only thing standing
  between a detected locked arb and its payout.

Both fits are labeled preliminary: the in-session sample is small and
covers minutes, not market regimes. The YAML records n, date range, and
method so no reader mistakes this for a production calibration.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from datetime import datetime
from pathlib import Path

import yaml

sys.path.insert(0, ".")

from backend.backtesting.replay import (  # noqa: E402
    ReplayEngine,
    load_labeled_snapshot,
)
from backend.errors import DataValidationError  # noqa: E402


def fit_drift(rows: list[dict]) -> dict | None:
    """Mean |mid move| per second between consecutive rounds."""
    by_market: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        if r.get("best_bid") is None or r.get("best_ask") is None:
            continue
        by_market.setdefault((r["venue"], r["market_id"]), []).append(r)
    moves: list[float] = []
    move_venues: set[str] = set()
    markets = 0
    first: datetime | None = None
    last: datetime | None = None
    for key, obs in by_market.items():
        obs.sort(key=lambda o: o["received_at"])
        if len(obs) < 2:
            continue
        markets += 1
        for prev, cur in zip(obs, obs[1:], strict=False):
            t0 = datetime.fromisoformat(prev["received_at"])
            t1 = datetime.fromisoformat(cur["received_at"])
            dt = (t1 - t0).total_seconds()
            if dt <= 0:
                continue
            mid0 = (prev["best_bid"] + prev["best_ask"]) / 2.0
            mid1 = (cur["best_bid"] + cur["best_ask"]) / 2.0
            moves.append(abs(mid1 - mid0) / dt)
            move_venues.add(key[0])
            first = t0 if first is None or t0 < first else first
            last = t1 if last is None or t1 > last else last
    if not moves or first is None or last is None:
        return None
    return {
        "drift_per_second": statistics.mean(moves),
        "median_per_second": statistics.median(moves),
        "n_observations": len(moves),
        "n_markets": markets,
        "venues": sorted(move_venues),
        "start": first.date().isoformat(),
        "end": last.date().isoformat(),
        "preliminary": True,
        "method": (
            "mean |mid move| per second between consecutive collection "
            "rounds over live order books; proxy for expected adverse move "
            "over the latency budget. Zero moves dominate short-horizon "
            "samples; the fit is preliminary until a longer window exists."
        ),
    }


def fit_kelly_pwin(snapshot_dir: Path) -> dict | None:
    """p_win for locked arb from paper-execution outcomes on live snaps."""
    paths = sorted(snapshot_dir.glob("round*-*.json"))
    if not paths:
        return None
    snapshots = []
    for p in paths:
        try:
            snapshots.append(load_labeled_snapshot(p))
        except DataValidationError as exc:
            print(f"  skip {p.name}: {exc}")
    if not snapshots:
        return None
    engine = ReplayEngine()
    report = engine.run(snapshots)
    executed = [r for r in report.rows if r.decision == "PAPER_EXECUTE"]
    if not executed:
        print("  no executed opportunities; cannot fit p_win")
        return None
    wins = 0
    for row in executed:
        filled_ok = row.trade_statuses and all(s == "FILLED" for s in row.trade_statuses)
        no_residual = not any("unpaired residual" in n for n in row.notes)
        if filled_ok and no_residual:
            wins += 1
    p_win = wins / len(executed)
    starts = min(s.captured_at for s in snapshots)
    ends = max(s.captured_at for s in snapshots)
    # Floor/ceiling: a tiny sample must not claim certainty or impossibility.
    p_win = min(0.999, max(0.5, round(p_win, 4)))
    return {
        "arbitrage_p_win": p_win,
        "n_executions": len(executed),
        "n_wins": wins,
        "start": starts.date().isoformat(),
        "end": ends.date().isoformat(),
        "preliminary": True,
        "method": (
            "fraction of paper-executed locked-arbitrage opportunities on "
            "live snapshots with all legs FILLED and no unpaired residual; "
            "floored at 0.5 and capped at 0.999 for small samples"
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-dir", default="data/live")
    parser.add_argument("--out", default="configs/calibration.yaml")
    args = parser.parse_args()

    live = Path(args.live_dir)
    obs_path = live / "latency_observations.jsonl"
    rows = []
    if obs_path.exists():
        rows = [json.loads(line) for line in obs_path.read_text().splitlines() if line.strip()]
    print(f"observations: {len(rows)}")

    calibration: dict = {}
    drift = fit_drift(rows)
    if drift:
        calibration["latency_drift"] = drift
        print(
            f"drift: {drift['drift_per_second']}/s "
            f"(median {drift['median_per_second']}), "
            f"n={drift['n_observations']} moves over {drift['n_markets']} markets, "
            f"{drift['start']} to {drift['end']}"
        )
    else:
        print("drift: insufficient data, left uncalibrated")

    kelly = fit_kelly_pwin(live / "snapshots")
    if kelly:
        calibration["kelly"] = kelly
        print(
            f"p_win: {kelly['arbitrage_p_win']} "
            f"({kelly['n_wins']}/{kelly['n_executions']} clean executions), "
            f"{kelly['start']} to {kelly['end']}"
        )
    else:
        print("kelly: insufficient data, left uncalibrated")

    if not calibration:
        print("nothing to write; configs/calibration.yaml unchanged")
        return
    header = (
        "# Empirical calibrations fitted from live venue observations.\n"
        "# Written by scripts/fit_calibration.py. All fits here are labeled\n"
        "# preliminary: small in-session samples, not production calibrations.\n"
    )
    Path(args.out).write_text(header + yaml.safe_dump(calibration, sort_keys=False))
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
