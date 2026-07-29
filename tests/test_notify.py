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
    assert out.startswith("⚠️") and "be jokios nuorodos" in out
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
    assert out.startswith("⚠️ GALIMA HALIUCINACIJA") and "NEEGZISTUOJA" in out


def test_dar_neaisku_is_never_a_hallucination_even_with_a_bad_url(monkeypatch):
    """The Ignitis case: a future threshold, correctly 'dar neaišku'. Even
    though nasdaqbaltic.com refused the fetch, this is not a hallucination —
    there is simply no actionable outcome yet, so it is ❌, not ⚠️."""
    from arbus import aicheck

    monkeypatch.setattr(aicheck, "verify_url", lambda u, **k: "broken")
    text = (
        "REZULTATAS: žinomas\n"
        "KAS ĮVYKO: iki šiol maksimumas 22,70 €, žemiau 23 €.\n"
        "ŠALTINIS: https://nasdaqbaltic.com/statistics/lt/shares/vilnius/IGN1L\n"
        "SIŪLOMA BAIGTIS: dar neaišku\n"
        "PASITIKĖJIMAS: vidutinis")
    out = aicheck._finalize(text, verify=True)
    assert out.startswith("❌ AI NEŽINO")


def test_site_that_refuses_the_bot_is_not_called_fake(monkeypatch):
    """403/429/timeout is 'the site blocked us', not 'the link is fabricated'."""
    from arbus import aicheck

    class R:
        status_code = 403
    monkeypatch.setattr(aicheck.requests, "get", lambda *a, **k: R())
    assert aicheck.verify_url("https://nasdaqbaltic.com/x") == "unknown"

    class G:
        status_code = 404
    monkeypatch.setattr(aicheck.requests, "get", lambda *a, **k: G())
    assert aicheck.verify_url("https://eurovision.tv/made-up") == "broken"


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
    assert out.startswith("✅ AI SIŪLO: Taip") and "nuoroda veikia" in out


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


# ── provider fallback: a bad second key must not kill the check ──────────────

def test_aicheck_falls_back_to_a_working_provider(monkeypatch):
    """LLM_PROVIDER_AICHECK=perplexity with no valid key returned 401 and took
    the whole check down. It must fall back to the Anthropic key that works."""
    import requests
    from arbus import aicheck, llm

    monkeypatch.setattr(llm, "provider", lambda stage=None: "perplexity")
    monkeypatch.setattr(llm, "available_providers", lambda: ["anthropic", "perplexity"])

    calls = []

    def fake_research(*a, force_provider=None, **k):
        calls.append(force_provider)
        if force_provider == "perplexity":
            raise requests.HTTPError("401 Client Error: Unauthorized")
        return ("REZULTATAS: žinomas\nŠALTINIS: https://lrt.lt/x\n"
                "SIŪLOMA BAIGTIS: Taip")

    monkeypatch.setattr(llm, "research", fake_research)
    monkeypatch.setattr(aicheck, "verify_url", lambda u, **k: "ok")
    monkeypatch.setattr(aicheck.resolvers, "facts_for", lambda *a, **k: "")

    out = aicheck._run("q", "Taip / Ne", "rules", "Taip", "src", verify=None) \
        if False else aicheck._run("q", "Taip / Ne", "rules", "Taip", "src")
    assert calls == ["perplexity", "anthropic"]        # tried primary, then fell back
    assert "anthropic" in out and out.startswith("✅")   # note + real verdict


def test_aicheck_reports_a_401_as_a_key_problem(monkeypatch):
    import requests
    from arbus import aicheck, llm

    monkeypatch.setattr(llm, "provider", lambda stage=None: "perplexity")
    monkeypatch.setattr(llm, "available_providers", lambda: ["perplexity"])
    monkeypatch.setattr(aicheck.resolvers, "facts_for", lambda *a, **k: "")

    def boom(*a, **k):
        raise requests.HTTPError("401 Client Error: Unauthorized for url: ...")

    monkeypatch.setattr(llm, "research", boom)
    out = aicheck._run("q", "Taip / Ne", "rules", "Taip", "src")
    assert "401" in out and "raktas" in out            # names the real cause


# ── search-tool rate limit: retry, don't record it as a real "unknown" ──────

