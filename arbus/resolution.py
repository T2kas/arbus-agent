"""The resolution engine — freeze, propose, challenge, decide, settle.

Implements the Notion spec "Resolution logika (supaprastintas draft)".

Why it is shaped this way:

* **Freezing is instant and cheap.** Arbus runs an AMM and takes the other side
  of user bets, so every minute a market trades on a stale price after the
  outcome is knowable is money leaking to whoever knows. Both entry paths —
  the circuit breaker and a user's resolution request — stop trading
  immediately, before any checking happens.
* **The AI never decides.** It reads the cited source and reports whether it
  actually says what the proposer claims. That summary goes to the admin; it
  changes no state and moves no Arbucks.
* **Settlement is delayed, not instant.** The admin decides in the app
  dashboard, and the payout waits SETTLEMENT_DELAY_MINUTES so a misclick can be
  undone. Once Arbucks are paid out there is no way back.

States: OPEN → PENDING → RESOLVING → RESOLVED / VOID, or back to OPEN when the
event turns out not to have happened.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config, ledger

log = logging.getLogger(__name__)

OPEN, PENDING, RESOLVING, RESOLVED, VOID = (
    "OPEN", "PENDING", "RESOLVING", "RESOLVED", "VOID")

SCHEMA = """
CREATE TABLE IF NOT EXISTS resolution_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    proposed_option TEXT NOT NULL,
    source_url TEXT NOT NULL,
    bond INTEGER NOT NULL,
    outcome TEXT NOT NULL DEFAULT '',   -- '' | correct | incorrect
    created_at TEXT NOT NULL,
    ai_summary TEXT NOT NULL DEFAULT '',
    ai_checked_at TEXT NOT NULL DEFAULT '',
    alerted_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS challenges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id INTEGER NOT NULL,
    market_id INTEGER NOT NULL,
    user_id TEXT NOT NULL,
    bond INTEGER NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    outcome TEXT NOT NULL DEFAULT '',   -- '' | correct | incorrect
    created_at TEXT NOT NULL
);
"""


# Columns added after the first release. SQLite has no "ADD COLUMN IF NOT
# EXISTS", so they are applied in place on every init — the tables already live
# in a database that must not be rebuilt.
_ADDED_REQUEST_COLUMNS = {
    "ai_summary": "TEXT NOT NULL DEFAULT ''",
    "ai_checked_at": "TEXT NOT NULL DEFAULT ''",
    "alerted_at": "TEXT NOT NULL DEFAULT ''",
}


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    have = {r["name"] for r in conn.execute("PRAGMA table_info(resolution_requests)")}
    for column, decl in _ADDED_REQUEST_COLUMNS.items():
        if column not in have:
            conn.execute(f"ALTER TABLE resolution_requests ADD COLUMN {column} {decl}")
    ledger.init(conn)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _state(conn: sqlite3.Connection, market_id: int) -> str:
    row = conn.execute("SELECT resolution_state FROM markets WHERE id = ?",
                       (market_id,)).fetchone()
    if row is None:
        raise ValueError(f"market {market_id} not found")
    return row["resolution_state"] or OPEN


def _set(conn: sqlite3.Connection, market_id: int, **fields) -> None:
    cols = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE markets SET {cols} WHERE id = ?",
                 (*fields.values(), market_id))


# ── Entry path 1: the circuit breaker ───────────────────────────────────────

def circuit_breaker_tripped(price_move: float, distinct_users: int) -> bool:
    """Whether a price move looks like leaked information rather than a punt.

    A big move from ONE account is just someone predicting confidently — a
    whale, not a leak — so the move only counts when several distinct users
    push the same way inside the window. Requiring both is what stops the
    breaker firing on every large bet.
    """
    return (abs(price_move) >= config.CB_PRICE_MOVE
            and distinct_users >= config.CB_MIN_DISTINCT_USERS)


def trip_circuit_breaker(conn: sqlite3.Connection, market_id: int,
                         price_move: float, distinct_users: int,
                         window_minutes: int = config.CB_WINDOW_MINUTES) -> bool:
    """Freeze a market on suspicious flow. Returns True if it froze it now."""
    if not circuit_breaker_tripped(price_move, distinct_users):
        return False
    if _state(conn, market_id) != OPEN:
        return False  # already frozen or settled — nothing to do
    reason = (f"circuit breaker: {price_move:+.0%} move with {distinct_users} "
              f"distinct users in {window_minutes} min")
    _set(conn, market_id, resolution_state=PENDING, freeze_reason=reason,
         frozen_at=_now().isoformat())
    log.info("market %d frozen — %s", market_id, reason)
    return True


# ── Entry path 2: a user reports the outcome ────────────────────────────────

def is_eligible(conn: sqlite3.Connection, user_id: str) -> tuple[bool, str]:
    rep = ledger.reputation(conn, user_id)
    if rep["predictions"] < config.ELIGIBILITY_MIN_PREDICTIONS:
        return False, (f"needs {config.ELIGIBILITY_MIN_PREDICTIONS} predictions, "
                       f"has {rep['predictions']}")
    return True, ""


def bond_for(is_important: bool) -> int:
    return (config.PROPOSAL_BOND_IMPORTANT if is_important
            else config.PROPOSAL_BOND_STANDARD)


def submit_request(conn: sqlite3.Connection, market_id: int, user_id: str,
                   proposed_option: str, source_url: str,
                   is_important: bool = False) -> int:
    """A user stakes a bond to claim the outcome is known. Freezes the market."""
    state = _state(conn, market_id)
    if state != OPEN:
        raise ValueError(f"market {market_id} is {state}, not open for reports")
    eligible, why = is_eligible(conn, user_id)
    if not eligible:
        raise ValueError(f"user not eligible: {why}")
    if not source_url.startswith("http"):
        raise ValueError("a source URL is required")

    bond = bond_for(is_important)
    if ledger.balance(conn, user_id) < bond:
        raise ValueError(f"insufficient balance for the {bond} Arbucks bond")

    ledger.post(conn, user_id, -bond, "bond_escrow", market_id,
                f"resolution request: {proposed_option}")
    cur = conn.execute(
        "INSERT INTO resolution_requests (market_id, user_id, proposed_option,"
        " source_url, bond, created_at) VALUES (?,?,?,?,?,?)",
        (market_id, user_id, proposed_option, source_url, bond, _now().isoformat()),
    )
    _set(conn, market_id, resolution_state=PENDING,
         freeze_reason=f"resolution request by {user_id}",
         frozen_at=_now().isoformat())
    return cur.lastrowid


def challenge_window_open(conn: sqlite3.Connection, request_id: int,
                          now: datetime | None = None) -> bool:
    row = conn.execute("SELECT created_at FROM resolution_requests WHERE id = ?",
                       (request_id,)).fetchone()
    if row is None:
        return False
    created = datetime.fromisoformat(row["created_at"])
    return (now or _now()) < created + timedelta(hours=config.CHALLENGE_WINDOW_HOURS)


def submit_challenge(conn: sqlite3.Connection, request_id: int, user_id: str,
                     reason: str, source_url: str = "") -> int:
    """Another user stakes a bond to dispute a resolution request."""
    req = conn.execute("SELECT * FROM resolution_requests WHERE id = ?",
                       (request_id,)).fetchone()
    if req is None:
        raise ValueError(f"resolution request {request_id} not found")
    if req["user_id"] == user_id:
        raise ValueError("cannot challenge your own resolution request")
    if not challenge_window_open(conn, request_id):
        raise ValueError("challenge window has closed")
    bond = config.CHALLENGE_BOND
    if ledger.balance(conn, user_id) < bond:
        raise ValueError(f"insufficient balance for the {bond} Arbucks bond")

    ledger.post(conn, user_id, -bond, "bond_escrow", req["market_id"],
                f"challenge of request {request_id}")
    cur = conn.execute(
        "INSERT INTO challenges (request_id, market_id, user_id, bond, reason,"
        " source_url, created_at) VALUES (?,?,?,?,?,?,?)",
        (request_id, req["market_id"], user_id, bond, reason[:300], source_url,
         _now().isoformat()),
    )
    return cur.lastrowid


# ── Admin decision, with an undo window ─────────────────────────────────────

def admin_decide(conn: sqlite3.Connection, market_id: int, decision: str,
                 option: str = "", admin: str = "admin",
                 void_reason: str = "") -> datetime:
    """Record the admin's decision and schedule settlement.

    Settlement is not immediate: it lands after SETTLEMENT_DELAY_MINUTES so a
    misclick can still be cancelled. Once Arbucks are paid out, they cannot be
    taken back.

    There are normally only two decisions:

    * RESOLVED — the winning option, per the market's own rules. A cancelled
      concert or a postponed vote is NOT a reason to void: the rules already
      say what non-occurrence means (usually "Ne"), the same way Polymarket and
      Kalshi write it into the rules up front.
    * OPEN — the event had not actually happened, the freeze was premature and
      trading resumes.

    VOID is a last resort for a market that is genuinely unusable (contradictory
    rules, a resolution source that no longer exists). It requires
    `void_reason`, so choosing it is deliberate and auditable rather than the
    easy way out of a hard call.
    """
    if decision not in (RESOLVED, VOID, OPEN):
        raise ValueError(f"invalid decision: {decision}")
    if decision == VOID and not void_reason:
        raise ValueError(
            "VOID needs void_reason — a cancelled or postponed event is settled "
            "by the market's own rules (usually 'Ne'), not voided. Void only a "
            "market whose rules cannot be applied at all.")
    state = _state(conn, market_id)
    if state not in (PENDING, RESOLVING):
        raise ValueError(f"market {market_id} is {state}; nothing to decide")
    if decision == RESOLVED and not option:
        raise ValueError("RESOLVED requires the winning option")

    settle_at = _now() + timedelta(minutes=config.SETTLEMENT_DELAY_MINUTES)
    _set(conn, market_id, resolution_state=RESOLVING, admin_decision=decision,
         resolution_option=option, decided_by=admin,
         decided_at=_now().isoformat(), settle_at=settle_at.isoformat())
    if void_reason:
        _set(conn, market_id, resolution_note=f"VOID: {void_reason}"[:300])
    return settle_at


def cancel_decision(conn: sqlite3.Connection, market_id: int) -> None:
    """Undo a pending decision during the delay window. Frozen, not settled."""
    if _state(conn, market_id) != RESOLVING:
        raise ValueError("nothing to cancel — the market is not awaiting settlement")
    _set(conn, market_id, resolution_state=PENDING, admin_decision="",
         resolution_option="", settle_at="")


def due_for_settlement(conn: sqlite3.Connection,
                       now: datetime | None = None) -> list[sqlite3.Row]:
    now = now or _now()
    return [r for r in conn.execute(
        "SELECT * FROM markets WHERE resolution_state = ? AND settle_at != ''",
        (RESOLVING,)).fetchall()
        if datetime.fromisoformat(r["settle_at"]) <= now]


# ── Settlement: the only place Arbucks move ─────────────────────────────────

def settle(conn: sqlite3.Connection, market_id: int) -> dict:
    """Pay out bonds and rewards for a decided market. Idempotent per market."""
    row = conn.execute("SELECT * FROM markets WHERE id = ?", (market_id,)).fetchone()
    if row is None:
        raise ValueError(f"market {market_id} not found")
    if row["resolution_state"] != RESOLVING:
        raise ValueError(f"market {market_id} is {row['resolution_state']}, not awaiting settlement")

    decision = row["admin_decision"]
    winning = row["resolution_option"]
    summary = {"market_id": market_id, "decision": decision, "option": winning,
               "paid": [], "forfeited": []}

    requests = conn.execute(
        "SELECT * FROM resolution_requests WHERE market_id = ? AND outcome = ''",
        (market_id,)).fetchall()

    for req in requests:
        challenges = conn.execute(
            "SELECT * FROM challenges WHERE request_id = ? AND outcome = ''",
            (req["id"],)).fetchall()

        if decision == RESOLVED:
            proposer_right = req["proposed_option"].strip().lower() == winning.strip().lower()
        else:
            # VOID or back-to-OPEN: nobody was proven right, so bonds are
            # returned rather than taken. Punishing a good-faith report the
            # admin could not confirm would kill reporting entirely.
            proposer_right = None

        if proposer_right is True:
            ledger.post(conn, req["user_id"], req["bond"], "bond_return", market_id,
                        "correct resolution request")
            ledger.post(conn, req["user_id"], config.REWARD_CORRECT_PROPOSAL,
                        "reward", market_id, "first correct report")
            ledger.bump(conn, req["user_id"], "correct_proposals")
            conn.execute("UPDATE resolution_requests SET outcome = 'correct' WHERE id = ?",
                         (req["id"],))
            summary["paid"].append((req["user_id"], req["bond"] + config.REWARD_CORRECT_PROPOSAL))
        elif proposer_right is False:
            ledger.bump(conn, req["user_id"], "false_proposals")
            conn.execute("UPDATE resolution_requests SET outcome = 'incorrect' WHERE id = ?",
                         (req["id"],))
            summary["forfeited"].append((req["user_id"], req["bond"]))
        else:
            ledger.post(conn, req["user_id"], req["bond"], "bond_return", market_id,
                        f"{decision.lower()}: bond returned")
            summary["paid"].append((req["user_id"], req["bond"]))

        for ch in challenges:
            # A challenge is right exactly when the proposal it disputed was wrong.
            if proposer_right is False:
                reward = int(req["bond"] * config.CHALLENGE_REWARD_SHARE)
                ledger.post(conn, ch["user_id"], ch["bond"], "bond_return", market_id,
                            "correct challenge")
                ledger.post(conn, ch["user_id"], reward, "reward", market_id,
                            "challenge reward (50% of proposal bond)")
                ledger.bump(conn, ch["user_id"], "correct_challenges")
                conn.execute("UPDATE challenges SET outcome = 'correct' WHERE id = ?",
                             (ch["id"],))
                summary["paid"].append((ch["user_id"], ch["bond"] + reward))
            elif proposer_right is True:
                ledger.bump(conn, ch["user_id"], "false_challenges")
                conn.execute("UPDATE challenges SET outcome = 'incorrect' WHERE id = ?",
                             (ch["id"],))
                summary["forfeited"].append((ch["user_id"], ch["bond"]))
            else:
                ledger.post(conn, ch["user_id"], ch["bond"], "bond_return", market_id,
                            f"{decision.lower()}: bond returned")
                summary["paid"].append((ch["user_id"], ch["bond"]))

    final = {RESOLVED: RESOLVED, VOID: VOID, OPEN: OPEN}[decision]
    _set(conn, market_id, resolution_state=final, settle_at="",
         resolved_at=_now().isoformat() if final != OPEN else "",
         status="resolved" if final == RESOLVED else
                ("void" if final == VOID else row["status"]))
    if final == OPEN:  # event had not happened — trading resumes
        _set(conn, market_id, freeze_reason="", frozen_at="", admin_decision="")
    return summary


def settle_due(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    return [settle(conn, row["id"]) for row in due_for_settlement(conn, now)]
