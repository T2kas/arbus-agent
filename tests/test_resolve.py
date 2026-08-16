"""Offline tests for Job 2 — resolution monitoring."""

from datetime import date

from arbus import resolve, store
from arbus.schemas import Candidate

TODAY = date(2026, 9, 20)


def _market(conn, question="Ar nedarbo lygis viršys 7 % iki rugsėjo?",
            resolve_by="2026-09-15", options=None, status="candidate"):
    cand = Candidate(
        question_lt=question, market_type="binary",
        options_lt=options or ["Taip", "Ne"], probabilities=[0.4, 0.6],
        category="ekonomika", resolve_by=resolve_by, duration_class="long",
        resolution_hint_lt="Pagal Statistikos departamento duomenis.",
        sources=["https://osp.stat.gov.lt/x"], rationale_en="t",
    )
    return store.insert_candidate(conn, cand, "b1", status)


# ── which markets come due ──────────────────────────────────────────────────

def test_due_markets_respects_date_status_and_grace():
    conn = store.connect()
    due = _market(conn, resolve_by="2026-09-15")
    _market(conn, question="Ar kita rinka iki gruodžio?", resolve_by="2026-12-01")
    _market(conn, question="Ar atmesta rinka iki rugsėjo?", resolve_by="2026-09-01",
            status="rejected")
    conn.commit()
    ids = [r["id"] for r in resolve.due_markets(conn, TODAY)]
    assert ids == [due]                       # future and rejected excluded


def test_already_checked_markets_are_not_rechecked():
    conn = store.connect()
    mid = _market(conn, resolve_by="2026-09-01")
    conn.commit()
    resolve.record(conn, mid, {"verdict": "RESOLVED", "option": "Taip",
                               "confidence": "HIGH", "source": "https://x.lt/a"},
                   freeze=True, note="done")
    conn.commit()
    assert resolve.due_markets(conn, TODAY) == []


# ── parsing ─────────────────────────────────────────────────────────────────

def test_parses_full_verdict_lines():
    text = ("1: RESOLVED | Taip | HIGH | Nedarbas 7,2 % | https://osp.stat.gov.lt/a\n"
            "2: OPEN | - | LOW | Dar nepaskelbta | \n"
            "3: VOID | - | HIGH | Renginys atšauktas | https://lrt.lt/b\n")
    out = resolve._parse(text, 3)
    assert out[1]["verdict"] == "RESOLVED" and out[1]["option"] == "Taip"
    assert out[1]["confidence"] == "HIGH"
    assert out[1]["source"] == "https://osp.stat.gov.lt/a"
    assert out[2]["verdict"] == "OPEN" and out[2]["option"] == ""
    assert out[3]["verdict"] == "VOID"


def test_unknown_confidence_defaults_to_low():
    out = resolve._parse("1: RESOLVED | Taip | tikrai | ok | https://x.lt/a", 1)
    assert out[1]["confidence"] == "LOW"


def test_missing_lines_become_unclear_not_resolved():
    rows = []
    parsed = resolve._parse("no verdicts here", 2)
    assert parsed == {}
    # check_markets fills the gap defensively — see the auto-apply gate below
    assert rows == []


# ── the gate: what the sweep may FREEZE (it never settles) ──────────────────

OPTIONS = ["Taip", "Ne"]


def test_high_confidence_resolved_with_source_freezes():
    ok, detail = resolve.should_freeze(
        {"verdict": "RESOLVED", "option": "taip", "confidence": "HIGH",
         "source": "https://osp.stat.gov.lt/a"}, OPTIONS)
    assert ok and detail == "Taip"       # returns the canonical option casing


def test_medium_and_low_confidence_leave_the_market_trading():
    for conf in ("MEDIUM", "LOW"):
        ok, detail = resolve.should_freeze(
            {"verdict": "RESOLVED", "option": "Taip", "confidence": conf,
             "source": "https://x.lt/a"}, OPTIONS)
        assert not ok and conf.lower() in detail.lower()


def test_resolved_without_source_does_not_freeze():
    ok, detail = resolve.should_freeze(
        {"verdict": "RESOLVED", "option": "Taip", "confidence": "HIGH",
         "source": ""}, OPTIONS)
    assert not ok and "source" in detail


def test_option_not_on_the_market_does_not_freeze():
    ok, detail = resolve.should_freeze(
        {"verdict": "RESOLVED", "option": "Gal būt", "confidence": "HIGH",
         "source": "https://x.lt/a"}, OPTIONS)
    assert not ok and "not one of" in detail


def test_open_void_and_unclear_never_freeze():
    for verdict in ("OPEN", "VOID", "UNCLEAR"):
        ok, _ = resolve.should_freeze(
            {"verdict": verdict, "option": "Taip", "confidence": "HIGH",
             "source": "https://x.lt/a"}, OPTIONS)
        assert not ok


# ── recording: the sweep freezes, it never settles ──────────────────────────

def test_freezing_stops_trading_without_settling():
    conn = store.connect()
    mid = _market(conn)
    conn.commit()
    resolve.record(conn, mid, {"verdict": "RESOLVED", "option": "Taip",
                               "confidence": "HIGH", "source": "https://x.lt/a"},
                   freeze=True, note="Nedarbas 7,2 %")
    conn.commit()
    row = store.get_market(conn, mid)
    assert row["resolution_state"] == "PENDING"   # admin now decides
    assert row["status"] == "candidate"           # NOT settled
    assert row["resolved_at"] == ""
    assert row["resolution_option"] == "Taip"


def test_weak_verdict_records_findings_but_leaves_state_open():
    conn = store.connect()
    mid = _market(conn)
    conn.commit()
    resolve.record(conn, mid, {"verdict": "RESOLVED", "option": "Taip",
                               "confidence": "LOW", "source": ""},
                   freeze=False, note="neaišku")
    conn.commit()
    row = store.get_market(conn, mid)
    assert row["resolution_state"] == "OPEN"      # still trading
    assert row["resolution_verdict"] == "RESOLVED"


def test_freeze_does_not_override_an_already_frozen_market():
    conn = store.connect()
    mid = _market(conn)
    conn.commit()
    conn.execute("UPDATE markets SET resolution_state = 'RESOLVING' WHERE id = ?", (mid,))
    resolve.record(conn, mid, {"verdict": "RESOLVED", "option": "Taip",
                               "confidence": "HIGH", "source": "https://x.lt/a"},
                   freeze=True, note="x")
    conn.commit()
    assert store.get_market(conn, mid)["resolution_state"] == "RESOLVING"
