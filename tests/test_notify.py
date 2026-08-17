"""The freeze alert: what lands in the team's Telegram group.

Formatting is tested offline (no network) because the alert is the only thing
the team sees before deciding — if a field is missing there, the decision is
made without it.
"""

import time

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


def test_proposal_alert_leads_with_the_claim_and_says_closed():
    """A user proposal closes the market: the alert says SUSTABDYTA, shows the
    claimed outcome + source up top, then the advisory AI check."""
    market = {
        "id": "m-42",
        "question": "Ar Ignitis akcija viršys 20 €?",
        "market_options": [{"label": "TAIP"}, {"label": "NE"}],
        "resolve_by": "2026-09-01",
        "resolution_criteria": "Pagal Nasdaq Baltic uždarymo kainą.",
    }
    text = notify.proposal_message(market, "TAIP", "https://nasdaqbaltic.com/x", AI)

    assert "SUSTABDYTA" in text and "UŽŠALDYTA" not in text   # closed, not frozen
    assert "Pasiūlyta baigtis: TAIP" in text                  # the claim leads
    assert "https://nasdaqbaltic.com/x" in text               # the cited source
    assert "Ignitis akcija" in text                           # the market
    assert "Nasdaq Baltic" in text                            # deciding rules
    assert "nedarbas rugpjūtį" in text                        # the AI body is included
    assert "AI nieko nesprendžia" in text                     # advisory, always


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

    out = aicheck._run("q", "Taip / Ne", "rules", "Taip", "src", deep=True)
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
    out = aicheck._run("q", "Taip / Ne", "rules", "Taip", "src", deep=True)
    assert "401" in out and "raktas" in out            # names the real cause


# ── source fetch: read the cited page instead of paying to search for it ────

class _FakeGet:
    def __init__(self, status, text):
        self.status_code = status
        self.text = text


def test_source_text_strips_html_to_readable_text(monkeypatch):
    from arbus import aicheck

    html = ("<html><head><style>.x{}</style></head><body><nav>Meniu</nav>"
            "<article>Žalgiris laimėjo 89:80 antradienį.</article>"
            "<script>track()</script></body></html>")
    monkeypatch.setattr(aicheck.requests, "get", lambda *a, **k: _FakeGet(200, html))
    text = aicheck.fetch_source_text("https://lrt.lt/x")
    assert "Žalgiris laimėjo 89:80" in text
    assert "track()" not in text and "<article>" not in text


def test_source_text_empty_on_http_error(monkeypatch):
    from arbus import aicheck

    monkeypatch.setattr(aicheck.requests, "get", lambda *a, **k: _FakeGet(404, "gone"))
    assert aicheck.fetch_source_text("https://lrt.lt/missing") == ""


def test_source_facts_fetches_each_cited_url(monkeypatch):
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SOURCE_FETCH", True)
    body = "x" * 300                                   # substantial content
    monkeypatch.setattr(aicheck.requests, "get", lambda *a, **k: _FakeGet(200, body))
    facts = aicheck.source_facts("Šaltinis: https://lrt.lt/a", "taisyklės https://delfi.lt/b")
    assert "https://lrt.lt/a" in facts and "https://delfi.lt/b" in facts


def test_source_facts_skips_thin_pages(monkeypatch):
    """A page with almost no text (a redirect stub, a paywall) is not worth
    injecting — better to let the model search."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SOURCE_FETCH", True)
    monkeypatch.setattr(aicheck.requests, "get", lambda *a, **k: _FakeGet(200, "<p>hi</p>"))
    assert aicheck.source_facts("https://lrt.lt/thin") == ""


def test_source_facts_off_switch(monkeypatch):
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SOURCE_FETCH", False)
    called = []
    monkeypatch.setattr(aicheck.requests, "get",
                        lambda *a, **k: called.append(1) or _FakeGet(200, "x" * 300))
    assert aicheck.source_facts("https://lrt.lt/a") == ""
    assert not called                                  # no fetch when disabled


def test_a_fetched_source_drops_the_search_budget(monkeypatch):
    """The cost win: with the source text in hand, the run asks for only the
    with-source (cheapest) search budget, not the full open-market hunt."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_MAX_SEARCHES_WITH_SOURCE", 1)
    monkeypatch.setattr(config, "AICHECK_MAX_SEARCHES_OPEN", 4)
    monkeypatch.setattr(aicheck.resolvers, "facts_for", lambda *a, **k: "")
    monkeypatch.setattr(aicheck, "source_facts", lambda *a, **k: "Pateiktas šaltinis (...): ...")
    monkeypatch.setattr(aicheck.llm, "reset_usage", lambda: None)
    monkeypatch.setattr(aicheck.llm, "usage_line", lambda: "")
    monkeypatch.setattr(aicheck.llm, "provider", lambda s: "anthropic")
    monkeypatch.setattr(aicheck.llm, "available_providers", lambda: ["anthropic"])
    seen = {}

    def fake_retry(prompt, searches, prov):
        seen["searches"] = searches
        return "REZULTATAS: žinomas\nŠALTINIS: https://lrt.lt/x\nSIŪLOMA BAIGTIS: Taip"

    monkeypatch.setattr(aicheck, "_research_with_search_retry", fake_retry)
    aicheck._run("Ar X?", "Taip/Ne", "kriterijai", "(nenurodyta)", "https://lrt.lt/x")
    assert seen["searches"] == 1                        # with-source budget, not 4


