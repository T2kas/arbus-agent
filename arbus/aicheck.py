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
from datetime import date

from . import config, llm

log = logging.getLogger(__name__)

SYSTEM = (
    "You verify sources for a Lithuanian prediction-market admin. You never "
    "decide a market's outcome — you report what the cited source says, how it "
    "compares with primary sources, and anything that should make a human "
    "hesitate. Be brief and concrete; the admin reads this in a dashboard."
)


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

    prompt = llm.load_prompt(
        "aicheck",
        today=(today or date.today()).isoformat(),
        question=market["question_lt"],
        options=" / ".join(json.loads(market["options_json"])),
        criteria=market["resolution_hint_lt"],
        proposed=req["proposed_option"],
        source=req["source_url"],
    )
    try:
        return llm.research(prompt, system=SYSTEM,
                            max_uses=config.AICHECK_MAX_SEARCHES,
                            max_tokens=1200, stage="aicheck").strip()
    except Exception as exc:
        # The admin still has to decide; a failed check must not block the
        # dashboard or imply anything about the claim.
        log.warning("AI check failed for request %d: %s", request_id, exc)
        return ("AI patikra nepavyko (techninė klaida) — sprendimą priimk "
                f"pagal šaltinį pats: {req['source_url']}")


def check_freeze(conn: sqlite3.Connection, market_id: int,
                 today: date | None = None) -> str:
    """Summary for a market frozen by the circuit breaker, where nobody cited
    a source: is there any news that would explain the sudden flow?"""
    market = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if market is None:
        raise ValueError(f"market {market_id} not found")
    prompt = llm.load_prompt(
        "aicheck",
        today=(today or date.today()).isoformat(),
        question=market["question_lt"],
        options=" / ".join(json.loads(market["options_json"])),
        criteria=market["resolution_hint_lt"],
        proposed="(nenurodyta — rinka užšaldyta dėl įtartino srauto)",
        source="(nėra — ieškok pats, ar yra naujiena, paaiškinanti judėjimą)",
    )
    try:
        return llm.research(prompt, system=SYSTEM,
                            max_uses=config.AICHECK_MAX_SEARCHES,
                            max_tokens=1200, stage="aicheck").strip()
    except Exception as exc:
        log.warning("AI check failed for market %d: %s", market_id, exc)
        return "AI patikra nepavyko (techninė klaida) — patikrink naujienas rankiniu būdu."
