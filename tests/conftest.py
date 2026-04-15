"""Shared fixtures for staff duty roster tests."""

import os
from datetime import date

import pytest

from staff_duty.calendar_utils import build_holiday_set, get_quarter_days
from staff_duty.config import Directorate, RosterConfig
from staff_duty.solver import solve_joint


@pytest.fixture
def sample_config():
    """Return (sdnco_cfg, runner_cfg) for a 14-day range with 3 directorates."""
    start = date(2026, 7, 1)
    end = date(2026, 7, 14)

    sdnco_dirs = [
        Directorate("G1", 6),
        Directorate("G2", 4),
        Directorate("G3", 3),
    ]
    runner_dirs = [
        Directorate("G1", 6),
        Directorate("G2", 4),
        Directorate("G3", 3),
    ]

    sdnco_cfg = RosterConfig("SDNCO", start, end, sdnco_dirs)
    runner_cfg = RosterConfig("SD_Runner", start, end, runner_dirs)
    return sdnco_cfg, runner_cfg


@pytest.fixture
def solved_roster(sample_config):
    """Solve the sample config and return (sdnco_sol, runner_sol, all_days, holiday_dates)."""
    sdnco_cfg, runner_cfg = sample_config
    holiday_dates = build_holiday_set(sdnco_cfg.start_date, sdnco_cfg.end_date)
    all_days = get_quarter_days(sdnco_cfg.start_date, sdnco_cfg.end_date)
    sdnco_sol, runner_sol = solve_joint(sdnco_cfg, runner_cfg, all_days, holiday_dates)
    return sdnco_sol, runner_sol, all_days, holiday_dates


@pytest.fixture
def flask_test_client():
    """Create a Flask test client."""
    from staff_duty.app import app

    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


@pytest.fixture
def sample_config_json():
    """Return sample config as a JSON-compatible dict matching the web form format."""
    return {
        "start": "2026-07-01",
        "end": "2026-07-14",
        "dir_name": ["G1", "G2", "G3"],
        "sdnco_count": ["6", "4", "3"],
        "runner_count": ["6", "4", "3"],
    }


@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite database, set STAFF_DUTY_DB env var, yield path, clean up."""
    db_path = str(tmp_path / "test_staff_duty.db")
    old_val = os.environ.get("STAFF_DUTY_DB")
    os.environ["STAFF_DUTY_DB"] = db_path

    # Force database module to pick up the new path
    import staff_duty.database as db_mod
    original_db_path = db_mod.DB_PATH
    db_mod.DB_PATH = db_path

    yield db_path

    # Restore
    db_mod.DB_PATH = original_db_path
    if old_val is None:
        os.environ.pop("STAFF_DUTY_DB", None)
    else:
        os.environ["STAFF_DUTY_DB"] = old_val
