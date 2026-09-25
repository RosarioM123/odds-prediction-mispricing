"""Pytest session setup.

Unit tests must be deterministic regardless of ambient files: they always
exercise the documented placeholder calibration path, never whatever
configs/calibration.yaml happens to exist on the machine running them.
Calibration parsing and provenance are covered separately in
tests/test_calibration.py with explicit tmp_path fixtures.
"""

import os

os.environ.setdefault("ODDS_NO_CALIBRATION", "1")
