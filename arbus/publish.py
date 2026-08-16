"""Stage 7 — publish approved markets into the Arbus app over HTTP.

The app's endpoint does not exist yet, so this module is built to be pointed at
it later without touching the pipeline: set ARBUS_API_URL and ARBUS_API_KEY,
and `python -m arbus publish <id>...` POSTs the payload below. Until then
`--dry-run` prints exactly what would be sent, which is also the document to
hand whoever builds the endpoint.

Publishing is deliberately explicit and per-market: markets go live only for
ids a human passed on the command line, never automatically at the end of a
batch. Already-published markets are skipped unless --force is given, so a
repeated command cannot double-post.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone

import requests

from . import app, config

log = logging.getLogger(__name__)


def market_payload(row: sqlite3.Row) -> dict:
    """The wire format sent to the app. Keep this stable — it is the contract."""
    return {
        "external_id": f"arbus-{row['id']}",
        "question": row["question_lt"],
        "type": row["market_type"],
        "options": json.loads(row["options_json"]),
        "probabilities": json.loads(row["probabilities_json"]),
        "category": row["category"],
        "resolve_by": row["resolve_by"],
        "duration_class": row["duration_class"],
        "resolution_criteria": row["resolution_hint_lt"],
        "sources": json.loads(row["sources_json"]),
        "image_url": row["image_url"] if "image_url" in row.keys() else "",
        "language": "lt",
        "generated_at": row["created_at"],
    }


# The HTTP client lives in app.py (read side); these thin aliases keep the
# publish path and its callers on exactly the same auth and URL handling.
_headers = app.headers
_url_with = app.url_with
question_of = app.question_of


def fetch_app_markets(limit: int = 50) -> tuple[list[dict], str]:
    """Read what the app already has. Returns (rows, error)."""
    return app.markets(limit)


def app_questions(limit: int = 200) -> list[str]:
    """Questions already live in the app, for duplicate checking. Never raises:
    a batch must not fail because the app API is down."""
    rows, error = app.markets(limit)
    if error:
        log.warning("app dedupe skipped: %s", error)
        return []
    return [q for q in (app.question_of(r) for r in rows) if q]


def publish_market(row: sqlite3.Row) -> tuple[bool, str]:
    """POST one market. Returns (ok, detail)."""
    if not config.ARBUS_API_URL:
        return False, "ARBUS_API_URL is not set"
    headers = _headers()
    try:
        resp = requests.post(config.ARBUS_API_URL, headers=headers,
                             json=market_payload(row), timeout=config.ARBUS_API_TIMEOUT)
    except requests.RequestException as exc:
        return False, f"network error: {exc}"
    if resp.status_code >= 400:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    return True, f"HTTP {resp.status_code}"


def mark_published(conn: sqlite3.Connection, market_id: int, response: str) -> None:
    conn.execute(
        "UPDATE markets SET published_at = ?, publish_note = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), response[:300], market_id),
    )
