"""Tests for empirical calibration loading and provenance.

ALL INPUTS SYNTHETIC: every YAML document below is hand-written for
testing. Nothing here is live market data, and no result in this file may
be presented as live trading performance.

Covers: parsing of the latency-drift and Kelly sections, the preliminary
label, missing-file and malformed-file behavior, the ODDS_NO_CALIBRATION
kill-switch (which the rest of the suite relies on), and the
StrategyConfig wiring that turns a parsed calibration into engine inputs.
"""

import pytest
import yaml

from backend.arbitrage import settings
from backend.arbitrage.calibration import (
    NO_CALIBRATION_ENV,
    Calibration,
    load_calibration,
)
from backend.arbitrage.settings import StrategyConfig
from backend.errors import ConfigurationError

DRIFT_DOC = {
    "latency_drift": {
        "drift_per_second": 2.88e-07,
        "median_per_second": 0.0,
        "n_observations": 29,
        "n_markets": 15,
        "venues": ["polymarket"],
        "start": "2026-09-25",
        "end": "2026-09-25",
        "preliminary": True,
        "method": "test method",
    }
}

KELLY_DOC = {
    "kelly": {
        "arbitrage_p_win": 0.97,
        "n_executions": 40,
        "n_wins": 39,
        "start": "2026-09-25",
        "end": "2026-09-25",
        "preliminary": True,
        "method": "test method",
    }
}


def _write(tmp_path, doc):
    p = tmp_path / "calibration.yaml"
    p.write_text(yaml.safe_dump(doc))
    return p


@pytest.fixture()
def cal_env_off(monkeypatch):
    """Undo the conftest kill-switch so load_calibration reads files."""
    monkeypatch.delenv(NO_CALIBRATION_ENV, raising=False)


def test_load_drift_preliminary(tmp_path, cal_env_off):
    cal = load_calibration(_write(tmp_path, DRIFT_DOC))
    assert isinstance(cal, Calibration)
    assert cal.latency_drift is not None
    assert cal.latency_drift.drift_per_second == pytest.approx(2.88e-07)
    assert cal.latency_drift.n_observations == 29
    assert cal.latency_drift.n_markets == 15
    assert cal.latency_drift.preliminary is True
    assert cal.kelly is None


def test_load_kelly(tmp_path, cal_env_off):
    cal = load_calibration(_write(tmp_path, KELLY_DOC))
    assert cal.kelly is not None
    assert cal.kelly.arbitrage_p_win == pytest.approx(0.97)
    assert cal.kelly.n_executions == 40
    assert cal.kelly.preliminary is True


def test_load_both_sections(tmp_path, cal_env_off):
    doc = {**DRIFT_DOC, **KELLY_DOC}
    cal = load_calibration(_write(tmp_path, doc))
    assert cal.latency_drift is not None
    assert cal.kelly is not None


def test_missing_file_returns_none(tmp_path, cal_env_off):
    assert load_calibration(tmp_path / "nope.yaml") is None


def test_malformed_raises(tmp_path, cal_env_off):
    p = _write(tmp_path, {"latency_drift": {"n_observations": 5}})
    with pytest.raises(ConfigurationError):
        load_calibration(p)


def test_env_killswitch(tmp_path, cal_env_off, monkeypatch):
    p = _write(tmp_path, DRIFT_DOC)
    assert load_calibration(p) is not None
    monkeypatch.setenv(NO_CALIBRATION_ENV, "1")
    assert load_calibration(p) is None


def test_strategy_config_uses_calibrated_drift(tmp_path, cal_env_off, monkeypatch):
    cal = load_calibration(_write(tmp_path, DRIFT_DOC))
    monkeypatch.setattr(settings, "load_calibration", lambda: cal)
    cfg = StrategyConfig.load()
    assert cfg.latency.adverse_drift_per_second == pytest.approx(2.88e-07)
    assert cfg.latency.drift_status == "preliminary"
    assert cfg.latency.drift_is_placeholder is False


def test_strategy_config_kelly_preliminary(tmp_path, cal_env_off, monkeypatch):
    cal = load_calibration(_write(tmp_path, KELLY_DOC))
    monkeypatch.setattr(settings, "load_calibration", lambda: cal)
    cfg = StrategyConfig.load()
    assert cfg.kelly.arbitrage_p_win == pytest.approx(0.97)
    assert cfg.kelly.p_win_status == "preliminary"
    assert cfg.kelly.p_win_n == 40
    assert cfg.kelly.p_win_period == "2026-09-25 to 2026-09-25"


def test_strategy_config_defaults_when_uncalibrated(cal_env_off, monkeypatch):
    monkeypatch.setattr(settings, "load_calibration", lambda: None)
    cfg = StrategyConfig.load()
    assert cfg.latency.drift_status == "placeholder"
    assert cfg.latency.drift_is_placeholder is True
    assert cfg.kelly.p_win_status == "assumption"
    assert cfg.kelly.p_win_n == 0
