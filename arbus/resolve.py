"""Job 2 — resolution monitoring.

Job 1 creates markets; this closes them. For every market whose resolution date
has arrived, it checks the live web against that market's own resolution
criteria and proposes a verdict with evidence.

Two principles shape the design:

1. **Resolving wrongly is worse than resolving late.** Users lose credits they
   earned, and a wrong resolution is far harder to undo than a slow one. So a
   verdict is only auto-applied when the model reports HIGH confidence AND
   names a source; everything else is queued for a human. Nothing here writes
   to the app on its own — `arbus resolve --apply` records the verdict locally,
   and publishing stays a separate, deliberate step.

2. **The market's own criteria are the contract.** The `resolution_hint_lt`
   written at creation time decides the outcome, not the checker's opinion of
   what the question "really" meant.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from datetime import date, datetime, timedelta, timezone

from . import config, llm

log = logging.getLogger(__name__)

VERDICTS = ("RESOLVED", "OPEN", "VOID", "UNCLEAR")
CONFIDENCES = ("HIGH", "MEDIUM", "LOW")

SYSTEM = (
    "You are a careful resolver for a Lithuanian prediction-market app. You "
    "decide whether a market's outcome is publicly known, using its own stated "
    "resolution criteria. You never guess: an unresolved market costs nothing, "
    "a wrongly resolved one costs users their credits."
)

# "3: RESOLVED | Taip | HIGH | reason | https://..."
LINE_RE = re.compile(
    r"^\s*\**(\d+)\**\s*[:.\-]\s*\**(RESOLVED|OPEN|VOID|UNCLEAR)\**\s*"
    r"(?:\|(?P<option>[^|]*))?"
    r"(?:\|(?P<conf>[^|]*))?"
    r"(?:\|(?P<reason>[^|]*))?"
    r"(?:\|(?P<url>.*))?$",
    re.MULTILINE,
)


def due_markets(conn: sqlite3.Connection, today: date,
                grace_days: int = config.RESOLVE_GRACE_DAYS,
                limit: int = 100) -> list[sqlite3.Row]:
    """Markets whose resolution date has arrived and that are still unresolved.

    A grace period is applied because sources publish after the fact: a market
    resolving on the 1st may only be checkable on the 3rd. Markets already
    resolved, rejected, or voided are excluded.
    """
    cutoff = (today - timedelta(days=grace_days)).isoformat()
    return conn.execute(
        """SELECT * FROM markets
           WHERE resolve_by <= ?
             AND status NOT IN ('rejected', 'resolved', 'void')
             AND (resolution_verdict IS NULL OR resolution_verdict = '')
             AND COALESCE(resolution_state, 'OPEN') = 'OPEN'
           ORDER BY resolve_by
           LIMIT ?""",
        (cutoff, limit),
    ).fetchall()


def _prompt(rows: list[sqlite3.Row], today: date) -> str:
    lines = []
    for i, row in enumerate(rows, 1):
        options = json.loads(row["options_json"])
        lines.append(
            f"{i}. {row['question_lt']}\n"
            f"   options: {' / '.join(options)}\n"
            f"   resolution criteria: {row['resolution_hint_lt']}\n"
            f"   resolve_by: {row['resolve_by']}\n"
            f"   sources used when created: {' | '.join(json.loads(row['sources_json'])[:3])}"
        )
    return llm.load_prompt("resolve", today=today.isoformat(),
                           markets="\n".join(lines))


def _parse(text: str, n: int) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for m in LINE_RE.finditer(text):
        idx = int(m.group(1))
        if not 1 <= idx <= n:
            continue
        conf = (m.group("conf") or "").strip().upper()
        out[idx] = {
            "verdict": m.group(2),
            "option": (m.group("option") or "").strip().strip("-").strip(),
            "confidence": conf if conf in CONFIDENCES else "LOW",
            "reason": (m.group("reason") or "").strip(),
            "source": (m.group("url") or "").strip(),
        }
    return out


def check_markets(rows: list[sqlite3.Row], today: date) -> dict[int, dict]:
    """Return {row_index (1-based): verdict dict} for a batch of markets."""
    results: dict[int, dict] = {}
    size = config.RESOLVE_CHUNK_SIZE
    for start in range(0, len(rows), size):
        chunk = rows[start : start + size]
        try:
            text = llm.research(_prompt(chunk, today), system=SYSTEM,
                                max_uses=config.SEARCH_MAX_USES_VERIFY,
                                max_tokens=6000, stage="resolve")
            parsed = _parse(text, len(chunk))
            if len(parsed) < len(chunk):
                log.warning("resolve: parsed %d/%d verdicts; raw head: %s",
                            len(parsed), len(chunk), text[:200])
        except Exception as exc:
            log.warning("resolution chunk failed: %s", exc)
            parsed = {}
        for i in range(1, len(chunk) + 1):
            results[start + i] = parsed.get(
                i, {"verdict": "UNCLEAR", "option": "", "confidence": "LOW",
                    "reason": "resolution stage failed (technical)", "source": ""}
            )
    return results


def should_freeze(verdict: dict, options: list[str]) -> tuple[bool, str]:
    """Whether a sweep verdict is strong enough to FREEZE the market for review.

    The AI never resolves anything (see the Notion spec: "AI netrenkia
    sprendimų"). The strongest action it can take on its own is to stop trading
    and put the market in front of the admin — which is exactly what protects
    the AMM, since the loss comes from trading on a stale price, not from the
    verdict being recorded a few minutes later.

    Freezing needs a RESOLVED verdict at HIGH confidence with a cited source
    naming an option the market actually has; anything weaker is reported and
    left alone.
    """
    if verdict["verdict"] != "RESOLVED":
        return False, f"verdict is {verdict['verdict']}"
    if verdict["confidence"] != "HIGH":
        return False, f"confidence is {verdict['confidence']}"
    if not verdict["source"].startswith("http"):
        return False, "no source URL cited"
    match = next((o for o in options if o.strip().lower() == verdict["option"].lower()),
                 None)
    if match is None:
        return False, f"option {verdict['option']!r} is not one of the market's options"
    return True, match


def record(conn: sqlite3.Connection, market_id: int, verdict: dict,
           freeze: bool, note: str) -> None:
    """Store the sweep's findings, and freeze the market when they warrant it.

    Freezing stops trading and hands the market to the admin; it never settles
    anything. Settlement happens only through resolution.admin_decide() plus
    the undo window.
    """
    conn.execute(
        """UPDATE markets
              SET resolution_verdict = ?, resolution_option = ?,
                  resolution_confidence = ?, resolution_note = ?,
                  resolution_source = ?
            WHERE id = ?""",
        (verdict["verdict"], verdict["option"], verdict["confidence"],
         note[:300], verdict["source"][:300], market_id),
    )
    if freeze:
        conn.execute(
            """UPDATE markets
                  SET resolution_state = 'PENDING', freeze_reason = ?, frozen_at = ?
                WHERE id = ? AND resolution_state = 'OPEN'""",
            (f"deadline sweep: {verdict['verdict']} {verdict['option']}".strip(),
             datetime.now(timezone.utc).isoformat(), market_id),
        )
