"""Tests for the SQLite snapshot store (backend/markets/snapshot_store.py)."""

from datetime import timedelta

import pytest

from backend.errors import DataValidationError
from backend.markets.snapshot_store import SnapshotStore
from backend.schemas import Venue
from tests.fixtures import T0, polymarket_pair


def _payload(at, label="live"):
    yv, nv = polymarket_pair(yes_ask=0.45, no_ask=0.45, depth=200.0, at=at)
    return {
        "odds_snapshot_version": 1,
        "label": label,
        "venue": Venue.POLYMARKET.value,
        "captured_at": at.isoformat(),
        "markets": [yv.market.model_dump(mode="json"), nv.market.model_dump(mode="json")],
        "order_books": [yv.book.model_dump(mode="json"), nv.book.model_dump(mode="json")],
    }


def test_append_and_iter_chronological(tmp_path):
    store = SnapshotStore(tmp_path / "snaps.db")
    t1 = T0 + timedelta(seconds=60)
    store.append(_payload(t1))
    store.append(_payload(T0))
    inputs = store.iter_inputs()
    assert [i.captured_at for i in inputs] == [T0, t1]
    assert all(i.label == "live" for i in inputs)
    assert all(i.venue == Venue.POLYMARKET for i in inputs)
    assert len(inputs[0].books) == 2
    stats = store.stats()
    assert stats["n_snapshots"] == 2
    assert stats["by_venue"] == {"polymarket": 2}
    store.close()


def test_append_rejects_bad_payload(tmp_path):
    store = SnapshotStore(tmp_path / "snaps.db")
    bad = _payload(T0)
    bad["odds_snapshot_version"] = 999
    with pytest.raises(DataValidationError):
        store.append(bad)
    bad2 = _payload(T0)
    del bad2["markets"]
    with pytest.raises(DataValidationError):
        store.append(bad2)
    assert store.stats()["n_snapshots"] == 0
    store.close()


def test_iter_inputs_filters(tmp_path):
    store = SnapshotStore(tmp_path / "snaps.db")
    store.append(_payload(T0, label="live"))
    store.append(_payload(T0 + timedelta(seconds=60), label="simulated"))
    assert len(store.iter_inputs(label="live")) == 1
    assert len(store.iter_inputs(since=(T0 + timedelta(seconds=30)).isoformat())) == 1
    assert len(store.iter_inputs(venue="kalshi")) == 0
    store.close()


def test_import_directory(tmp_path):
    src = tmp_path / "jsons"
    src.mkdir()
    import json

    (src / "a.json").write_text(json.dumps(_payload(T0)))
    (src / "b.json").write_text(json.dumps(_payload(T0 + timedelta(seconds=60))))
    (src / "broken.json").write_text("{not json")
    store = SnapshotStore(tmp_path / "snaps.db")
    n = store.import_directory(src)
    assert n == 2
    assert store.stats()["n_snapshots"] == 2
    store.close()
