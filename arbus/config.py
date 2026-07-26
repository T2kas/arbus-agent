"""Central configuration for the Arbus market generator."""

from __future__ import annotations

import os
import re
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader (repo root); real env vars always win."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            if value.strip():
                os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

# Sonnet, not Opus: a full Opus batch with adaptive thinking and 16 searches per
# chunk burned ~$5. Sonnet is several times cheaper and, with the same web
# search and the same deterministic gates around it, loses very little here.
# Override per run with ANTHROPIC_MODEL.
MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
# Verification is the one stage where judgement pays for itself. Leave empty to
# reuse MODEL, or set ANTHROPIC_VERIFY_MODEL=claude-opus-5 to spend only there.
VERIFY_MODEL = os.environ.get("ANTHROPIC_VERIFY_MODEL", "")
# The resolution check is the other place where being right is worth paying for,
# and it runs a handful of times a day rather than 12 times a batch: the whole
# difference between Sonnet and Opus here is a few cents against a payout that
# cannot be clawed back. Set ANTHROPIC_AICHECK_MODEL="" to fall back to MODEL.
AICHECK_MODEL = os.environ.get("ANTHROPIC_AICHECK_MODEL", "claude-opus-5")

# Extended thinking is charged as output. "off" removes it entirely; the
# scaffolding around the model already does the reasoning we depend on.
ANTHROPIC_THINKING = os.environ.get("ANTHROPIC_THINKING", "adaptive")

# Each web search costs money AND injects fetched pages into the context, so
# these are the biggest single cost lever in the batch.
SEARCH_MAX_USES_DRAFT = 6
SEARCH_MAX_USES_VERIFY = 4

# Anthropic's web-search tool accepts `user_location` for only a few countries,
# and "LT" is rejected with a 400 that aborts the batch. Empty = search without
# a location hint (the prompts already tell the model to search in Lithuanian).
ANTHROPIC_SEARCH_COUNTRY = ""
ANTHROPIC_SEARCH_TIMEZONE = "Europe/Vilnius"

# ── RSS sources ─────────────────────────────────────────────────────────────
# Harvest is resilient: a dead feed is logged and skipped, never fatal.
# Add/remove freely — more niche sources (sports, showbiz) improve batch variety.
FEEDS: list[dict] = [
    {"name": "LRT",          "url": "https://www.lrt.lt/?rss"},
    {"name": "LRT Sportas",  "url": "https://www.lrt.lt/naujienos/sportas?rss"},
    {"name": "15min",        "url": "https://www.15min.lt/rss"},
    {"name": "15min Sportas","url": "https://www.15min.lt/rss/sportas"},
    {"name": "Delfi",        "url": "https://www.delfi.lt/rss/feeds/daily.xml"},
    {"name": "Delfi Sportas","url": "https://www.delfi.lt/rss/feeds/sportas.xml"},
    {"name": "Lrytas",       "url": "https://www.lrytas.lt/rss"},
    {"name": "VZ",           "url": "https://www.vz.lt/rss"},
    # TV3: both /rss and /rss/naujienos return an HTML page, not a feed
    # ("not well-formed (invalid token)"), so there is nothing to parse.
    # Its stories still reach us through Google News LT below.
    # Economy / business / geopolitics — the informative side of the batch.
    {"name": "LRT Verslas",  "url": "https://www.lrt.lt/naujienos/verslas?rss"},
    {"name": "15min Verslas","url": "https://www.15min.lt/rss/verslas"},
    {"name": "Delfi Verslas","url": "https://www.delfi.lt/rss/feeds/verslas.xml"},
    {"name": "LRT Pasaulis", "url": "https://www.lrt.lt/naujienos/pasaulyje?rss"},
    # Aggregated top stories — catches what several outlets converge on.
    {"name": "Google News LT",
     "url": "https://news.google.com/rss?hl=lt&gl=LT&ceid=LT:lt"},
]

# Two weeks, sampled evenly per day (see harvest.harvest) — markets should come
# from the period's biggest stories, not just whatever ran yesterday. Within a
# day, stories covered by several outlets rank first: multi-outlet coverage is
# the one engagement signal RSS actually carries.
HARVEST_DAYS = 14
HARVEST_MAX_HEADLINES = 160

