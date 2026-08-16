"""Advisory AI check for a frozen market — speeds the admin up, decides nothing.

Per the Notion spec: "AI netrenkia sprendimų — tik pagreitina admin darbą".
This module reads the source a user cited and reports whether it actually says
what they claim. It writes no state and moves no Arbucks; the output is text
for the admin to read before deciding in the dashboard.

It ALWAYS searches the web, whether or not a source was cited. An admin
freezing a market in the dashboard cannot attach one, and a user who reports
the right outcome may still cite a weak link — so "no usable source" must never
turn into "cannot verify" when the fact is public.

Cost scales with the search budget (each web search injects a page of tokens).
On Opus 5, ~5 searches is ~EUR 0.15 a check, and a stock/weather market whose
number a resolver already fetched drops to ~3 searches (~EUR 0.08). Cheaper
still: ANTHROPIC_AICHECK_MODEL=claude-sonnet-5 halves it, and a search-native
Perplexity key (LLM_PROVIDER_AICHECK=perplexity) is cheaper again because the
search is bundled, not billed per Anthropic token. A wrong payout costs more
than any of these and cannot be undone.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from html import unescape as _html_unescape

import requests

from . import config, llm, notify, resolvers

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; ArbusMarketAgent/1.0; +https://arbus.lt)"

SYSTEM = (
    "You investigate outcomes for a Lithuanian prediction-market admin. Search "
    "the web in Lithuanian, find what actually happened, and report it with the "
    "official source and the date. You never decide the market — a human does — "
    "but 'cannot verify' is only acceptable after you searched and the answer "
    "genuinely is not public. Two things are worse than not knowing: claiming a "
    "result you cannot paste a real URL for, and resolving on a same-surname "
    "namesake or an event that has not happened yet. Never do either."
)

# The model is told (prompt rule 1) that a known result needs a real, working
# URL, but it must not be trusted to obey: it has claimed confirmed results with
# source "nerasta" (Dirkstys) AND cited plausible-looking URLs that 404 or point
# at the wrong year (Eurovision, Sabonis). So every claim is checked against the
# ONE thing that cannot be faked — whether the cited link actually loads.
_HTTP_RE = re.compile(r"https?://[^\s\])>\"']+", re.I)
_KNOWN_RE = re.compile(r"REZULTATAS(?:\s+ŽINOMAS)?:\s*(žinomas|taip)", re.I)
_UNKNOWN_RE = re.compile(r"REZULTATAS(?:\s+ŽINOMAS)?:\s*(nežinomas|ne)\b", re.I)
_OUTCOME_RE = re.compile(r"SIŪLOMA\s+BAIGTIS:\s*(.+)", re.I)

_BAR = "─" * 22


# Only "page does not exist" reliably means a fabricated link. Real sources
# routinely answer 403/401/429/503 to a bot (nasdaqbaltic.com does — it blocked
# a correct Ignitis answer and got it flagged as a hallucination). Those are
# "the site refused us", not "the URL is fake", so they must NOT condemn a link.
_URL_GONE = {404, 410}


def verify_url(url: str, timeout: int = 12) -> str:
    """'ok' | 'broken' | 'unknown'.

    'broken' means the server said the page does not exist (404/410) — a
    fabricated link. 'unknown' means the request failed or the site refused us
    (403/429/timeout, or the sandbox blocking every call): those never prove a
    link is fake, so a real source is never wrongly flagged.
    """
    try:
        resp = requests.get(url, headers={"User-Agent": UA},
                            timeout=timeout, allow_redirects=True)
    except Exception:
        return "unknown"
    if resp.status_code in _URL_GONE:
        return "broken"
    return "ok" if resp.status_code < 400 else "unknown"


# Crude HTML → text: drop scripts/styles, strip tags, collapse whitespace. Not a
# parser — just enough that the article body reaches the model. The model reads
# past the nav/menu noise; the point is to hand it the source text so it does not
# pay to web-search for a page we already have.
_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def fetch_source_text(url: str, max_chars: int | None = None, timeout: int = 12) -> str:
    """Fetch a cited URL and return its readable text (''on any failure).

    A market's own source is the cheapest possible evidence: one free GET versus
    several billed web searches. Failure is never fatal — we just fall back to
    letting the model search."""
    if max_chars is None:
        max_chars = config.AICHECK_SOURCE_MAX_CHARS
    try:
        resp = requests.get(url, headers={"User-Agent": UA},
                            timeout=timeout, allow_redirects=True)
    except Exception:
        return ""
    if resp.status_code >= 400:
        return ""
    stripped = _TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", resp.text))
    text = _WS_RE.sub(" ", _html_unescape(stripped)).strip()
    return text[:max_chars]


def source_facts(*fields: str) -> str:
    """Fetch every URL cited in the given market fields (source, rules, …) and
    return their text as facts. Empty when fetching is off, no URL is present, or
    nothing substantial came back."""
    if not config.AICHECK_SOURCE_FETCH:
        return ""
    urls = _HTTP_RE.findall("\n".join(f for f in fields if f))
    out = []
    for url in dict.fromkeys(urls):                  # de-dupe, keep order
        text = fetch_source_text(url)
        if len(text) >= 200:                         # substantial content only
            out.append(f"Pateiktas šaltinis ({url}):\n{text}")
    return "\n\n".join(out)


def _suggested_outcome(text: str) -> str:
    m = _OUTCOME_RE.search(text)
    return m.group(1).strip() if m else ""


def _explain(exc: Exception | None) -> str:
    """A human-readable cause, so a config problem does not read as a bug."""
    msg = str(exc or "")
    if "401" in msg or "Unauthorized" in msg:
        return ("API raktas neteisingas arba nenustatytas (401) — patikrink "
                ".env (pvz. PERPLEXITY_API_KEY, jei nustatytas "
                "LLM_PROVIDER_AICHECK=perplexity)")
    if "429" in msg:
        return "pasiektas API limitas (429) — pabandyk vėliau"
    if "timeout" in msg.lower() or "connection" in msg.lower():
        return "tinklo klaida — nepavyko pasiekti API"
    return f"techninė klaida ({msg[:120]})" if msg else "techninė klaida"


def _finalize(text: str, verify: bool = None) -> str:
    """Prepend a can't-miss verdict header and vet every cited link.

    The header is the first thing the admin reads: a green check only when the
    AI claims a result AND a cited link actually loaded; a red cross when it
    does not know; a warning when it claims a result it cannot back with a
    working source. The model's own text is always kept underneath, unchanged.
    """
    if verify is None:
        verify = config.AICHECK_VERIFY_URLS
    urls = _HTTP_RE.findall(text)

    statuses = [verify_url(u) for u in urls] if (verify and urls) else []
    has_working = "ok" in statuses
    has_broken = "broken" in statuses

    outcome = _suggested_outcome(text)
    # The actionable question is "is there a concrete outcome to act on", so the
    # header keys off SIŪLOMA BAIGTIS, not REZULTATAS. This resolves the case
    # where the model wrote "REZULTATAS: žinomas" but "SIŪLOMA BAIGTIS: dar
    # neaišku" (Ignitis, a future threshold): no actionable outcome → ❌.
    has_outcome = bool(outcome) and "neaišk" not in outcome.lower()

    if not has_outcome:
        header = ("❌ AI NEŽINO / DAR NEAIŠKU — NESPRĘSK automatiškai. "
                  "Palauk įvykio arba oficialaus šaltinio.")
    elif has_broken:
        header = ("⚠️ GALIMA HALIUCINACIJA — AI siūlo baigtį, bet jo nurodyta "
                  "nuoroda NEEGZISTUOJA (404). Nepasitikėk — patikrink pats, ar "
                  "tai teisingi metai ir tas pats įvykis/asmuo.")
    elif not urls:
        header = (f"⚠️ AI siūlo: {outcome} — BET be jokios nuorodos. Be šaltinio "
                  "tai NĖRA patvirtinta, patikrink rankiniu būdu.")
    elif has_working:
        header = (f"✅ AI SIŪLO: {outcome} — ir nuoroda veikia. Vis tiek "
                  "įsitikink, kad metai ir įvykis sutampa.")
    else:  # links present but unverified (verify off) or site refused us
        header = (f"ℹ️ AI siūlo: {outcome}. Nuoroda pateikta, bet nepatvirtinta "
                  "— atidaryk ją pats ir patikrink metus.")

    return f"{header}\n{_BAR}\n{text}"


_SEARCH_ON = (
    "SEARCH THE WEB. Always — including when a source is cited, and especially "
    "when none is. An admin freezing a market cannot attach a source, and a user "
    "may cite a weak or wrong link, so judge the FACT, not the URL. Search in "
    "Lithuanian using the market's own words and find the official confirmation "
    "(LKL/UEFA/organiser, eurovision.tv, nba.com, lrs.lt, VRK, Nasdaq Baltic, "
    "Statistikos departamentas, LHMT)."
)
_SEARCH_OFF = (
    "YOU HAVE NO SEARCH TOOL THIS RUN — an authoritative feed already gave you "
    "the number above. Do NOT claim you searched, and do NOT describe archives or "
    "pages you did not open. Reason ONLY from the AUTHORITATIVE DATA and the "
    "market text. If they do not fully settle the market — e.g. a market asking "
    "whether a threshold was crossed at least once, when you were given only the "
    "latest value — say exactly that in ĮSPĖJIMAI and set SIŪLOMA BAIGTIS: dar "
    "neaišku. The one link you may cite is the source shown in the data above."
)


def _search_directive(searches: int) -> str:
    """Match the prompt to reality: telling the model to 'search the web' when it
    has no search tool made it fabricate a search it never ran (live: a fuel
    market 'ieškojau LEA archyvų' with 0 searches). When searching is off, tell
    it so and to reason only from the injected facts."""
    return _SEARCH_ON if searches > 0 else _SEARCH_OFF


_NO_DATA_ADVICE = (
    "⏭️ PRALEISTA — nėra automatinių duomenų. Šiai rinkai nėra nei feed'o "
    "(akcijos/oras/degalai), nei pridėto šaltinio, todėl AI paieška kainuotų "
    "~0,15–0,30 € ir dažniausiai vis tiek grąžintų „dar neaišku“. Patikrink "
    "rankiniu būdu. Jei rinka svarbi ir nori AI paieškos: "
    "arbus check --match \"…\" --deep"
)


def _run(question: str, options: str, criteria: str, proposed: str,
         source: str, today: date | None = None, on_error: str = "",
         searches: int | None = None, closes_at: str = "",
         deep: bool = False) -> str:
    """One advisory check. Never raises — a failed check must not block the
    admin, and it must never read as evidence either way.

    `searches` is larger when nobody cited a source: then the model is not
    verifying a link, it is finding out what happened, and the answer decides a
    payout. That is worth a few cents more.

    `deep` enables the paid web hunt for a market with NO feed and NO source.
    Default off: that hunt costs ~0.15-0.30 EUR and usually still lands on "dar
    neaišku" for a future event — at 200 markets and 15 freezes a day that is
    the difference between profitable and not. A no-data market is skipped with a
    manual-check note unless it is flagged important (`--deep`).
    """
    resolver_facts = resolvers.facts_for(question, closes_at)
    # A cited source is the cheapest evidence there is: fetch it ourselves (free
    # GET) and hand the model the text, so it does not pay to web-search for a
    # page we already have. This is the biggest cost lever when markets carry a
    # source — each avoided search is a full page of billed input tokens.
    fetched = source_facts(source, criteria)
    facts = "\n\n".join(f for f in (resolver_facts, fetched) if f)

    # No feed, no source, not flagged important: do NOT pay to search. Tell the
    # admin to check it by hand (or re-run it with --deep). This is what keeps a
    # full sweep cheap — only fact/source markets cost anything by default.
    if not facts and not deep and searches is None:
        return _NO_DATA_ADVICE

    # Search budget, cheapest tier first: with the source text in hand a single
    # confirming search is enough; a resolver number needs a couple; only a
    # market with neither feed nor source (and --deep) pays for the full hunt.
    if searches is None:
        if fetched:
            searches = config.AICHECK_MAX_SEARCHES_WITH_SOURCE
        elif resolver_facts:
            searches = config.AICHECK_MAX_SEARCHES
        else:
            searches = config.AICHECK_MAX_SEARCHES_OPEN
    prompt = llm.load_prompt(
        "aicheck", today=(today or date.today()).isoformat(),
        question=question, options=options, criteria=criteria,
        proposed=proposed, source=source,
        facts=facts or "(nėra automatinių duomenų — ieškok pats)",
        search_directive=_search_directive(searches),
    )

    # Try the configured provider first, then any OTHER provider that has a key.
    # A wrong or expired second key (the classic LLM_PROVIDER_AICHECK=perplexity
    # with no valid PERPLEXITY_API_KEY → 401) must never take the whole check
    # down when a working Anthropic key is right there.
    llm.reset_usage()                                # per-market cost accounting
    primary = llm.provider("aicheck")
    chain = [primary] + [p for p in llm.available_providers() if p != primary]
    last_exc = None
    for i, prov in enumerate(chain):
        try:
            text = _research_with_search_retry(prompt, searches, prov)
            if i:                                    # a fallback was used
                text += (f"\n(pastaba: {primary} nepavyko, "
                         f"patikra atlikta su {prov})")
            return _append_cost(_finalize(text))
        except Exception as exc:
            last_exc = exc
            log.warning("AI check via %s failed: %s", prov, _explain(exc))

    return on_error or (f"AI patikra nepavyko: {_explain(last_exc)}. "
                        "Patikrink naujienas rankiniu būdu.")


def _append_cost(summary: str) -> str:
    """Add the per-market cost line, when usage was measured."""
    line = llm.usage_line()
    return f"{summary}\n{_BAR}\n{line}" if line else summary


# The web-search TOOL can be exhausted even when the request returns 200: the
# model then reports it could not search and gives up, which is a transient
# infra limit, NOT a genuine "unknown". Two live-observed variants: a rate limit
# ("paieškos įrankio limitas išnaudotas / limit exceeded") and the per-turn cap
# ("You have called the web_search tool too many times this turn" — the model
# wanted more than max_uses). Both mean "no real answer this attempt", so both
# must trigger a retry with a fresh search budget rather than be trusted.
_SEARCH_FAIL_RE = re.compile(
    r"limit\s*exceeded|paieškos įrankio limit|nepavyko atlikti paieškos|"
    r"rate.?limit|search.{0,20}(unavailable|failed|exceeded)|"
    r"too\s+many\s+(times|web[_ ]?search|search)|web[_ ]?search.{0,20}too\s+many|"
    r"apribot|užblokuot|uzblokuot", re.I)

# A subset of the above: the model hit its per-turn search CAP (it wanted more
# searches than max_uses allowed and said so). This is not a rate limit and not
# a real failure — the fix is more budget, not a backoff. Retrying with the same
# small budget after 20s just burned another ~2.5 min for nothing (live-seen on
# Eurovision). So this case retries immediately with a bumped search budget.
_SEARCH_CAP_RE = re.compile(
    r"too\s+many\s+(times|web[_ ]?search|search)|web[_ ]?search.{0,20}too\s+many|"
    r"per\s+daug\s+(kart|paie[šs]k)", re.I)


def _looks_incomplete(text: str) -> bool:
    """True if the response was cut off before its final field.

    Anthropic silently returns whatever text it managed to generate when
    max_tokens is hit mid-answer (`llm._research_anthropic` only logs a
    warning) — live-observed on a search-heavy market. A response missing its
    terminal SIŪLOMA BAIGTIS line is not a considered "I don't know", it is a
    truncated one, and must not be read as either.
    """
    return not _OUTCOME_RE.search(text)


_FALLBACK_UNKNOWN = "REZULTATAS: nežinomas\nSIŪLOMA BAIGTIS: dar neaišku"
_FALLBACK_TIMEOUT = (
    "REZULTATAS: nežinomas\n"
    "ĮSPĖJIMAI: patikra nutraukta pasiekus laiko limitą — įvykis greičiausiai "
    "dar neišspręstas (paieška nerado galutinio rezultato per skirtą laiką)\n"
    "SIŪLOMA BAIGTIS: dar neaišku")


def _run_bounded(fn, timeout: int):
    """Run fn() but stop waiting after `timeout` seconds.

    A resolution check on a live, still-unresolved event searches all the way to
    its budget and STILL lands on "dar neaišku" — live-measured at ~10 min on a
    Conference League qualifier that had not been played yet. That verdict is
    correct (never invent an outcome), but 10 min for it stalls a 9-market batch.
    Anthropic streams keep-alive pings, so an httpx read timeout will not bound
    the wall clock; and signal.alarm does not exist on Windows. So the call runs
    on a daemon thread we simply stop waiting on: the abandoned request winds
    down on its own and cannot delay process exit.
    """
    box: dict = {}

    def worker():
        try:
            box["value"] = fn()
        except Exception as exc:            # re-raised on the caller's thread
            box["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError(f"aicheck exceeded {timeout}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _research_with_search_retry(prompt: str, searches: int, prov: str) -> str:
    text = ""
    # `searches` may legitimately be 0 (a fact market that must not search), so a
    # falsy `or` fallback would wrongly turn 0 into the default — be explicit.
    budget = config.AICHECK_MAX_SEARCHES if searches is None else searches
    for attempt in range(config.AICHECK_SEARCH_RETRIES + 1):
        try:
            text = _run_bounded(
                lambda: llm.research(prompt, system=SYSTEM, max_uses=budget,
                                     max_tokens=config.AICHECK_MAX_TOKENS,
                                     stage="aicheck", force_provider=prov),
                config.AICHECK_TIMEOUT_SECONDS).strip()
        except TimeoutError:
            # Searched to the limit without a confirmable result: that IS "dar
            # neaišku". Return it now rather than hang the batch or fall through
            # to a weaker provider that will not find LT sources either. This is a
            # normal outcome for a --deep hunt on an unresolved event, not an
            # error — so it is logged calmly and the note explains itself.
            log.info("aicheck reached its %ds time budget (%s); returning "
                     "'dar neaišku'", config.AICHECK_TIMEOUT_SECONDS, prov)
            return _FALLBACK_TIMEOUT
        cap_hit = bool(text) and bool(_SEARCH_CAP_RE.search(text))
        bad = bool(text) and (_SEARCH_FAIL_RE.search(text) or _looks_incomplete(text))
        if text and not bad:
            return text
        if attempt < config.AICHECK_SEARCH_RETRIES:
            if cap_hit:
                # The model wanted more searches, not a rate limit: a backoff
                # buys nothing. Give it more budget and retry immediately.
                budget += config.AICHECK_SEARCH_CAP_BUMP
                log.warning("aicheck hit its search cap (%s); retrying now with "
                            "%d searches", prov, budget)
                continue
            wait = config.AICHECK_SEARCH_BACKOFF_SECONDS * (attempt + 1)
            reason = ("rate-limited" if (text and _SEARCH_FAIL_RE.search(text))
                     else "truncated/empty")
            log.warning("aicheck response %s (%s); retrying in %ds", reason, prov, wait)
            time.sleep(wait)
    # Every attempt was empty, rate-limited or truncated: never surface that
    # partial/garbage text as if it were a considered answer.
    return _FALLBACK_UNKNOWN


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
                today=today, closes_at=market["resolve_by"],
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
                source="(nėra — surask pats, kas įvyko)",
                today=today, closes_at=market["resolve_by"])


def check_app_market(market: dict, today: date | None = None,
                     deep: bool = False) -> str:
    """Same check for a market frozen in the APP, where this repo has no row.

    Markets paused by an admin in the dashboard never existed in the local
    database, which is why `arbus check` used to report "nothing frozen" while
    the app had stopped markets sitting there.

    `deep` (from `--deep`) allows the paid web hunt on a market with no feed and
    no source; without it such a market is skipped with a manual-check note so a
    full sweep stays cheap.
    """
    from . import notify

    view = notify.market_view(market)
    return _run(question=view["question"],
                options=" / ".join(view["options"]),
                criteria=view["rules"],
                proposed="(nenurodyta — rinka sustabdyta appe)",
                source="(nėra — adminas negali prisegti šaltinio, todėl ieškok pats)",
                today=today, closes_at=view["resolve_by"], deep=deep)


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

def pending_app_markets(conn: sqlite3.Connection, limit: int = 20
                        ) -> tuple[list[dict], str]:
    """App markets that need a decision: frozen ones AND overdue ones.

    `arbus check` is a deliberate, manual command, so it always returns
    everything that is currently frozen or trading past its date — it does NOT
    skip markets it checked before. Skipping caused exactly the confusing case
    where a paused/closed market showed in `arbus app` but `check` said
    "nothing frozen", because a prior run had recorded it. A fresh check is
    cheap next to a wrong resolution, and re-reading the live outcome is often
    the point.

    Overdue matters as much as frozen. "Ar M. Sinkevičius taps premjeru?" was
    decided weeks ago, yet the market was still `open` with its date long past —
    nobody paused it, so nothing looked at it while the AMM kept taking the
    other side of a known outcome.
    """
    from . import app as app_api

    rows, error = app_api.markets(200)
    if error:
        return [], error

    frozen = [r for r in rows if app_api.is_frozen(r)]
    overdue = app_api.overdue_markets(rows) if config.APP_CHECK_OVERDUE else []
    seen_ids: set[str] = set()
    out = []
    for row in frozen + overdue:
        mid = app_api.market_id_of(row)
        if mid not in seen_ids:            # de-dupe within THIS run only
            seen_ids.add(mid)
            out.append(row)
    return out[:limit], ""


def review_app_market(conn: sqlite3.Connection, market: dict,
                      today: date | None = None, alert: bool = True,
                      deep: bool = False) -> str:
    """Check one app-frozen market, remember it, and alert the group."""
    from . import app as app_api

    summary = check_app_market(market, today, deep=deep)
    conn.execute(
        "INSERT OR REPLACE INTO app_checks (app_market_id, question, status,"
        " ai_summary, checked_at) VALUES (?,?,?,?,?)",
        (app_api.market_id_of(market), app_api.question_of(market),
         app_api.status_of(market), summary,
         datetime.now(timezone.utc).isoformat()))
    if alert:
        notify.notify_resolution(market, None, summary)
    return summary


# ── resolution PROPOSALS: check only what a user proposed, seeing the claim ──

def pending_app_proposals(limit: int = 50) -> tuple[list[dict], str]:
    """User-submitted resolution proposals, each paired with its market.

    This is the cheap, targeted trigger the team wants: check ONLY markets
    someone proposed a resolution for — not every closed market — and let the AI
    see the claimed outcome and the cited source."""
    from . import app as app_api

    proposals, error = app_api.resolution_proposals(limit)
    if error:
        return [], error
    markets, m_err = app_api.markets(200)
    by_id = {} if m_err else {app_api.market_id_of(m): m for m in markets}
    out = [{"proposal": p, "market": by_id.get(str(p.get("market_id")), {})}
           for p in proposals]
    return out, ""


def check_app_proposal(proposal: dict, market: dict, today: date | None = None,
                       deep: bool = True) -> str:
    """Verify one user's proposed resolution: does the cited source (and a search)
    support the outcome they claim? The claim and source are handed to the model,
    and a cited URL is fetched for free, so this is both targeted and cheap."""
    from . import app as app_api, notify

    view = notify.market_view(market) if market else {}
    proposed = app_api.option_label(market, proposal.get("proposed_option_id"))
    source = proposal.get("source") or "(vartotojas nenurodė šaltinio)"
    summary = _run(
        question=view.get("question") or "(rinka nerasta app'e)",
        options=" / ".join(view.get("options") or []),
        criteria=view.get("rules") or "",
        proposed=f"vartotojas siūlo baigtį: {proposed}",
        source=source, today=today,
        closes_at=view.get("resolve_by", ""), deep=deep)
    header = (f"👤 Vartotojo pasiūlyta baigtis: {proposed}\n"
              f"🔗 Pateiktas šaltinis: {source}\n{_BAR}\n")
    return header + summary


def review_app_proposal(item: dict, today: date | None = None,
                        alert: bool = True, deep: bool = True) -> str:
    """Check one proposal and post the summary to Telegram."""
    summary = check_app_proposal(item["proposal"], item["market"], today, deep)
    if alert:
        notify.notify_resolution(item["market"] or {}, None, summary)
    return summary
