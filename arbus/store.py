"""SQLite persistence: candidate history, dedupe corpus, full specs."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import config
from .schemas import Candidate, FullSpec

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL,
    question_lt TEXT NOT NULL,
    market_type TEXT NOT NULL,
    options_json TEXT NOT NULL,
    probabilities_json TEXT NOT NULL,
    category TEXT NOT NULL,
    resolve_by TEXT NOT NULL,
    duration_class TEXT NOT NULL,
    resolution_hint_lt TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    rationale_en TEXT NOT NULL,
    verify_verdict TEXT NOT NULL DEFAULT '',
    verify_note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'candidate',  -- candidate | needs_review | rejected | promoted
    reject_reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS full_specs (
    market_id INTEGER PRIMARY KEY REFERENCES markets(id),
    spec_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


# Columns added after the first release. Existing databases are migrated in
# place on connect, so an old data/arbus.db keeps working without a manual step.
_ADDED_COLUMNS = {
    "image_url": "TEXT NOT NULL DEFAULT ''",
    "image_source": "TEXT NOT NULL DEFAULT ''",
    "published_at": "TEXT NOT NULL DEFAULT ''",
    "publish_note": "TEXT NOT NULL DEFAULT ''",
    # Job 2 — resolution monitoring.
    "resolution_verdict": "TEXT NOT NULL DEFAULT ''",
    "resolution_option": "TEXT NOT NULL DEFAULT ''",
    "resolution_confidence": "TEXT NOT NULL DEFAULT ''",
    "resolution_note": "TEXT NOT NULL DEFAULT ''",
    "resolution_source": "TEXT NOT NULL DEFAULT ''",
    "resolved_at": "TEXT NOT NULL DEFAULT ''",
}


def connect(db_path: str = config.DB_PATH) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(markets)")}
    for name, decl in _ADDED_COLUMNS.items():
        if name not in have:
            conn.execute(f"ALTER TABLE markets ADD COLUMN {name} {decl}")
    conn.commit()
    return conn


def recent_questions(conn: sqlite3.Connection, days: int = config.DEDUPE_LOOKBACK_DAYS) -> list[str]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        "SELECT question_lt FROM markets WHERE created_at >= ? AND status != 'rejected'",
        (cutoff,),
    ).fetchall()
    return [r["question_lt"] for r in rows]


def insert_candidate(
    conn: sqlite3.Connection,
    cand: Candidate,
    batch_id: str,
    status: str,
    verify_verdict: str = "",
    verify_note: str = "",
    reject_reason: str = "",
) -> int:
    cur = conn.execute(
        """INSERT INTO markets (batch_id, question_lt, market_type, options_json,
               probabilities_json, category, resolve_by, duration_class,
               resolution_hint_lt, sources_json, rationale_en,
               verify_verdict, verify_note, status, reject_reason, created_at,
               image_url, image_source)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            batch_id,
            cand.question_lt,
            cand.market_type,
            json.dumps(cand.options_lt, ensure_ascii=False),
            json.dumps(cand.probabilities),
            cand.category,
            cand.resolve_by,
            cand.duration_class,
            cand.resolution_hint_lt,
            json.dumps(cand.sources),
            cand.rationale_en,
            verify_verdict,
            verify_note,
            status,
            reject_reason,
            datetime.now(timezone.utc).isoformat(),
            getattr(cand, "image_url", ""),
            getattr(cand, "image_source", ""),
        ),
    )
    return cur.lastrowid


def rebuild_from_exports(conn: sqlite3.Connection, export_dir: str = config.EXPORT_DIR) -> tuple[int, int]:
    """Recreate the dedupe corpus from exports/batch_*.json.

    The database is local state and can be lost (a bad pull, a new machine).
    Every batch also writes a JSON export containing the same candidates, so the
    corpus is reconstructible: the exports are the backup. Questions already in
    the database are skipped, making this safe to re-run.

    Returns (imported, skipped).
    """
    known = {q for (q,) in conn.execute("SELECT question_lt FROM markets")}
    imported = skipped = 0
    for path in sorted(Path(export_dir).glob("batch_*.json")):
        batch_id = path.stem.replace("batch_", "")
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for row in rows:
            question = row.get("question_lt", "")
            if not question or question in known:
                skipped += 1
                continue
            try:
                cand = Candidate(**{k: v for k, v in row.items()
                                    if k in Candidate.model_fields})
            except Exception:
                skipped += 1
                continue
            insert_candidate(conn, cand, batch_id, "candidate",
                             verify_verdict=row.get("verify_verdict", ""),
                             verify_note=row.get("verify_note", ""))
            known.add(question)
            imported += 1
    conn.commit()
    return imported, skipped


def get_market(conn: sqlite3.Connection, market_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()


def save_full_spec(conn: sqlite3.Connection, market_id: int, spec: FullSpec) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO full_specs (market_id, spec_json, created_at) VALUES (?,?,?)",
        (market_id, spec.model_dump_json(), datetime.now(timezone.utc).isoformat()),
    )
    conn.execute("UPDATE markets SET status = 'promoted' WHERE id = ?", (market_id,))


def list_markets(conn: sqlite3.Connection, status: str | None = None, limit: int = 50) -> list[sqlite3.Row]:
    if status:
        return conn.execute(
            "SELECT * FROM markets WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)
        ).fetchall()
    return conn.execute("SELECT * FROM markets ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
