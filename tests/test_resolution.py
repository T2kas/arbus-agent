"""Tests for the resolution engine: freeze, propose, challenge, settle.

Numbers come from the Notion spec "Resolution logika (v1)": proposal bond
200/450, +30 reward for a correct report, 50% of the proposal bond to a correct
challenger. The challenge bond mirrors the bond of the report it disputes
(Polymarket's rule), so it is 200 or 450 accordingly.
"""

from datetime import datetime, timedelta, timezone

import pytest

from arbus import config, ledger, resolution, store
from arbus.schemas import Candidate


def _conn_with_market(options=None, question="Ar nedarbas viršys 7 % iki spalio?"):
    conn = store.connect()
    cand = Candidate(
        question_lt=question, market_type="binary",
        options_lt=options or ["Taip", "Ne"], probabilities=[0.4, 0.6],
        category="ekonomika", resolve_by="2026-10-01", duration_class="long",
        resolution_hint_lt="Pagal Statistikos departamento duomenis.",
        sources=["https://osp.stat.gov.lt/x"], rationale_en="t",
    )
    mid = store.insert_candidate(conn, cand, "b1", "candidate")
    conn.commit()
    return conn, mid


def _fund(conn, user, amount=1000, predictions=25):
    ledger.post(conn, user, amount, "settlement", note="test funding")
    ledger.bump(conn, user, "predictions", predictions)


# ── circuit breaker ─────────────────────────────────────────────────────────

def test_breaker_needs_both_move_and_multiple_users():
    # A whale alone must not trip it — that is prediction, not a leak.
    assert not resolution.circuit_breaker_tripped(0.40, distinct_users=1)
    assert not resolution.circuit_breaker_tripped(0.40, distinct_users=2)
    # Small move with many users is normal trading.
    assert not resolution.circuit_breaker_tripped(0.05, distinct_users=9)
    # Both together = suspicious.
    assert resolution.circuit_breaker_tripped(0.20, distinct_users=3)
    assert resolution.circuit_breaker_tripped(-0.20, distinct_users=5)  # direction-agnostic


def test_tripping_freezes_the_market_once():
    conn, mid = _conn_with_market()
    assert resolution.trip_circuit_breaker(conn, mid, 0.30, 4) is True
    assert resolution._state(conn, mid) == resolution.PENDING
    # already frozen — a second trip changes nothing
    assert resolution.trip_circuit_breaker(conn, mid, 0.30, 4) is False


def test_weak_signal_leaves_market_open():
    conn, mid = _conn_with_market()
    assert resolution.trip_circuit_breaker(conn, mid, 0.30, 1) is False
    assert resolution._state(conn, mid) == resolution.OPEN


# ── resolution requests ─────────────────────────────────────────────────────

def test_request_escrows_bond_and_freezes():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    before = ledger.balance(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://osp.stat.gov.lt/a")
    assert ledger.balance(conn, "u1") == before - config.PROPOSAL_BOND_STANDARD
    assert resolution._state(conn, mid) == resolution.PENDING


def test_important_markets_take_the_larger_bond():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a",
                              is_important=True)
    assert ledger.balance(conn, "u1") == 1000 - config.PROPOSAL_BOND_IMPORTANT


def test_ineligible_user_cannot_report():
    conn, mid = _conn_with_market()
    _fund(conn, "rookie", predictions=2)
    with pytest.raises(ValueError, match="not eligible"):
        resolution.submit_request(conn, mid, "rookie", "Taip", "https://x.lt/a")


def test_report_requires_source_and_funds():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    with pytest.raises(ValueError, match="source URL"):
        resolution.submit_request(conn, mid, "u1", "Taip", "not-a-url")
    _fund(conn, "broke", amount=10)
    with pytest.raises(ValueError, match="insufficient"):
        resolution.submit_request(conn, mid, "broke", "Taip", "https://x.lt/a")


def test_frozen_market_takes_no_further_reports():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    _fund(conn, "u2")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    with pytest.raises(ValueError, match="not open"):
        resolution.submit_request(conn, mid, "u2", "Ne", "https://x.lt/b")


# ── challenges ──────────────────────────────────────────────────────────────

def test_challenge_escrows_bond():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    _fund(conn, "u2")
    rid = resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.submit_challenge(conn, rid, "u2", "šaltinis nesako to")
    assert ledger.balance(conn, "u2") == 1000 - config.PROPOSAL_BOND_STANDARD


def test_challenge_bond_matches_the_report_it_disputes():
    """Polymarket's rule: the dispute bond equals the proposer's bond. A
    challenger forced to risk more than the proposer simply never challenges."""
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    _fund(conn, "u2")
    rid = resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a",
                                    is_important=True)
    resolution.submit_challenge(conn, rid, "u2", "netikiu")
    assert ledger.balance(conn, "u2") == 1000 - config.PROPOSAL_BOND_IMPORTANT


def test_cannot_challenge_your_own_request():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    rid = resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    with pytest.raises(ValueError, match="your own"):
        resolution.submit_challenge(conn, rid, "u1", "persigalvojau")


def test_challenge_window_closes():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    rid = resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    later = datetime.now(timezone.utc) + timedelta(
        hours=config.CHALLENGE_WINDOW_HOURS + 1)
    assert resolution.challenge_window_open(conn, rid) is True
    assert resolution.challenge_window_open(conn, rid, now=later) is False


# ── admin decision + undo window ────────────────────────────────────────────

def test_decision_does_not_settle_immediately():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    balance_before = ledger.balance(conn, "u1")
    settle_at = resolution.admin_decide(conn, mid, resolution.RESOLVED, "Taip")
    assert resolution._state(conn, mid) == resolution.RESOLVING
    assert ledger.balance(conn, "u1") == balance_before   # nothing paid yet
    assert settle_at > datetime.now(timezone.utc)
    assert resolution.due_for_settlement(conn) == []      # not yet due