# ── PULSE: live social / attention signal (stage 1b) ────────────────────────
# News RSS shows what newsrooms published; the pulse shows what Lithuanians are
# actually searching, discussing and looking up — the culture/hype layer news
# misses. Each source is resilient (a failure is logged and skipped) and every
# signal carries a checkable attention number for the attention gate. See
# arbus/pulse.py. Turn the whole stage off with PULSE_ENABLED=False.
PULSE_ENABLED = True
PULSE_MAX_PER_SOURCE = 12       # cap signals kept per source (keeps the prompt tight)
# Entertainment charts (YouTube trending, Apple Music) are seasoning, not the
# backbone — keep them from flooding the prompt with view-count bait.
PULSE_ENTERTAINMENT_CAP = 5
GOOGLE_TRENDS_GEO = "LT"        # also used as YouTube regionCode
REDDIT_SUBS = ["lietuva", "Lithuania"]  # lietuva = LT-language, Lithuania = mixed/expat
# TikTok Creative Center (best-effort; may be empty if LT is unsupported there).
TIKTOK_COUNTRY = "LT"           # try "" or a bigger market as a fallback if LT is empty
TIKTOK_PERIOD = 7               # trend window in days: 7, 30 or 120
# Apple's public marketing RSS: key-free, genuinely country-scoped charts.
APPLE_STOREFRONT = "lt"

# Nasdaq Vilnius watchlist — the household names whose share moves can seed
# informative "Ar akcijų kaina viršys X?" markets. Quotes come from Yahoo
# Finance's public chart JSON (key-free; ".VS" = Vilnius). A delisted or
# renamed ticker just yields no signal — per-ticker failures never propagate.
NASDAQ_VILNIUS_TICKERS: list[tuple[str, str]] = [
    ("IGN1L.VS", "Ignitis grupė"),
    ("TEL1L.VS", "Telia Lietuva"),
    ("SAB1L.VS", "Artea bankas"),
    ("APG1L.VS", "Apranga"),
    ("KNF1L.VS", "KN Energies"),
    ("GRG1L.VS", "Grigeo"),
    ("NTU1L.VS", "Novaturas"),
    ("AKO1L.VS", "AKOLA Group"),
]

# Optional keyed sources — inert until the key is set, so enabling them is safe.
#   YOUTUBE_API_KEY  free from Google Cloud (YouTube Data API v3) -> Trending LT.
# Spotify Top 50 Lietuva is a documented future source (client-credentials);
# add a _spotify() fetcher to pulse.SOURCES when you wire a key.

# ── Batch shape ─────────────────────────────────────────────────────────────
DEFAULT_BATCH_SIZE = 35

# Drafting is THEMED: each chunk carries a mandatory theme, so the balance is
# enforced structurally in code — the model cannot drift into view-count bait
# when its chunk mandate is state affairs. (label, share, mandate-for-prompt).
# The last theme absorbs rounding remainders.
# Themed quotas apply to the ACCEPTED batch, not just the drafted one. The
# informative themes carry more factual constraints (launch date, already
# decided, sources), so they lose more candidates at validation — without
# top-ups a batch drifts back to whatever survives most easily, which is
# culture. After validation, themes below their target are re-drafted.
TOPUP_ROUNDS = 2          # extra drafting rounds for under-delivered themes
TOPUP_OVERDRAFT = 2.0     # draft this multiple of the deficit (some fail again)
# Hard ceiling on drafting calls per batch. Top-ups cost real money, so a batch
# where every theme keeps failing must not spiral: once this many draft calls
# have been made, the batch finishes with whatever it has.
MAX_DRAFT_CALLS = 12
# Rejections from earlier in the same run are fed back into later chunk prompts
# so the model stops repeating a mistake while the batch is still being drafted.
REJECT_FEEDBACK_LIMIT = 8