def test_search_ratelimit_is_retried_then_succeeds(monkeypatch):
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SEARCH_BACKOFF_SECONDS", 0)
    calls = []

    def fake_research(prompt, **k):
        calls.append(1)
        if len(calls) == 1:
            return ("REZULTATAS: nežinomas\nĮSPĖJIMAI: paieškos įrankio limitas "
                    "išnaudotas (limit exceeded)\nSIŪLOMA BAIGTIS: dar neaišku")
        return ("REZULTATAS: žinomas\nŠALTINIS: https://lrt.lt/x\n"
                "SIŪLOMA BAIGTIS: Taip")

    monkeypatch.setattr(aicheck.llm, "research", fake_research)
    out = aicheck._research_with_search_retry("p", 5, "anthropic")
    assert len(calls) == 2 and "žinomas" in out      # retried past the rate limit


def test_too_many_web_search_calls_is_retried(monkeypatch):
    """Live-observed: the model reports "You have called the web_search tool too
    many times this turn" when it wants more than max_uses. That is a hit cap,
    not a considered unknown — it must retry with a fresh budget, not be trusted.
    The regex missing this phrasing let it fall through to "dar neaišku"."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SEARCH_BACKOFF_SECONDS", 0)
    calls = []

    def fake_research(prompt, **k):
        calls.append(1)
        if len(calls) == 1:
            return ("REZULTATAS: nežinomas\nĮSPĖJIMAI: Paieškos įrankis buvo "
                    "užblokuotas (You have called the web_search tool too many "
                    "times this turn)\nSIŪLOMA BAIGTIS: dar neaišku")
        return ("REZULTATAS: žinomas\nŠALTINIS: https://eurovision.tv/x\n"
                "SIŪLOMA BAIGTIS: Taip")

    monkeypatch.setattr(aicheck.llm, "research", fake_research)
    out = aicheck._research_with_search_retry("p", 3, "anthropic")
    assert len(calls) == 2 and "Taip" in out


def test_search_ratelimit_gives_up_gracefully_after_retries(monkeypatch):
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SEARCH_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(aicheck.llm, "research",
                        lambda p, **k: "ĮSPĖJIMAI: rate limit\nSIŪLOMA BAIGTIS: dar neaišku")
    out = aicheck._research_with_search_retry("p", 5, "anthropic")
    assert "dar neaišku" in out                       # never crashes


# ── truncated (max_tokens) responses: retry, never surface as a real answer ──

def test_truncated_response_missing_the_verdict_line_is_retried(monkeypatch):
    """Anthropic silently returns partial text when max_tokens is hit — no
    exception, just a log warning. A response cut off before its final
    SIŪLOMA BAIGTIS line must not be read as a considered 'unknown' or, worse,
    have some other line misparsed as the verdict."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SEARCH_BACKOFF_SECONDS", 0)
    calls = []

    def fake_research(prompt, **k):
        calls.append(1)
        if len(calls) == 1:
            # cut off mid-answer, before SIŪLOMA BAIGTIS ever appears
            return ("REZULTATAS: žinomas\nKAS ĮVYKO: 2026-07-30 Vilniuje vyks "
                    "atsakomosios rungtynės su Tbilisio")
        return ("REZULTATAS: žinomas\nŠALTINIS: https://uefa.com/x\n"
                "SIŪLOMA BAIGTIS: dar neaišku")

    monkeypatch.setattr(aicheck.llm, "research", fake_research)
    out = aicheck._research_with_search_retry("p", 5, "anthropic")
    assert len(calls) == 2 and "SIŪLOMA BAIGTIS: dar neaišku" in out


def test_truncated_response_never_returned_as_final_answer(monkeypatch):
    """If every retry is still truncated, fall back to the honest unknown
    rather than surfacing a cut-off fragment as if it were complete."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SEARCH_BACKOFF_SECONDS", 0)
    monkeypatch.setattr(aicheck.llm, "research",
                        lambda p, **k: "REZULTATAS: žinomas\nKAS ĮVYKO: dar rašau")
    out = aicheck._research_with_search_retry("p", 5, "anthropic")
    assert out == aicheck._FALLBACK_UNKNOWN
    assert "dar rašau" not in out                     # the fragment is discarded


def test_complete_response_with_a_real_unknown_is_not_treated_as_truncated():
    from arbus import aicheck

    text = ("REZULTATAS: nežinomas\nKAS ĮVYKO: dar neįvyko\nŠALTINIS: nerasta\n"
            "SIŪLOMA BAIGTIS: dar neaišku\nPASITIKĖJIMAS: žemas")
    assert aicheck._looks_incomplete(text) is False
