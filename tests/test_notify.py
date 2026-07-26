"""The freeze alert: what lands in the team's Telegram group.

Formatting is tested offline (no network) because the alert is the only thing
the team sees before deciding — if a field is missing there, the decision is
made without it.
"""

from arbus import ledger, notify, resolution, store
from arbus.schemas import Candidate

AI = ("ŠALTINIS PATVIRTINA: taip\n"
      "KĄ SAKO ŠALTINIS: nedarbas rugpjūtį siekė 7,2 %.\n"
      "PIRMINIS ŠALTINIS: https://osp.stat.gov.lt/x\n"
      "SIŪLYMAS ADMINUI: patvirtinti")


def _market(conn):
    cand = Candidate(
        question_lt="Ar nedarbas viršys 7 % iki spalio?", market_type="binary",
        options_lt=["Taip", "Ne"], probabilities=[0.4, 0.6],
        category="ekonomika", resolve_by="2026-10-01", duration_class="long",
        resolution_hint_lt="Pagal Statistikos departamento duomenis. Jei "
                           "duomenys nebus paskelbti — rinka baigiasi „Ne“.",
        sources=["https://osp.stat.gov.lt/x"], rationale_en="t",
    )
    mid = store.insert_candidate(conn, cand, "b1", "candidate")
    conn.commit()
    return mid


def test_alert_carries_everything_needed_to_decide():
    conn = store.connect()
    mid = _market(conn)
    ledger.post(conn, "u1", 1000, "settlement")
    ledger.bump(conn, "u1", "predictions", 25)
    rid = resolution.submit_request(conn, mid, "u1", "Taip", "https://lrt.lt/x")
    conn.commit()

    market = store.get_market(conn, mid)
    request = conn.execute("SELECT * FROM resolution_requests WHERE id = ?",
                           (rid,)).fetchone()
    text = notify.resolution_message(market, request, AI)

    assert f"#{mid}" in text and "Ar nedarbas viršys" in text
    assert "Taip" in text and "https://lrt.lt/x" in text     # claim + evidence
    assert "u1" in text                                      # who reported it
    assert "Statistikos departamento" in text                # the deciding rules
    assert "nedarbas rugpjūtį" in text                       # the AI reading
    assert "AI nieko nesprendžia" in text                    # advisory, always


def test_breaker_freeze_alert_says_why_without_a_reporter():
    conn = store.connect()
    mid = _market(conn)
    conn.commit()
    resolution.trip_circuit_breaker(conn, mid, price_move=0.30, distinct_users=4)
    conn.commit()
    text = notify.resolution_message(store.get_market(conn, mid), None, AI)
    assert "circuit breaker" in text
    assert "Pranešė" not in text          # nobody claimed anything


def test_send_is_a_no_op_without_credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert notify.send("hello") is False   # never raises, never blocks resolution