DRAFT_THEMES: list[tuple[str, float, str]] = [
    ("valstybė ir geopolitika", 0.30,
     "ONLY draft markets about state affairs and geopolitics: the war in "
     "Ukraine and its milestones (ceasefire, negotiations, sanctions), security "
     "and airspace incidents (drones, balloons, GPS jamming, red alerts), "
     "NATO/EU decisions affecting Lithuania, Belarus/Kaliningrad tensions, "
     "Lithuanian foreign-policy milestones (e.g. China relations measured by "
     "concrete events like ambassadors returning), Seimas and presidential "
     "decisions, elections, party ratings."),
    ("ekonomika ir finansai", 0.30,
     "ONLY draft markets about the economy, finance and state statistics. "
     "PREFER THE SIMPLE, WIDELY UNDERSTOOD INDICATORS most of the batch: "
     "nedarbo lygis, infliacija, degalų ir elektros kaina, minimali alga ir "
     "vidutinis atlyginimas, mokesčių pakeitimai (GPM, PVM, „Sodra“), Euribor "
     "ir būsto paskolų palūkanos, būsto kainos. EVERYDAY-MONEY DECISIONS "
     "belong here and are currently under-represented: pensijos ir jų "
     "indeksavimas, pensijų kaupimo pakopos ir jų reforma, „Sodra“, "
     "kompensacijos, šildymo sezono kaina, ES/ECB sprendimai, kurie pasiekia "
     "kiekvieno kišenę (palūkanų sprendimai, naujų eurų banknotų dizainas). "
     "A market a normal person understands in one reading beats a technically "
     "impressive one. "
     "Secondary: demographics (gyventojų skaičius, emigracija — Statistikos "
     "departamentas), BVP, valstybės skola, Nasdaq Vilnius stocks from the "
     "pulse (threshold + coarse deadline, current price in the rationale), "
     "Lithuanian companies (Vinted, Ignitis, Telia, bankai)."),
    ("sportas", 0.20,
     "ONLY draft markets about Lithuanian sport: national teams, "
     "Žalgiris/Rytas in European competitions, LT athletes and their clubs, "
     "transfers. Outcomes must be concrete and checkable — 'ar pateks į kitą "
     "etapą?', 'ar laimės rungtynes?' — NEVER metaphors like 'atsities' or "
     "'sužibės'."),
    ("kultūra ir visuomenė", 0.20,
     "ONLY draft markets about culture and society with MASS recognition: big "
     "festivals and events (sold-out only if the event is 2+ weeks away), "
     "kino box-office ('ar taps žiūrimiausiu filmu Lietuvoje pagal savaitgalio "
     "žiūrovus?' — better than any view-count), TV shows and music releases, "
     "top-tier influencers everyone knows (the Dirkstys tier). AT MOST ONE "
     "views/followers/streams metric market in this chunk, only for something "
     "the whole country knows."),
]

# App goes live ~mid-September 2026. Per team direction, generate markets that
# resolve from 2026-08-10 onward (no market may resolve earlier than this).
# Set to "" once live (then only "must be in the future" applies).
MIN_RESOLVE_DATE = "2026-08-10"
# Duration mix targets (informational — enforced softly via prompt + report)
DURATION_MIX = {"short": 0.30, "medium": 0.50, "long": 0.20}
# Boundaries in days used to (re)classify duration from resolve_by
SHORT_MAX_DAYS = 2
MEDIUM_MAX_DAYS = 30

# ── Gambling-language linter ────────────────────────────────────────────────
# Stems matched case-insensitively inside user-facing Lithuanian text.
# NOTE: the bare word "bet" is deliberately absent — in Lithuanian it means
# "but" and is one of the most common conjunctions in the language.
BANNED_STEMS = [
    "lažyb",        # lažybos, lažybų ...
    "lažin",        # lažintis, lažinuosi ...
    r"\bstatym",    # statymas as its own word — NOT įstatymas (law) / pastatymas
    "koeficient",   # koeficientas (odds)
    "kazino",
    "azart",        # azartiniai lošimai
    "lošim",        # lošimas, lošimai
    "bukmeker",
    "totalizator",
    "jackpot",
    "wager",
    "odds",
]

# ── Blocked subjects (editorial taste, curated by the team) ─────────────────
# Case-insensitive substrings. Markets whose question mentions any of these
# are rejected, and the generator is told upfront to avoid them. Add niche
# creators or dead-end angles here as you spot them in batches.
BLOCKED_SUBJECTS = [
    "šeškės",                 # niche creator group, not mass-audience
    "statybų transliacij",    # stadium-livestream-metrics angle: about nothing
]