def test_a_fact_market_does_not_search(monkeypatch):
    """The core complaint: a market whose number a resolver already fetched
    (Ignitis price, temperature) was still web-searching, burning credits and
    the shared rate limit. With a fact in hand the budget must be 0 — read it,
    don't search."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_MAX_SEARCHES", 0)
    monkeypatch.setattr(config, "AICHECK_MAX_SEARCHES_OPEN", 4)
    monkeypatch.setattr(aicheck.resolvers, "facts_for",
                        lambda *a, **k: "akcijos: Ignitis 22,70 €")
    monkeypatch.setattr(aicheck, "source_facts", lambda *a, **k: "")
    monkeypatch.setattr(aicheck.llm, "reset_usage", lambda: None)
    monkeypatch.setattr(aicheck.llm, "usage_line", lambda: "")
    monkeypatch.setattr(aicheck.llm, "provider", lambda s: "anthropic")
    monkeypatch.setattr(aicheck.llm, "available_providers", lambda: ["anthropic"])
    seen = {}

    def fake_retry(prompt, searches, prov):
        seen["searches"] = searches
        return "REZULTATAS: nežinomas\nSIŪLOMA BAIGTIS: dar neaišku"

    monkeypatch.setattr(aicheck, "_research_with_search_retry", fake_retry)
    aicheck._run("Ar Ignitis > 23 €?", "Taip/Ne", "kriterijai", "(nenurodyta)",
                 "(nėra)")
    assert seen["searches"] == 0                        # fact tier: no search


def test_no_data_market_is_skipped_without_paying_for_a_search(monkeypatch):
    """The profitability fix: a market with no feed and no source must NOT trigger
    the ~0.15-0.30 EUR web hunt by default — it is skipped with a manual-check
    note and the LLM is never called."""
    from arbus import aicheck

    monkeypatch.setattr(aicheck.resolvers, "facts_for", lambda *a, **k: "")
    monkeypatch.setattr(aicheck, "source_facts", lambda *a, **k: "")
    called = []
    monkeypatch.setattr(aicheck, "_research_with_search_retry",
                        lambda *a, **k: called.append(1) or "x")
    out = aicheck._run("Ar X įvyks?", "Taip/Ne", "rules", "(nenurodyta)", "(nėra)")
    assert not called                                   # LLM never invoked
    assert "PRALEISTA" in out and "--deep" in out


def test_deep_flag_lets_an_important_no_data_market_search(monkeypatch):
    """With --deep the same no-data market DOES run the web hunt."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_MAX_SEARCHES_OPEN", 4)
    monkeypatch.setattr(aicheck.resolvers, "facts_for", lambda *a, **k: "")
    monkeypatch.setattr(aicheck, "source_facts", lambda *a, **k: "")
    monkeypatch.setattr(aicheck.llm, "reset_usage", lambda: None)
    monkeypatch.setattr(aicheck.llm, "usage_line", lambda: "")
    monkeypatch.setattr(aicheck.llm, "provider", lambda s: "anthropic")
    monkeypatch.setattr(aicheck.llm, "available_providers", lambda: ["anthropic"])
    seen = {}

    def fake_retry(prompt, searches, prov):
        seen["searches"] = searches
        return "REZULTATAS: nežinomas\nSIŪLOMA BAIGTIS: dar neaišku"

    monkeypatch.setattr(aicheck, "_research_with_search_retry", fake_retry)
    out = aicheck._run("Ar X įvyks?", "Taip/Ne", "rules", "(nenurodyta)", "(nėra)",
                       deep=True)
    assert seen["searches"] == 4 and "PRALEISTA" not in out


