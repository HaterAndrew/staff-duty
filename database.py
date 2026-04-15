"""SQLite persistence layer for the staff duty roster app."""

import json
import os
import sqlite3
import uuid
from datetime import UTC, datetime

# Fly.io persistent volume at /data/; fall back to local file for dev
if os.path.isdir('/data/'):
    _default_db = '/data/staff_duty.db'
else:
    _default_db = os.path.join(os.path.dirname(__file__) or '.', 'staff_duty.db')

DB_PATH = os.environ.get('STAFF_DUTY_DB', _default_db)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS configs (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    config_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rosters (
    id                       TEXT PRIMARY KEY,
    config_id                TEXT REFERENCES configs(id) ON DELETE SET NULL,
    quarter                  TEXT NOT NULL,
    generated_at             TEXT NOT NULL,
    solver_status            TEXT NOT NULL,
    gini_sdnco               REAL,
    gini_runner              REAL,
    roster_json              TEXT NOT NULL,
    config_json              TEXT NOT NULL,
    swaps_json               TEXT DEFAULT '[]',
    locked_json              TEXT DEFAULT '{}',
    soldier_assignments_json TEXT DEFAULT '[]'
);
"""


def get_db() -> sqlite3.Connection:
    """Get a database connection, creating tables if needed."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Ensure tables exist on every connection (cheap no-op if already there)
    conn.executescript(_SCHEMA)
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_db()
    conn.close()


# ---------------------------------------------------------------------------
# Config CRUD
# ---------------------------------------------------------------------------

def save_config(name: str, config: dict) -> str:
    """Save a roster configuration. Returns the new config id."""
    config_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO configs (id, name, created_at, config_json) VALUES (?, ?, ?, ?)",
            (config_id, name, now, json.dumps(config)),
        )
        conn.commit()
    finally:
        conn.close()
    return config_id


def list_configs() -> list[dict]:
    """Return all saved configs, newest first."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, created_at, config_json FROM configs ORDER BY created_at DESC"
        ).fetchall()
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "created_at": r["created_at"],
                "config": json.loads(r["config_json"]),
            }
            for r in rows
        ]
    finally:
        conn.close()


def get_config(config_id: str) -> dict | None:
    """Fetch a single config by id, or None if not found."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id, name, created_at, config_json FROM configs WHERE id = ?",
            (config_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "created_at": row["created_at"],
            "config": json.loads(row["config_json"]),
        }
    finally:
        conn.close()


def delete_config(config_id: str) -> bool:
    """Delete a config. Returns True if a row was actually removed."""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM configs WHERE id = ?", (config_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Roster CRUD
# ---------------------------------------------------------------------------

def save_roster(
    config_id: str | None,
    quarter: str,
    solver_status: str,
    gini_sdnco: float,
    gini_runner: float,
    roster_json: dict,
    config_json: dict,
) -> str:
    """Persist a generated roster. Returns the new roster id."""
    roster_id = str(uuid.uuid4())
    now = datetime.now(UTC).isoformat()
    conn = get_db()
    try:
        conn.execute(
            """INSERT INTO rosters
               (id, config_id, quarter, generated_at, solver_status,
                gini_sdnco, gini_runner, roster_json, config_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                roster_id,
                config_id,
                quarter,
                now,
                solver_status,
                gini_sdnco,
                gini_runner,
                json.dumps(roster_json),
                json.dumps(config_json),
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return roster_id


def _roster_row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a roster row to a plain dict with parsed JSON fields."""
    return {
        "id": row["id"],
        "config_id": row["config_id"],
        "quarter": row["quarter"],
        "generated_at": row["generated_at"],
        "solver_status": row["solver_status"],
        "gini_sdnco": row["gini_sdnco"],
        "gini_runner": row["gini_runner"],
        "roster": json.loads(row["roster_json"]),
        "config": json.loads(row["config_json"]),
        "swaps": json.loads(row["swaps_json"]),
        "locks": json.loads(row["locked_json"]),
        "soldier_assignments": json.loads(row["soldier_assignments_json"]),
    }


def list_rosters(page: int = 1, per_page: int = 20) -> tuple[list[dict], int]:
    """Return a page of rosters (newest first) and total count."""
    conn = get_db()
    try:
        total = conn.execute("SELECT COUNT(*) FROM rosters").fetchone()[0]
        offset = (page - 1) * per_page
        rows = conn.execute(
            "SELECT * FROM rosters ORDER BY generated_at DESC LIMIT ? OFFSET ?",
            (per_page, offset),
        ).fetchall()
        return [_roster_row_to_dict(r) for r in rows], total
    finally:
        conn.close()


def get_roster(roster_id: str) -> dict | None:
    """Fetch a single roster by id, or None if not found."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT * FROM rosters WHERE id = ?", (roster_id,)
        ).fetchone()
        if row is None:
            return None
        return _roster_row_to_dict(row)
    finally:
        conn.close()


def delete_roster(roster_id: str) -> bool:
    """Delete a roster. Returns True if a row was actually removed."""
    conn = get_db()
    try:
        cur = conn.execute("DELETE FROM rosters WHERE id = ?", (roster_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_roster_swaps(roster_id: str, swaps: list) -> bool:
    """Replace the swaps list on a roster."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE rosters SET swaps_json = ? WHERE id = ?",
            (json.dumps(swaps), roster_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_roster_locks(roster_id: str, locks: dict) -> bool:
    """Replace the locks dict on a roster."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE rosters SET locked_json = ? WHERE id = ?",
            (json.dumps(locks), roster_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def update_roster_soldiers(roster_id: str, assignments: list) -> bool:
    """Replace the soldier assignments list on a roster."""
    conn = get_db()
    try:
        cur = conn.execute(
            "UPDATE rosters SET soldier_assignments_json = ? WHERE id = ?",
            (json.dumps(assignments), roster_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()