# ── Headline-clarity linter ─────────────────────────────────────────────────
# Vague phrasing that must never appear in the QUESTION text (rigor belongs
# in the resolution criteria, the headline must be instantly clear).
VAGUE_STEMS = [
    "panaš",            # panašaus lygio, panašiai ...
    "artimiaus",        # artimiausią savaitę / artimiausiu metu
    "netrukus",
    "greitu metu",
    "pvz",              # examples belong in the rules, not the headline
    "ir pan",
    "ir kt",
    "ar kitok",         # "ar kitoks sprendimas" style catch-alls
    # "any member of an undefined class" — resolution nightmare unless the
    # class is objectively defined (precise measurable phrases like
    # "bent vieną dieną 30 °C" are fine and don't match these stems)
    "bent vienoje didž",
    "bent vienas didel",
    "bent viena didel",
    "bent vienas stambus",
    "bent viena stambi",
    "bent vienas žinomas",
    "bent viena žinoma",
    "bent vienas populiarus",
    "kardinaliai",      # "kardinaliai pakeistą įvaizdį" — unmeasurable intensifier
    "įvaizd",           # "išlaikys įvaizdį" — a person's "image" is not checkable
    "emocing",          # "emocinga reakcija" — mood, not a verifiable event
    # Sports-page metaphors — no source ever reports that a team "bounced
    # back"; ask for the concrete outcome ("pateks į kitą etapą") instead.
    "atsities",
    "atsigaus",
    "sužibės", "suzibes",
    "nustebins",
]

# ── Open-ended binary questions need a time anchor ──────────────────────────
# "Ar rinktinė paskelbs galutinį sąrašą?" WILL eventually happen — without a
# time bound the question is meaningless. A binary headline must contain either
# a coarse time reference or an event scope (the event then defines timing).
TIME_HINT_STEMS = [
    "sausio", "sausį", "sausi", "vasario", "vasarį", "vasari", "kovo", "kovą",
    "balandžio", "balandi", "balandį", "gegužės", "geguzes", "gegužę",
    "birželio", "birzelio", "birželį", "liepos", "liepą", "liepa",
    "rugpjūčio", "rugpjucio", "rugpjūtį", "rugsėjo", "rugsejo", "rugsėjį",
    "spalio", "spalį", "spali", "lapkričio", "lapkricio", "lapkritį",
    "gruodžio", "gruodzio", "gruodį",
    "šiemet", "siemet", "vasarą", "vasara", "rudenį", "rudeni", "ruden",
    "žiemą", "ziema", "pavasarį", "pavasari", "sezon", "iki 20", "per 20",
    # Relative windows are time bounds too — "per mėnesį", "pirmą rodymo
    # savaitgalį", "per 30 dienų", "ketvirtį", "metų pabaigoje".
    "savaitgal", "savait", "mėnes", "menes", "ketvirt", "dienų", "dienu",
    # Stems, not full forms: "pabaigos"/"pabaigoje", "pradžios"/"pradžioje".
    "per parą", "pabaig", "pradži", "pradzi", "metų gale", "metu gale",
]

# A bare year is a time bound too — "Ar ES 2026 m. priims sankcijų paketą?" is
# bounded, and rejecting it cost 11 good markets in one batch. Matched
# separately from the stem list because a substring like "20" would fire on
# thresholds ("20 tūkst.") and dates inside entity names.
TIME_HINT_RE = re.compile(r"\b20\d{2}\b")
EVENT_SCOPE_STEMS = [
    "rungtyn", "final", "čempionat", "cempionat", "turnyr", "lyg", "etap",
    "rinkim", "festival", "koncert", "apdovanojim", "olimp", "grand prix",
    "švent", "svent", "atrank", "varžyb", "varzyb", "eurovizij", "pusfinal",
    "ture", "tour",
]

# ── Headline-format linter ──────────────────────────────────────────────────
# Detail that belongs in the RESOLUTION RULES must never clutter the QUESTION.
# Parentheses are banned outright (parenthetical caveats go in the rules), and
# these words are rules-only noise in a headline:
#   "viešai" (publicly) — once a market resolves on an announcement it is public
#   by definition; say "must be a public statement" in the rules instead.
# Matched as stems (viešai, viešas; oficialiai, oficialus, oficialų...).
#   "viešai"   — anything announced is public by definition
#   "oficial*" — if a market resolves at all, it resolves on official facts;
#                where "official" is defined belongs in the rules
HEADLINE_NOISE_WORDS = ["viešai", "viešas", "oficial"]

# Causal / speculative links cannot be verified: no source ever reports that
# event A "encouraged" or "caused" event B. Predict the second event directly.
# Kills: "Ar „Mere" uždarymas paskatins kito tinklo atėjimą?"
CAUSAL_RE = re.compile(
    r"\b(paskatins|paskatino|paskatintų|lems|nulems|nulemtų|sąlygos|sąlygotų"
    r"|turės\s+įtakos|padarys\s+įtaką|dėl\s+to\s+bus)\b",
    re.IGNORECASE,
)

