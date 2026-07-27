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


# ── the verdict header + URL verification (kill hallucinations) ─────────────

def test_no_source_confident_result_is_flagged(monkeypatch):
    from arbus import aicheck

    # The Dirkstys failure: claimed a confirmed win, source "nerasta".
    dirkstys = (
        "REZULTATAS: žinomas\n"
        "KAS ĮVYKO: 2025-12-14 Deividas Dirkstys nugalėjo Maslabojevą.\n"
        "ŠALTINIS: nerasta\n"
        "SIŪLOMA BAIGTIS: TAIP\n"
        "PASITIKĖJIMAS: aukštas")
    out = aicheck._finalize(dirkstys, verify=False)
    assert out.startswith("⚠️") and "nepateikė jokios nuorodos" in out
    assert dirkstys in out                           # model text kept


def test_broken_url_is_caught_as_hallucination(monkeypatch):
    """The Eurovision/Sabonis failure: a plausible URL that 404s."""
    from arbus import aicheck

    monkeypatch.setattr(aicheck, "verify_url", lambda u, **k: "broken")
    text = (
        "REZULTATAS: žinomas\n"
        "KAS ĮVYKO: 2026 m. Lietuva pateko į finalą.\n"
        "ŠALTINIS: https://eurovision.tv/story/made-up-2026\n"
        "SIŪLOMA BAIGTIS: Taip\n"
        "PASITIKĖJIMAS: aukštas")
    out = aicheck._finalize(text, verify=True)
    assert out.startswith("⚠️ GALIMA HALIUCINACIJA") and "NEVEIKIA" in out


def test_working_url_earns_the_green_check(monkeypatch):
    from arbus import aicheck

    monkeypatch.setattr(aicheck, "verify_url", lambda u, **k: "ok")
    text = (
        "REZULTATAS: žinomas\n"
        "KAS ĮVYKO: 2026-07-20 M. Sinkevičius paskirtas premjeru.\n"
        "ŠALTINIS: https://www.lrs.lt/x\n"
        "SIŪLOMA BAIGTIS: Taip\n"
        "PASITIKĖJIMAS: aukštas")
    out = aicheck._finalize(text, verify=True)
    assert out.startswith("✅ AI ŽINO REZULTATĄ") and "Taip" in out


def test_honest_unknown_gets_the_red_cross():
    from arbus import aicheck

    unknown = (
        "REZULTATAS: nežinomas\n"
        "KAS ĮVYKO: dar neįvyko — kova numatyta rugsėjį.\n"
        "ŠALTINIS: nerasta\n"
        "SIŪLOMA BAIGTIS: dar neaišku\n"
        "PASITIKĖJIMAS: žemas")
    out = aicheck._finalize(unknown, verify=False)
    assert out.startswith("❌ AI NEŽINO")


def test_unknown_verification_does_not_condemn_a_link():
    """In the sandbox every fetch fails; a real source must not be called fake
    just because the network is down."""
    from arbus import aicheck

    assert aicheck.verify_url("https://definitely-not-resolvable.invalid") == "unknown"