def test_prompt_tells_the_model_not_to_fake_a_search_when_it_cannot(monkeypatch):
    """With 0 searches the model had no tool yet wrote 'ieškojau LEA archyvų'
    (live). The directive must switch to 'you have no search tool' so it reasons
    from the facts instead of fabricating a search."""
    from arbus import aicheck

    assert "SEARCH THE WEB" in aicheck._search_directive(4)
    off = aicheck._search_directive(0)
    assert "NO SEARCH TOOL" in off and "Do NOT claim you searched" in off


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


def test_search_cap_hit_bumps_the_budget_and_skips_the_backoff(monkeypatch):
    """A per-turn cap hit means the model wanted MORE searches, not that we are
    rate-limited. So the retry must (a) not waste a backoff and (b) hand the
    model a bigger budget — retrying with the same small budget just burned
    another full search sequence for nothing (live-seen ~5 min on Eurovision)."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SEARCH_CAP_BUMP", 2)
    slept = []
    monkeypatch.setattr(aicheck.time, "sleep", lambda s: slept.append(s))
    budgets = []

    def fake_research(prompt, **k):
        budgets.append(k["max_uses"])
        if len(budgets) == 1:
            return ("REZULTATAS: nežinomas\nĮSPĖJIMAI: You have called the "
                    "web_search tool too many times this turn\n"
                    "SIŪLOMA BAIGTIS: dar neaišku")
        return ("REZULTATAS: žinomas\nŠALTINIS: https://lrt.lt/x\n"
                "SIŪLOMA BAIGTIS: Taip")

    monkeypatch.setattr(aicheck.llm, "research", fake_research)
    out = aicheck._research_with_search_retry("p", 4, "anthropic")
    assert budgets == [4, 6]        # second turn got the +2 bump
    assert slept == []              # no backoff on a cap hit
    assert "Taip" in out


def test_real_ratelimit_still_backs_off_without_bumping(monkeypatch):
    """The opposite case: a genuine rate limit is NOT a cap hit, so it keeps the
    backoff and does not inflate the budget."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_SEARCH_CAP_BUMP", 2)
    slept = []
    monkeypatch.setattr(aicheck.time, "sleep", lambda s: slept.append(s))
    budgets = []

    def fake_research(prompt, **k):
        budgets.append(k["max_uses"])
        if len(budgets) == 1:
            return ("REZULTATAS: nežinomas\nĮSPĖJIMAI: rate limit exceeded\n"
                    "SIŪLOMA BAIGTIS: dar neaišku")
        return ("REZULTATAS: žinomas\nŠALTINIS: https://lrt.lt/x\n"
                "SIŪLOMA BAIGTIS: Taip")

    monkeypatch.setattr(aicheck.llm, "research", fake_research)
    out = aicheck._research_with_search_retry("p", 4, "anthropic")
    assert budgets == [4, 4]        # budget unchanged
    assert slept and slept[0] > 0   # a real rate limit still waits
    assert "Taip" in out


def test_a_check_that_runs_too_long_is_cut_off_to_the_honest_unknown(monkeypatch):
    """A live/unresolved event searches to its budget and still can't confirm an
    outcome; live it took ~10 min to land on 'dar neaišku'. The wall-clock bound
    must return that same unknown quickly instead of hanging the whole batch."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_TIMEOUT_SECONDS", 1)

    def slow_research(prompt, **k):
        time.sleep(5)                                 # longer than the 1s bound
        return "REZULTATAS: žinomas\nSIŪLOMA BAIGTIS: Taip"

    monkeypatch.setattr(aicheck.llm, "research", slow_research)
    started = time.monotonic()
    out = aicheck._research_with_search_retry("p", 4, "anthropic")
    assert out == aicheck._FALLBACK_TIMEOUT              # honest unknown + reason
    assert "dar neaišku" in out and "laiko limit" in out
    assert time.monotonic() - started < 4             # did not wait for the call


def test_a_fast_check_is_not_affected_by_the_timeout(monkeypatch):
    """The bound must not touch a normal, quick check — it returns its real
    answer, not the fallback unknown."""
    from arbus import aicheck, config

    monkeypatch.setattr(config, "AICHECK_TIMEOUT_SECONDS", 30)
    monkeypatch.setattr(aicheck.llm, "research",
                        lambda p, **k: "REZULTATAS: žinomas\n"
                                       "ŠALTINIS: https://lrt.lt/x\nSIŪLOMA BAIGTIS: Taip")
    out = aicheck._research_with_search_retry("p", 4, "anthropic")
    assert "Taip" in out and out != aicheck._FALLBACK_UNKNOWN


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