# ── Unresolvable-option / subjective-premise linter ─────────────────────────
# Multi-outcome options must be concrete, mutually exclusive, publicly
# checkable outcomes. These stems mark options that instead describe intent,
# tempo, tone, or degrees of ambiguity — you cannot verify "kept it slow" or
# "quietly shelved" against any source, so the market can never resolve.
# Matched case-insensitively inside OPTION text. Archetype this kills: a market
# whose options were "actively push the impeachment" / "keep it on the slow
# track" / "quietly put it in a drawer" — moods, not outcomes.
SUBJECTIVE_OPTION_STEMS = [
    "ant lėto",
    "į stalčių",
    "i stalčių",
    "be aiškaus",
    "be aiškių",
    "be aktyvi",         # "be aktyvių veiksmų"
    "faktiškai",
    "de facto",
    "neoficial",         # neoficialiai
    "tyliai",
    "aktyviai stum",     # aktyviai stumti
    "palikti kaip yra",
    "padėta į stal",
    "padeta i stal",
    "pusiau",            # "pusiau paremti" and similar half-measures
    "ant stabdž",        # "nuleisti ant stabdžių" — colloquial, unverifiable
    "po kilimu",         # "nušluoti po kilimu"
    "numarin",           # "numarinti tylomis"
    "įšaldy", "išaldy", "užšaldy", "uzsaldy",  # "įšaldyti klausimą"
]

# Undefined-superlative framings: "the main / primary <decision / stance /
# message / signal / reaction>" — who decides which is 'the main' one is itself
# subjective, so the market is unresolvable. Match a "pagrindin*" superlative
# close to a subjective-stance noun in the QUESTION. Kept narrow (proximity +
# specific nouns) so legit uses like "pagrindinis prizas/favoritas" are safe.
MAIN_STANCE_RE = re.compile(
    r"pagrindin\w*(?:\W+\w+){0,6}?\W+"
    r"(sprendim|pozicij|nuostat|žinut|zinut|signal|reakcij|krypt|žingsn|zingsn|akcent|turin)",
    re.IGNORECASE,
)

# Data-source attribution belongs in the resolution rules, not the QUESTION.
# "pagal X duomenis / kainoraštį / rodiklį" in a headline is noise — Polymarket
# keeps the source of truth in the rules and the sources list, and the headline
# just states the idea. Match "pagal ... <source-word>" in the question.
SOURCE_ATTR_RE = re.compile(
    r"pagal\b(?:\W+\w+){0,6}?\W+"
    r"(duomen|kainoraš|rodikl|skelbiam|tarnyb|skaičiav|skaiciav|apskaič|vertinim|statistik|ataskait)",
    re.IGNORECASE,
)

# Day-precision dates do not belong in a headline. The resolve_by field and the
# rules carry exact timing; the headline may reference a MONTH or a season
# ("rugpjūčio mėnesį", "šį rudenį") or nothing at all. This is the difference
# between the bloated
#   "Ar Vilniuje bent vieną dieną tarp 2026 m. rugpjūčio 1–31 d. oficialiai bus
#    užfiksuotas ≥30 mm paros kritulių kiekis pagal LHMT duomenis?"
# and the clean
#   "Ar Vilniuje rugpjūčio mėnesį bus užfiksuotas ≥30 mm paros kritulių kiekis?"
_LT_MONTHS = ("sausio|vasario|kovo|balandžio|gegužės|birželio|liepos|"
              "rugpjūčio|rugsėjo|spalio|lapkričio|gruodžio")
HEADLINE_DATE_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}"                                   # 2026-08-15
    rf"|(?:{_LT_MONTHS})\s+\d{{1,2}}\s*(?:[–—-]\s*\d{{1,2}}\s*)?d\."  # rugpjūčio 15 d.
    r"|\b\d{1,2}\s*d\.\s*(?:mėn\.)?\s*\d{4}",              # 15 d. 2026
    re.IGNORECASE,
)

