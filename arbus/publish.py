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

from . import config

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


def _headers() -> dict[str, str]:
    """Auth headers for the app API.

    Supabase's PostgREST wants the key in BOTH `apikey` and `Authorization`;
    sending only the bearer token gets a 401 that looks like a bad key. Any
    other backend just gets the bearer token.
    """
    headers = {"Content-Type": "application/json"}
    if config.ARBUS_API_KEY:
        headers["Authorization"] = f"Bearer {config.ARBUS_API_KEY}"
        if "supabase.co" in config.ARBUS_API_URL:
            headers["apikey"] = config.ARBUS_API_KEY
            headers["Prefer"] = "return=representation"
    return headers


def _url_with(params: str) -> str:
    sep = "&" if "?" in config.ARBUS_API_URL else "?"
    return f"{config.ARBUS_API_URL}{sep}{params}"


QUESTION_FIELDS = ("question_lt", "question", "title", "name", "text")


def fetch_app_markets(limit: int = 50) -> tuple[list[dict], str]:
    """Read what the app already has. Returns (rows, error).

    Read-only, and used for two things: proving the connection works, and
    keeping the generator from proposing a market the app already lists.
    """
    if not config.ARBUS_API_URL:
        return [], "ARBUS_API_URL is not set"
    try:
        resp = requests.get(_url_with(f"limit={limit}"), headers=_headers(),
                            timeout=config.ARBUS_API_TIMEOUT)
    except requests.RequestException as exc:
        return [], f"network error: {exc}"
    if resp.status_code >= 400:
        return [], f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        data = resp.json()
    except ValueError:
        return [], f"response is not JSON: {resp.text[:120]}"
    if isinstance(data, dict):            # some APIs wrap the list
        data = data.get("markets") or data.get("data") or []
    return (data if isinstance(data, list) else []), ""


def question_of(row: dict) -> str:
    """The question text, whatever the app's column happens to be called."""
    for field in QUESTION_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def app_questions(limit: int = 200) -> list[str]:
    """Questions already live in the app, for duplicate checking. Never raises:
    a batch must not fail because the app API is down."""
    try:
        rows, error = fetch_app_markets(limit)
    except Exception as exc:               # defensive: this runs inside a batch
        log.warning("app dedupe skipped: %s", exc)
        return []
    if error:
        log.warning("app dedupe skipped: %s", error)
        return []
    return [q for q in (question_of(r) for r in rows) if q]


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
