"""Arbucks bookkeeping and reputation — the swappable persistence layer.

Everything the resolution engine knows about balances, bonds and reputation
goes through this module. Balances live in this repo's SQLite for v1 so the
numbers can be tuned without touching the app backend; when they move to the
app's database, only this file is rewritten and the engine above it is
untouched.

The ledger is append-only. A balance is the sum of its entries, never a mutated
number, so every Arbuck a user gains or loses has a row explaining why — which
is what makes a disputed resolution auditable after the fact.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    market_id INTEGER,
    delta INTEGER NOT NULL,           -- Arbucks, negative = taken from the user
    kind TEXT NOT NULL,               -- bond_escrow | bond_return | bond_forfeit
                                      -- | reward | settlement
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id);

CREATE TABLE IF NOT EXISTS reputation (
    user_id TEXT PRIMARY KEY,
    correct_proposals INTEGER NOT NULL DEFAULT 0,
    false_proposals INTEGER NOT NULL DEFAULT 0,
    correct_challenges INTEGER NOT NULL DEFAULT 0,
    false_challenges INTEGER NOT NULL DEFAULT 0,
    predictions INTEGER NOT NULL DEFAULT 0
);
"""


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def post(conn: sqlite3.Connection, user_id: str, delta: int, kind: str,
         market_id: int | None = None, note: str = "") -> None:
    """Append one ledger entry. Never updates an existing row."""
    conn.execute(
        "INSERT INTO ledger (user_id, market_id, delta, kind, note, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (user_id, market_id, int(delta), kind, note[:200], _now()),
    )


def balance(conn: sqlite3.Connection, user_id: str) -> int:
    row = conn.execute("SELECT COALESCE(SUM(delta), 0) FROM ledger WHERE user_id = ?",
                       (user_id,)).fetchone()
    return int(row[0])


def history(conn: sqlite3.Connection, user_id: str, limit: int = 50) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM ledger WHERE user_id = ? ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()


# ── Reputation ──────────────────────────────────────────────────────────────
# v1: accumulated and displayable only. It does NOT change bond sizes or reward
# amounts — that is explicitly deferred to v2, so nothing here reads it back
# into the economics.

_REPUTATION_FIELDS = ("correct_proposals", "false_proposals",
                      "correct_challenges", "false_challenges", "predictions")


def _ensure_user(conn: sqlite3.Connection, user_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO reputation (user_id) VALUES (?)", (user_id,))


def bump(conn: sqlite3.Connection, user_id: str, field: str, by: int = 1) -> None:
    if field not in _REPUTATION_FIELDS:
        raise ValueError(f"unknown reputation field: {field}")
    _ensure_user(conn, user_id)
    conn.execute(f"UPDATE reputation SET {field} = {field} + ? WHERE user_id = ?",
                 (by, user_id))


def reputation(conn: sqlite3.Connection, user_id: str) -> dict:
    _ensure_user(conn, user_id)
    row = conn.execute("SELECT * FROM reputation WHERE user_id = ?", (user_id,)).fetchone()
    data = {f: row[f] for f in _REPUTATION_FIELDS}
    # A single readable number for the profile: right calls minus wrong ones.
    data["score"] = (data["correct_proposals"] + data["correct_challenges"]
                     - data["false_proposals"] - data["false_challenges"])
    return data