# ── Categories ──────────────────────────────────────────────────────────────
# Free-text categories produced labels like "Ekonomika & finansai (atlyginimai,
# valstybės statistika)", which makes reports and any app-side filtering
# useless. Model output is normalized to this fixed set (first keyword match
# wins; anything unmatched becomes "kita").
CATEGORIES: dict[str, list[str]] = {
    "geopolitika":  ["geopolit", "karas", "ukrain", "rusij", "nato", "saugum",
                     "gynyb", "dron", "oro erdv", "sankcij", "baltarus"],
    "politika":     ["politik", "seim", "prezident", "vyriausyb", "rinkim",
                     "partij", "įstatym", "svietim", "švietim", "education"],
    "ekonomika":    ["ekonom", "finans", "kain", "infliac", "atlyginim", "mokes",
                     "palūkan", "palukan", "bvp", "skol", "nedarb", "biudzet",
                     "biudžet", "economy", "economics", "demograf", "gyventoj"],
    "verslas":      ["versl", "įmon", "imon", "akcij", "birž", "birz", "bank",
                     "company", "business", "stock"],
    "sportas":      ["sport", "krepšin", "krepsin", "futbol", "žalgir", "zalgir",
                     "rytas", "olimp", "rinktin"],
    "kultura":      ["kultur", "kultūr", "muzik", "kin", "film", "festival",
                     "renginy", "rengin", "tv", "culture"],
    "influenceriai": ["influenc", "kūrėj", "kurej", "tiktok", "youtub",
                      "instagram", "socialini"],
    "orai":         ["ora", "weather", "karšt", "karst", "temperat", "lhmt"],
}
DEFAULT_CATEGORY = "kita"

# ── Dedupe ──────────────────────────────────────────────────────────────────
DEDUPE_SIMILARITY = 87        # rapidfuzz token_set_ratio threshold (0-100)
DEDUPE_LOOKBACK_DAYS = 60     # compare against markets created in this window

# ── Verification ────────────────────────────────────────────────────────────
# Candidates per verification call. Bigger chunks = fewer calls, and the system
# prompt + tool setup is paid once per call rather than per candidate.
VERIFY_CHUNK_SIZE = 12
# Z.AI (Zhipu) GLM — OpenAI-compatible endpoint. Unlike Perplexity it is NOT
# search-native, so research/verification depend on the server-side web_search
# tool below; without it GLM answers from memory and verification degrades.
ZAI_BASE_URL = "https://api.z.ai/api/paas/v4/chat/completions"
ZAI_MODEL = "glm-4.6"
ZAI_STRUCTURE_MODEL = "glm-4.6"
ZAI_WEB_SEARCH = True
# GLM is verbose and truncated a 15-candidate batch at 8K, producing JSON that
# ends mid-object. Give the structuring step room; the floor applies only to
# this provider.
ZAI_STRUCTURE_MIN_TOKENS = 20000
# GLM also drifts from the schema on long batches, so it drafts in smaller
# chunks than the search-native providers.
ZAI_DRAFT_CHUNK_SIZE = 8

PERPLEXITY_MODEL = "sonar-pro"            # research + verification (search-native)
# sonar-pro for structuring too: the small "sonar" model corrupts Lithuanian
# diacritics when copying text (tested 2026-07-13).
PERPLEXITY_STRUCTURE_MODEL = "sonar-pro"

# Draft in chunks: Perplexity output caps around 8K tokens, so one call can't
# reliably carry 35 candidates through draft + structure.
DRAFT_CHUNK_SIZE = 15

# ── Resolution system (see Notion "Resolution logika", v1) ──────────────────
# Arbus runs an AMM and takes the other side of user bets, so a slow resolution
# is a leak: whoever already knows the outcome can keep trading against a stale
# price. Freezing is therefore immediate and cheap, and every later step is
# deliberate.
#
# Economy (Arbucks). Start balance 250, max bet 10 000, 1 EUR ~ 400 Arbucks.
# Bonds are deliberately large relative to the start balance so that being
# wrong hurts a new user rather than costing something they do not miss.
PROPOSAL_BOND_STANDARD = 200
PROPOSAL_BOND_IMPORTANT = 450
# Polymarket's dispute bond equals the proposer's bond, and for good reason: a
# challenger risking more than the proposer needs to be far more certain than
# the proposer was, so nobody disputes and false reports go unchallenged. Ours
# mirrors the bond of the request being challenged — 200 against a standard
# report, 450 against an important one. CHALLENGE_BOND is only the fallback
# when the original bond cannot be read.
CHALLENGE_BOND = PROPOSAL_BOND_STANDARD
REWARD_CORRECT_PROPOSAL = 30
CHALLENGE_REWARD_SHARE = 0.50      # of the proposal bond, to a correct challenger
ELIGIBILITY_MIN_PREDICTIONS = 20   # before a user may propose a resolution
CHALLENGE_WINDOW_HOURS = 2