def test_decision_can_be_undone_inside_the_window():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.admin_decide(conn, mid, resolution.RESOLVED, "Taip")
    resolution.cancel_decision(conn, mid)
    assert resolution._state(conn, mid) == resolution.PENDING
    assert resolution.due_for_settlement(conn) == []


def test_cannot_cancel_what_was_never_decided():
    conn, mid = _conn_with_market()
    with pytest.raises(ValueError, match="nothing to cancel"):
        resolution.cancel_decision(conn, mid)


def test_resolved_requires_a_winning_option():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    with pytest.raises(ValueError, match="requires the winning option"):
        resolution.admin_decide(conn, mid, resolution.RESOLVED)


# ── settlement maths ────────────────────────────────────────────────────────

def _settle_now(conn, mid):
    conn.execute("UPDATE markets SET settle_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(), mid))
    return resolution.settle(conn, mid)


def test_correct_reporter_gets_bond_back_plus_reward():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.admin_decide(conn, mid, resolution.RESOLVED, "Taip")
    _settle_now(conn, mid)
    assert ledger.balance(conn, "u1") == 1000 + config.REWARD_CORRECT_PROPOSAL
    assert ledger.reputation(conn, "u1")["correct_proposals"] == 1
    assert resolution._state(conn, mid) == resolution.RESOLVED


def test_wrong_reporter_loses_the_bond():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.admin_decide(conn, mid, resolution.RESOLVED, "Ne")
    _settle_now(conn, mid)
    assert ledger.balance(conn, "u1") == 1000 - config.PROPOSAL_BOND_STANDARD
    assert ledger.reputation(conn, "u1")["false_proposals"] == 1


def test_correct_challenger_gets_bond_plus_half_the_proposal_bond():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    _fund(conn, "u2")
    rid = resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.submit_challenge(conn, rid, "u2", "šaltinis to nesako")
    resolution.admin_decide(conn, mid, resolution.RESOLVED, "Ne")  # challenger right
    _settle_now(conn, mid)
    expected = int(config.PROPOSAL_BOND_STANDARD * config.CHALLENGE_REWARD_SHARE)
    assert ledger.balance(conn, "u2") == 1000 + expected
    assert ledger.balance(conn, "u1") == 1000 - config.PROPOSAL_BOND_STANDARD
    assert ledger.reputation(conn, "u2")["correct_challenges"] == 1


def test_wrong_challenger_loses_the_bond():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    _fund(conn, "u2")
    rid = resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.submit_challenge(conn, rid, "u2", "netikiu")
    resolution.admin_decide(conn, mid, resolution.RESOLVED, "Taip")  # proposer right
    _settle_now(conn, mid)
    assert ledger.balance(conn, "u2") == 1000 - config.PROPOSAL_BOND_STANDARD
    assert ledger.reputation(conn, "u2")["false_challenges"] == 1


def test_void_needs_an_explicit_reason():
    """A cancelled or postponed event is settled by the market's own rules
    (usually 'Ne'), the way Polymarket and Kalshi write it up front — voiding
    must be a deliberate act, not the easy way out of a hard call."""
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    with pytest.raises(ValueError, match="void_reason"):
        resolution.admin_decide(conn, mid, resolution.VOID)


def test_void_records_the_reason_it_was_allowed():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.admin_decide(conn, mid, resolution.VOID,
                            void_reason="šaltinis nustojo egzistuoti")
    assert "šaltinis nustojo" in store.get_market(conn, mid)["resolution_note"]


def test_void_returns_every_bond():
    """Nobody was proven wrong, so nobody is punished — otherwise good-faith
    reporting on hard markets stops entirely."""
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    _fund(conn, "u2")
    rid = resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.submit_challenge(conn, rid, "u2", "neaišku")
    resolution.admin_decide(conn, mid, resolution.VOID,
                            void_reason="rinkos taisyklės neįgyvendinamos")
    _settle_now(conn, mid)
    assert ledger.balance(conn, "u1") == 1000
    assert ledger.balance(conn, "u2") == 1000
    assert resolution._state(conn, mid) == resolution.VOID


def test_back_to_open_returns_bonds_and_resumes_trading():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.admin_decide(conn, mid, resolution.OPEN)
    _settle_now(conn, mid)
    assert ledger.balance(conn, "u1") == 1000
    assert resolution._state(conn, mid) == resolution.OPEN
    row = store.get_market(conn, mid)
    assert row["freeze_reason"] == ""     # unfrozen, tradeable again


def test_settle_due_only_pays_after_the_window():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.admin_decide(conn, mid, resolution.RESOLVED, "Taip")
    assert resolution.settle_due(conn) == []              # inside the window
    future = datetime.now(timezone.utc) + timedelta(
        minutes=config.SETTLEMENT_DELAY_MINUTES + 1)
    assert len(resolution.settle_due(conn, now=future)) == 1


def test_settlement_cannot_run_twice():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.admin_decide(conn, mid, resolution.RESOLVED, "Taip")
    _settle_now(conn, mid)
    with pytest.raises(ValueError, match="not awaiting settlement"):
        resolution.settle(conn, mid)


def test_ledger_explains_every_movement():
    conn, mid = _conn_with_market()
    _fund(conn, "u1")
    resolution.submit_request(conn, mid, "u1", "Taip", "https://x.lt/a")
    resolution.admin_decide(conn, mid, resolution.RESOLVED, "Taip")
    _settle_now(conn, mid)
    kinds = [r["kind"] for r in ledger.history(conn, "u1")]
    assert "bond_escrow" in kinds and "bond_return" in kinds and "reward" in kinds
