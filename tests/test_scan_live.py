"""Tests for the live scan loop's ledger persistence.

ALL INPUTS SYNTHETIC: every market, book, and scan summary below is
hand-built. No test here touches the network, and no result may be
presented as live trading performance.

Covers: open_ledger creates the schema, record_scan persists a scan and
its opportunities, export_live_report.build_report renders a ledger with
the LIVE_PAPER_DATA flag, and run_scan end-to-end with stubbed venue
adapters (detection -> paper execution -> summary rows).
"""

import importlib.util
from pathlib import Path

from backend.arbitrage.costs import FeeModel
from backend.arbitrage.settings import StrategyConfig
from backend.schemas import Outcome, Venue, utcnow
from tests.fixtures import make_book, make_market

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scan_live = _load("scan_live")
export_live_report = _load("export_live_report")


def _summary(rows=()):
    return {
        "started_at": "2026-09-25T12:00:00+00:00",
        "finished_at": "2026-09-25T12:00:08+00:00",
        "n_markets": 4,
        "n_books": 4,
        "n_opportunities": len(rows),
        "n_executed": sum(1 for r in rows if r["decision"] == "PAPER_EXECUTE"),
        "n_rejected": sum(1 for r in rows if r["decision"] == "REJECTED"),
        "fetch_s": 7.5,
        "detect_s": 0.01,
        "total_s": 8.1,
        "kalshi_env": "prod",
        "detector_flags": [],
        "rows": list(rows),
    }


def _row(decision="PAPER_EXECUTE", opp_id="opp-1"):
    return {
        "opportunity_id": opp_id,
        "detected_at": "2026-09-25T12:00:05+00:00",
        "strategy": "bundle_arbitrage",
        "net_edge_per_contract": 0.05,
        "quantity": 10.0,
        "decision": decision,
        "expected_net": 0.5,
        "realized_net": 0.5 if decision == "PAPER_EXECUTE" else None,
        "trade_statuses": ["FILLED", "FILLED"],
        "notes": ["note-a"],
    }


def test_open_ledger_creates_schema(tmp_path):
    conn = scan_live.open_ledger(tmp_path / "ledger.db")
    tables = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    assert {"scans", "scan_opportunities"} <= tables
    conn.close()


def test_record_scan_round_trip(tmp_path):
    conn = scan_live.open_ledger(tmp_path / "ledger.db")
    scan_id = scan_live.record_scan(
        conn, _summary([_row("PAPER_EXECUTE", "opp-1"), _row("REJECTED", "opp-2")])
    )
    assert scan_id == 1
    scan = conn.execute("SELECT * FROM scans WHERE id = 1").fetchone()
    assert scan[3] == 4  # n_markets
    assert scan[5] == 2  # n_opportunities
    assert scan[6] == 1  # n_executed
    opps = conn.execute(
        "SELECT opportunity_id, decision, quantity FROM scan_opportunities"
        " WHERE scan_id = 1 ORDER BY opportunity_id"
    ).fetchall()
    assert opps == [("opp-1", "PAPER_EXECUTE", 10.0), ("opp-2", "REJECTED", 10.0)]
    conn.close()


def test_export_live_report_flags(tmp_path):
    ledger = tmp_path / "ledger.db"
    conn = scan_live.open_ledger(ledger)
    scan_live.record_scan(conn, _summary([_row("PAPER_EXECUTE", "opp-1")]))
    conn.close()

    report = export_live_report.build_report(ledger)
    assert report["mode"] == "live"
    assert report["n_scans"] == 1
    assert report["n_opportunities"] == 1
    assert report["n_executed"] == 1
    assert any("LIVE_PAPER_DATA" in f for f in report["flags"])
    assert any("SAMPLE_TOO_SMALL" in f for f in report["flags"])
    assert len(report["rows"]) == 1
    assert report["rows"][0]["opportunity_id"] == "opp-1"


def test_export_empty_ledger_returns_none(tmp_path):
    ledger = tmp_path / "ledger.db"
    conn = scan_live.open_ledger(ledger)
    conn.close()
    assert export_live_report.build_report(ledger) is None


class StubAdapter:
    """Minimal MarketDataAdapter double: canned markets and books."""

    def __init__(self, markets, books):
        self._markets = markets
        self._books = {b.market_id: b for b in books}

    def fetch_markets(self, limit=20):
        return self._markets[:limit]

    def fetch_order_book(self, market):
        return self._books[market.market_id]


def _arb_pair():
    ym = make_market(
        venue=Venue.POLYMARKET,
        outcome=Outcome.YES,
        market_id="pm-yes-1",
        event_id="evt-live",
        taker_fee_rate=0.01,
    )
    nm = make_market(
        venue=Venue.POLYMARKET,
        outcome=Outcome.NO,
        market_id="pm-no-1",
        event_id="evt-live",
        taker_fee_rate=0.01,
    )
    # yes_ask + no_ask = 0.80: a wide synthetic bundle edge.
    # Books are timestamped now: quotes older than max_book_age_seconds
    # are STALE and never traded.
    now = utcnow()
    yb = make_book(ym, bids=[(0.38, 500.0)], asks=[(0.40, 500.0)], at=now)
    nb = make_book(nm, bids=[(0.38, 500.0)], asks=[(0.40, 500.0)], at=now)
    return [ym, nm], [yb, nb]


def test_run_scan_detects_and_records(monkeypatch, tmp_path):
    markets, books = _arb_pair()
    stub = StubAdapter(markets, books)
    monkeypatch.setattr(
        scan_live, "venue_adapters", lambda: {"polymarket": stub, "kalshi": StubAdapter([], [])}
    )
    summary = scan_live.run_scan(StrategyConfig.load(), FeeModel(), 10, 10, 2, min_volume_24h=0.0)
    assert summary["n_opportunities"] == 1
    assert summary["n_executed"] == 1
    assert summary["rows"][0]["strategy"] == "bundle_arbitrage"
    assert summary["rows"][0]["decision"] == "PAPER_EXECUTE"
    assert summary["rows"][0]["quantity"] > 0

    ledger = tmp_path / "ledger.db"
    conn = scan_live.open_ledger(ledger)
    scan_id = scan_live.record_scan(conn, summary)
    n_opps = conn.execute(
        "SELECT COUNT(*) FROM scan_opportunities WHERE scan_id = ?", (scan_id,)
    ).fetchone()[0]
    conn.close()
    assert n_opps == 1


def test_run_scan_no_opportunities_still_writes_scan(monkeypatch):
    monkeypatch.setattr(
        scan_live,
        "venue_adapters",
        lambda: {"polymarket": StubAdapter([], []), "kalshi": StubAdapter([], [])},
    )
    summary = scan_live.run_scan(StrategyConfig.load(), FeeModel(), 10, 10, 2)
    assert summary["n_opportunities"] == 0
    assert summary["rows"] == []
    assert summary["total_s"] >= 0
    # started_at is a real wall-clock timestamp, not perf_counter garbage.
    assert summary["started_at"].startswith("202")
