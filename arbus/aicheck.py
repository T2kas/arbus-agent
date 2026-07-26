"""Advisory AI check for a frozen market — speeds the admin up, decides nothing.

Per the Notion spec: "AI netrenkia sprendimų — tik pagreitina admin darbą".
This module reads the source a user cited and reports whether it actually says
what they claim. It writes no state and moves no Arbucks; the output is text
for the admin to read before deciding in the dashboard.

Kept deliberately cheap (~€0.01 per check, one call, few searches) because it
runs on every freeze, and a freeze should never be something the team hesitates
to trigger on cost grounds.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timezone

from . import config, llm, notify

log = logging.getLogger(__name__)

SYSTEM = (
    "You verify sources for a Lithuanian prediction-market admin. You never "
    "decide a market's outcome — you report what the cited source says, how it "
    "compares with primary sources, and anything that should make a human "
    "hesitate. Be brief and concrete; the admin reads this in a dashboard."
)


def _run(question: str, options: str, criteria: str, proposed: str,
         source: str, today: date | None = None, on_error: str = "") -> str:
    """One advisory check. Never raises — a failed check must not block the
    admin, and it must never read as evidence either way."""
    prompt = llm.load_prompt(
        "aicheck", today=(today or date.today()).isoformat(),
        question=question, options=options, criteria=criteria,
        proposed=proposed, source=source,
    )
    try:
        return llm.research(prompt, system=SYSTEM,
                            max_uses=config.AICHECK_MAX_SEARCHES,
                            max_tokens=1200, stage="aicheck").strip()
    except Exception as exc:
        log.warning("AI check failed: %s", exc)
        return on_error or ("AI patikra nepavyko (techninė klaida) — patikrink "
                            "naujienas rankiniu būdu.")


def check_request(conn: sqlite3.Connection, request_id: int,
                  today: date | None = None) -> str:
    """Return an admin-readable summary for one resolution request."""
    req = conn.execute("SELECT * FROM resolution_requests WHERE id = ?",
                       (request_id,)).fetchone()
    if req is None:
        raise ValueError(f"resolution request {request_id} not found")
    market = conn.execute("SELECT * FROM markets WHERE id = ?",
                          (req["market_id"],)).fetchone()
    if market is None:
        raise ValueError(f"market {req['market_id']} not found")

    return _run(question=market["question_lt"],
                options=" / ".join(json.loads(market["options_json"])),
                criteria=market["resolution_hint_lt"],
                proposed=req["proposed_option"],
                source=req["source_url"],
                today=today,
                # The admin still has to decide; a failed check must not block
                # the dashboard or imply anything about the claim.
                on_error=("AI patikra nepavyko (techninė klaida) — sprendimą "
                          f"priimk pagal šaltinį pats: {req['source_url']}"))


def check_freeze(conn: sqlite3.Connection, market_id: int,
                 today: date | None = None) -> str:
    """Summary for a market frozen by the circuit breaker, where nobody cited
    a source: is there any news that would explain the sudden flow?"""
    market = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if market is None:
        raise ValueError(f"market {market_id} not found")
    return _run(question=market["question_lt"],
                options=" / ".join(json.loads(market["options_json"])),
                criteria=market["resolution_hint_lt"],
                proposed="(nenurodyta — rinka užšaldyta dėl įtartino srauto)",
                source="(nėra — ieškok pats, ar yra naujiena, paaiškinanti judėjimą)",
                today=today)


def check_app_market(market: dict, today: date | None = None) -> str:
    """Same check for a market frozen in the APP, where this repo has no row.

    Markets paused by an admin in the dashboard never existed in the local
    database, which is why `arbus check` used to report "nothing frozen" while
    the app had stopped markets sitting there.
    """
    from . import notify

    view = notify.market_view(market)
    return _run(question=view["question"],
                options=" / ".join(view["options"]),
                criteria=view["rules"],
                proposed="(nenurodyta — rinka sustabdyta appe)",
                source="(nėra — patikrink, ar rezultatas jau žinomas viešai)",
                today=today)


# ── What the admin actually runs: check, store, and ping Telegram ───────────

def pending_requests(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """Resolution requests that froze a market and have not been checked yet."""
    return conn.execute(
        """SELECT r.* FROM resolution_requests r
             JOIN markets m ON m.id = r.market_id
            WHERE r.outcome = '' AND r.ai_checked_at = ''
              AND COALESCE(m.resolution_state, 'OPEN') IN ('PENDING', 'RESOLVING')
            ORDER BY r.created_at
            LIMIT ?""",
        (limit,),
    ).fetchall()


def pending_freezes(conn: sqlite3.Connection, limit: int = 10) -> list[sqlite3.Row]:
    """Markets frozen with nobody's claim attached — the circuit breaker or the
    deadline sweep put them there, so there is no cited source to check."""
    return conn.execute(
        """SELECT m.* FROM markets m
            WHERE m.resolution_state = 'PENDING'
              AND NOT EXISTS (SELECT 1 FROM resolution_requests r
                               WHERE r.market_id = m.id AND r.outcome = '')
            ORDER BY m.frozen_at
            LIMIT ?""",
        (limit,),
    ).fetchall()


def review_request(conn: sqlite3.Connection, request_id: int,
                   today: date | None = None, alert: bool = True) -> str:
    """Check one request, store the summary, and alert Telegram.

    The summary is stored so a failed or repeated run never costs a second
    LLM call, and `alerted_at` records that the team was told — the alert is
    the point, the dashboard is where they act on it.
    """
    summary = check_request(conn, request_id, today)
    conn.execute(
        "UPDATE resolution_requests SET ai_summary = ?, ai_checked_at = ? WHERE id = ?",
        (summary, datetime.now(timezone.utc).isoformat(), request_id))
    req = conn.execute("SELECT * FROM resolution_requests WHERE id = ?",
                       (request_id,)).fetchone()
    market = conn.execute("SELECT * FROM markets WHERE id = ?",
                          (req["market_id"],)).fetchone()
    if alert and notify.notify_resolution(market, req, summary):
        conn.execute("UPDATE resolution_requests SET alerted_at = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(), request_id))
    return summary


def review_freeze(conn: sqlite3.Connection, market_id: int,
                  today: date | None = None, alert: bool = True) -> str:
    """Same, for a market frozen without a user's claim."""
    summary = check_freeze(conn, market_id, today)
    market = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if alert:
        notify.notify_resolution(market, None, summary)
    return summary


# ── markets frozen in the app, which this database has never seen ───────────

def pending_app_markets(conn: sqlite3.Connection, force: bool = False,
                        limit: int = 20) -> tuple[list[dict], str]:
    """Markets the app has paused/stopped and that we have not checked yet."""
    from . import app as app_api

    rows, error = app_api.frozen_markets()
    if error:
        return [], error
    if not force:
        seen = {r["app_market_id"] for r in conn.execute(
            "SELECT app_market_id FROM app_checks")}
        rows = [r for r in rows if app_api.market_id_of(r) not in seen]
    return rows[:limit], ""


def review_app_market(conn: sqlite3.Connection, market: dict,
                      today: date | None = None, alert: bool = True) -> str:
    """Check one app-frozen market, remember it, and alert the group."""
    from . import app as app_api

    summary = check_app_market(market, today)
    conn.execute(
        "INSERT OR REPLACE INTO app_checks (app_market_id, question, status,"
        " ai_summary, checked_at) VALUES (?,?,?,?,?)",
        (app_api.market_id_of(market), app_api.question_of(market),
         app_api.status_of(market), summary,
         datetime.now(timezone.utc).isoformat()))
    if alert:
        notify.notify_resolution(market, None, summary)
    return summary