# Circuit breaker. A price move alone is not evidence of leaked information —
# one whale simply predicting hard would trip it — so a move only counts when
# several distinct users move the same way inside the window.
CB_WINDOW_MINUTES = 10
CB_PRICE_MOVE = 0.15               # 15 percentage points inside the window
CB_MIN_DISTINCT_USERS = 3

# Admin presses resolve in the app dashboard; settlement waits this long so a
# misclick can be undone. Payouts cannot be clawed back once made.
SETTLEMENT_DELAY_MINUTES = 5

# Markets whose resolve_by has passed but which nobody reported would otherwise
# sit open forever. This is an ADDITION to the two paths in the Notion spec —
# set to 0 to disable it and rely only on the circuit breaker and user reports.
DEADLINE_SWEEP_GRACE_DAYS = 1

# The advisory check runs on every freeze, so it must stay cheap enough that
# nobody hesitates to freeze a market (~EUR 0.05 per check: one call, three
# searches at $0.01 each plus the tokens the search results add).
AICHECK_MAX_SEARCHES = 3

# ── No-event fallback: why VOID is not a normal outcome ─────────────────────
# Polymarket and Kalshi settle "the event did not happen" from the market's own
# rules — usually NO — rather than cancelling the market. Cancelling is their
# last resort for a broken market, not their answer to a cancelled concert.
# We copy that: every market's rules must say up front what happens if the
# event is called off, postponed or never announced. If the model forgets, the
# validator appends the default below, so no market can ship without the clause.
FALLBACK_RE = re.compile(
    r"neįvyk|neivyk|atšauk|atsauk|nukel|nepaskelb|nebus paskelb|nesutei", re.I
)
FALLBACK_BINARY = ("Jei įvykis neįvyks, bus atšauktas arba nukeltas vėlesniam "
                   "laikui nei nurodyta data — rinka baigiasi „Ne“.")
FALLBACK_MULTI = ("Jei iki nurodytos datos oficialus rezultatas nebus "
                  "paskelbtas (įvykis atšauktas ar nukeltas), rinka sprendžiama "
                  "pagal pirmą oficialų rezultatą, paskelbtą po tos datos.")

# ── Job 2: resolution monitoring ────────────────────────────────────────────
# Sources publish after the fact, so a market resolving on the 1st may only be
# checkable on the 3rd. Markets become due once their date has passed by this
# many days.
RESOLVE_GRACE_DAYS = 1
RESOLVE_CHUNK_SIZE = 8      # markets per resolution LLM call
# A verdict is applied automatically only at HIGH confidence with a cited
# source; everything else waits for a human. Resolving wrongly takes credits
# from users who earned them, which is much harder to undo than resolving late.
RESOLVE_AUTO_APPLY_CONFIDENCE = "HIGH"

# ── Market images ───────────────────────────────────────────────────────────
# Each market gets a picture from its own source article's og:image tag. See
# arbus/images.py for the rights caveat before showing these to users.
IMAGES_ENABLED = True
IMAGE_MAX_SOURCES = 3     # stop after this many source URLs per candidate
IMAGE_TIMEOUT = 12

# ── Publishing to the Arbus app ─────────────────────────────────────────────
# Set ARBUS_API_URL + ARBUS_API_KEY in .env once the app endpoint exists.
# `python -m arbus publish --dry-run` prints the payload without sending.
ARBUS_API_URL = os.environ.get("ARBUS_API_URL", "")
ARBUS_API_KEY = os.environ.get("ARBUS_API_KEY", "")
ARBUS_API_TIMEOUT = 30
# Read the app's live markets before drafting, so the generator never proposes
# something users can already see. Skipped silently when the app is unreachable.
APP_DEDUPE = True
# Which market statuses in the APP mean "trading is stopped, an admin has to
# decide". The app owns these strings, so the list is generous on purpose and
# matched case-insensitively; `python -m arbus app --schema` shows the real
# values in use.
APP_FROZEN_STATUSES = {
    "paused", "pristabdyta", "pristabdytas", "suspended",
    "stopped", "sustabdyta", "sustabdytas", "halted",
    "frozen", "uzsaldyta", "užšaldyta", "pending_resolution", "resolving",
}

# ── Paths (relative to repo root) ───────────────────────────────────────────
DB_PATH = "data/arbus.db"
REPORT_DIR = "reports"
EXPORT_DIR = "exports"
