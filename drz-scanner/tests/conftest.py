from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def fixture():
    return load_fixture


@pytest.fixture
def settings(tmp_path, monkeypatch):
    """Settings pointed at a temp directory so tests never touch real data."""
    from app.config import Settings

    return Settings(
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        raw_archive_dir=tmp_path / "raw",
        calibration_path=tmp_path / "calibration.json",
        history_dir=tmp_path / "history",
        archive_raw_responses=False,
    )


@pytest.fixture
def calibration():
    """The shipped calibration, so tests exercise the real parameters."""
    from app.calibration import load_calibration

    return load_calibration(Path(__file__).parent.parent / "data" / "calibration.json")


@pytest.fixture(autouse=True)
def _reset_excluded_event_ids():
    """The scanner remembers non-AU event ids for the day; tests must not."""
    from app import drz

    drz.EXCLUDED_EVENT_IDS.clear()
    yield
    drz.EXCLUDED_EVENT_IDS.clear()
