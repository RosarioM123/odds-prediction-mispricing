"""Tests for chronological replay."""

from datetime import timedelta

from backend.backtesting.replay import (
    ReplayEngine,
    SnapshotInput,
    load_labeled_snapshot,
)
from backend.markets.snapshots import save_snapshot
from backend.schemas import Venue
from tests.fixtures import T0, make_book, make_market, polymarket_pair


def make_snapshot(
    *,
    yes_ask=0.45,
    no_ask=0.45,
    depth=200.0,
    at=T0,
    book_at=None,
    label="simulated",
    event_id="evt-1",
):
    yv, nv = polymarket_pair(
        yes_ask=yes_ask, no_ask=no_ask, depth=depth, at=(book_at or at), event_id=event_id
    )
    return SnapshotInput(
        label=label,
        venue=Venue.POLYMARKET,
        markets=[yv.market, nv.market],
        books=[yv.book, nv.book],
        captured_at=at,
    )


def test_replay_executes_bundle_and_reports_honestly():
    engine = ReplayEngine(bankroll=100.0)
    snaps = [make_snapshot(at=T0), make_snapshot(at=T0 + timedelta(seconds=60))]
    report = engine.run(snaps)
    assert report.n_snapshots == 2
    assert report.n_executed == 2
    assert report.n_rejected == 0
    assert report.trades_by_status.get("FILLED") == 4
    assert report.total_realized_net > 0
    # Honesty: tiny sample must be flagged, hit rate not meaningful.
    assert any("SAMPLE_TOO_SMALL" in f for f in report.flags)
    assert any("ALL_INPUT_SIMULATED" in f for f in report.flags)
    assert report.labels == ["simulated"]


def test_replay_sorts_chronologically():
    engine = ReplayEngine(bankroll=100.0)
    snaps = [make_snapshot(at=T0 + timedelta(seconds=60)), make_snapshot(at=T0)]
    report = engine.run(snaps)
    assert report.n_executed == 2
    times = [r.detected_at for r in report.rows]
    assert times == sorted(times)


def test_replay_mixed_labels_flagged():
    engine = ReplayEngine(bankroll=100.0)
    snaps = [
        make_snapshot(at=T0, label="simulated"),
        make_snapshot(at=T0 + timedelta(seconds=60), label="live"),
    ]
    report = engine.run(snaps)
    assert any("MIXED_LABELS" in f for f in report.flags)


def test_replay_stale_snapshot_executes_nothing():
    engine = ReplayEngine(bankroll=100.0)
    snaps = [make_snapshot(at=T0, book_at=T0 - timedelta(seconds=30))]
    report = engine.run(snaps)
    assert report.n_executed == 0
    assert report.n_opportunities == 0


def test_replay_conflicting_metadata_flagged():
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45)
    bad = make_market(
        venue=Venue.POLYMARKET,
        outcome=nv.market.outcome,
        market_id="pm-no-2",
        event_id="evt-1",
        question="Something completely different",
    )
    bad_book = make_book(bad, asks=[(0.45, 200.0)], at=T0)
    snap = SnapshotInput(
        label="simulated",
        venue=Venue.POLYMARKET,
        markets=[yv.market, nv.market, bad],
        books=[yv.book, nv.book, bad_book],
        captured_at=T0,
    )
    report = ReplayEngine(bankroll=100.0).run([snap])
    assert report.n_opportunities == 0
    assert any("CONFLICTING_METADATA" in f for f in report.flags)


def test_load_labeled_snapshot_roundtrip(tmp_path):
    yv, nv = polymarket_pair()
    path = save_snapshot(
        tmp_path / "s.json",
        Venue.POLYMARKET,
        [yv.market, nv.market],
        [yv.book, nv.book],
        label="live",
    )
    loaded = load_labeled_snapshot(path)
    assert loaded.label == "live"
    assert loaded.captured_at.tzinfo is not None
    assert len(loaded.books) == 2
