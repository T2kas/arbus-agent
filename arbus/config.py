"""Central configuration for the Arbus market generator."""

from __future__ import annotations

import os
import re
from pathlib import Path


def _clean_env_value(value: str) -> str:
    """Strip an inline `# comment` and surrounding quotes from a .env value.

    Without this, a line like `ANTHROPIC_AICHECK_MODEL=claude-sonnet-5  # cheaper`
    sets the value to "claude-sonnet-5  # cheaper" — a garbage model name that
    the API rejects with a 404. An inline comment is any `#` preceded by
    whitespace; a `#` inside a quoted value or with no space before it (e.g. a
    URL fragment) is kept.
    """
    value = value.strip()
    if value and value[0] not in ("'", '"'):
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        value = value[1:-1]
    return value


def _load_dotenv() -> None:
    """Minimal .env loader (repo root); real env vars always win."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            value = _clean_env_value(value)
            if value:
                os.environ.setdefault(key.strip(), value)


_load_dotenv()

def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in ("1", "on", "true", "yes")


# Generation model. Sonnet by default: a full Opus batch with adaptive thinking
# and many searches per chunk burned ~$5, and with the same web search and
# deterministic gates around it Sonnet loses very little. Easy toggle to compare:
#   ARBUS_OPUS=on   -> generate with Opus 5 (sharper, pricier)
#   ARBUS_OPUS=off  -> Sonnet 5 (default, cheaper)
# An explicit ANTHROPIC_MODEL always wins over the toggle.
MODEL = os.environ.get("ANTHROPIC_MODEL") or (
    "claude-opus-5" if _flag("ARBUS_OPUS") else "claude-sonnet-5")
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
# ...but the resolution check keeps it OFF: with thinking on, Sonnet produced a
# reasoning trace before each of its search round-trips, making a 9-market check
# take ~6 min per market (live-measured). The aicheck prompt already spells out
# the reasoning steps, so "off" is far faster and cheaper with little accuracy
# cost. Set ANTHROPIC_AICHECK_THINKING=adaptive to turn it back on.
AICHECK_THINKING = os.environ.get("ANTHROPIC_AICHECK_THINKING", "off")

# Each web search costs money AND injects fetched pages into the context, so
# these are the biggest single cost lever in the batch.
SEARCH_MAX_USES_DRAFT = 6
SEARCH_MAX_USES_VERIFY = 4

# Localize Anthropic web search to Lithuania. The `country` field rejects "LT"
# with a 400 that aborts the call, but `city`/`region`/`timezone` are free-form
# and Lithuania can use them — and localizing is what makes the tool return
# LRT/Delfi pages instead of US results (the reason resolution missed findable
# M.A.M.A./Eurovision outcomes). Leave COUNTRY empty; a bad code kills the call.
ANTHROPIC_SEARCH_CITY = os.environ.get("ANTHROPIC_SEARCH_CITY", "Vilnius")
ANTHROPIC_SEARCH_REGION = os.environ.get("ANTHROPIC_SEARCH_REGION", "Vilnius")
ANTHROPIC_SEARCH_COUNTRY = os.environ.get("ANTHROPIC_SEARCH_COUNTRY", "")
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

PERPLEXITY_MODEL = os.environ.get("PERPLEXITY_MODEL", "sonar-pro")  # draft + verify
# sonar-pro for structuring too: the small "sonar" model corrupts Lithuanian
# diacritics when copying text (tested 2026-07-13).
PERPLEXITY_STRUCTURE_MODEL = "sonar-pro"
# NOTE (tested 2026-07-28): for RESOLUTION, Anthropic web search beats
# Perplexity decisively — side by side, Opus found the exact LRT article, score
# and return-leg date for a Žalgiris tie while Perplexity (even
# sonar-reasoning-pro) returned "nerasta" for the same market and for Eurovision.
# The differentiator is the SEARCH BACKEND, not the model: Anthropic's
# web_search, localized to Vilnius, surfaces LRT/Delfi; Perplexity's does not.
# So prefer LLM_PROVIDER_AICHECK=anthropic. The sweet spot is Sonnet 5, which
# uses the SAME Anthropic search as Opus at about half the cost; Opus only for
# maximum reasoning on ambiguous cases. This Perplexity model applies only if
# you still route the check through Perplexity.
PERPLEXITY_AICHECK_MODEL = os.environ.get(
    "PERPLEXITY_AICHECK_MODEL", "sonar-reasoning-pro")

# ── OpenRouter: one key → OpenAI / DeepSeek / Gemini / … + web search ────────
# An OpenAI-compatible gateway for trying cheaper reasoning models than Opus.
# Web search (Exa) is added by appending ":online" to the model id, so any model
# can ground answers in current sources. Pick a model per stage by its id.
#   Resolution (reasoning matters): openai/gpt-5 (default), openai/o4-mini,
#     deepseek/deepseek-r1 (cheapest reasoning), google/gemini-2.5-pro.
#   Search engine: "exa" ($0.005/search) or "parallel" ($0.001, cheapest).
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-5")
OPENROUTER_AICHECK_MODEL = os.environ.get("OPENROUTER_AICHECK_MODEL", "openai/gpt-5")
OPENROUTER_STRUCTURE_MODEL = os.environ.get(
    "OPENROUTER_STRUCTURE_MODEL", "openai/gpt-5")
OPENROUTER_SEARCH_ENGINE = os.environ.get("OPENROUTER_SEARCH_ENGINE", "exa")
OPENROUTER_SEARCH_RESULTS = int(os.environ.get("OPENROUTER_SEARCH_RESULTS", "5"))

# ── OpenAI (native key, Responses API + built-in web search) ─────────────────
# Direct OpenAI, for testing GPT-5 / o-series on their own credits. Swap the
# model to compare: gpt-5 (default), gpt-5-mini (cheaper), o4-mini (reasoning).
# OpenAI's web search accepts a Lithuania country code, so it is localized here.
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
OPENAI_AICHECK_MODEL = os.environ.get("OPENAI_AICHECK_MODEL", "gpt-5")
OPENAI_STRUCTURE_MODEL = os.environ.get("OPENAI_STRUCTURE_MODEL", "gpt-5")
OPENAI_SEARCH_COUNTRY = os.environ.get("OPENAI_SEARCH_COUNTRY", "LT")
# Reasoning models spend output budget thinking; floor the visible-answer room
# generously, or GPT-5 can burn the whole budget reasoning and return an empty
# message (the weather market came back <no output>). Unused tokens are not
# billed, so a high cap is free insurance.
OPENAI_MIN_OUTPUT_TOKENS = int(os.environ.get("OPENAI_MIN_OUTPUT_TOKENS", "8000"))
# Reasoning effort for GPT-5 / o-series: "minimal" | "low" | "medium" | "high".
# Default "low" — GPT-5's default effort billed ~1 EUR for one check; low keeps
# the wrong-year/namesake reasoning at a fraction of the cost. "" omits it.
OPENAI_REASONING_EFFORT = os.environ.get("OPENAI_REASONING_EFFORT", "low")

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
ELIGIBILITY_MIN_PREDICTIONS = 5    # before a user may propose a resolution
                                   # (start low, raise once reporting works)
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

# Search budget = the main lever for BOTH cost and latency. Each web search is a
# separate server-side round-trip (Anthropic emits a `pause_turn` and we make
# another streaming call per search), so on a slow tier 5 searches meant ~5-6
# sequential calls and minutes per market — live-measured. Two tiers: when a
# resolver already handed us the deciding number (a stock price, a temperature)
# the model only has to read it, so a couple of searches suffice; when nobody
# cited a source and there is no feed (M.A.M.A., Eurovision) it has to FIND the
# outcome. `_run` picks the tier from whether facts were injected. Both are
# env-tunable — raise for thoroughness on hard events, lower for speed.
#
# NOTE on the OPEN tier: 3 was measured too tight. A search-heavy market
# (Eurovision, live) wanted a 4th search, hit the per-turn cap, and the model
# announced it could not finish — which _research_with_search_retry then treated
# as a failure and RE-RAN the whole sequence after a 20s wait (~5 min total). A
# single 4-search pass (~3 min) is both faster and cheaper than 3 + retry, and
# the cap-hit retry below bumps the budget instead of wasting a same-budget turn.
AICHECK_MAX_SEARCHES = int(os.environ.get("AICHECK_MAX_SEARCHES", "2"))
AICHECK_MAX_SEARCHES_OPEN = int(os.environ.get("AICHECK_MAX_SEARCHES_OPEN", "4"))
# Output budget for the aicheck call. Anthropic silently returns whatever it
# generated when this is hit mid-answer (only a log warning, no error), so a too-
# tight cap truncates the response before its SIŪLOMA BAIGTIS line — which then
# triggered a full retry that RE-RAN every web search, paying for them twice.
# You are billed for tokens the model actually generates, NOT for this cap, so a
# generous ceiling is essentially free: a short verdict still costs ~300 tokens,
# the cap only lets the model reach the end of it after its inline reasoning
# across several searches. Set high enough that truncation (and the wasteful
# re-search retry) effectively never happens; it stays well under Sonnet's
# per-response maximum.
AICHECK_MAX_TOKENS = int(os.environ.get("AICHECK_MAX_TOKENS", "16000"))
# The web-search tool gets rate-limited when many markets run in a burst (the
# request returns 200 but the model reports "limit exceeded" and finds nothing).
# Retry that single market after a backoff (also covers a truncated response),
# and pace the checks so it is rarer.
AICHECK_SEARCH_RETRIES = 1
AICHECK_SEARCH_BACKOFF_SECONDS = 20
# Hard wall-clock bound per check. A market whose event has not resolved yet
# (a live Conference League qualifier, live-measured) searches all the way to
# its budget and still lands on "dar neaišku" — the right verdict, but it took
# ~10 min to reach it and would stall a 9-market batch. Anthropic's keep-alive
# pings defeat an httpx read timeout and Windows has no signal.alarm, so the
# check runs on a daemon thread we stop waiting on after this many seconds and
# return the same honest unknown. Generous enough that a resolvable market
# (Eurovision finished in ~2 min) always completes; only the pathological
# unresolved case is cut off. Env-tunable for a faster interactive run.
AICHECK_TIMEOUT_SECONDS = int(os.environ.get("AICHECK_TIMEOUT_SECONDS", "300"))
# Prices for the per-check cost line (Anthropic list prices, USD per 1M tokens;
# search is per request). Rough on purpose — a "3 vs 15 cents" signal, not a
# bill. Defaults are Claude Sonnet 5 + web_search ($10/1000). Override per .env
# if you switch model or Anthropic changes prices.
AICHECK_PRICE_INPUT_PER_M = float(os.environ.get("AICHECK_PRICE_INPUT_PER_M", "3.0"))
AICHECK_PRICE_OUTPUT_PER_M = float(os.environ.get("AICHECK_PRICE_OUTPUT_PER_M", "15.0"))
AICHECK_PRICE_CACHE_READ_PER_M = float(os.environ.get("AICHECK_PRICE_CACHE_READ_PER_M", "0.30"))
AICHECK_PRICE_CACHE_WRITE_PER_M = float(os.environ.get("AICHECK_PRICE_CACHE_WRITE_PER_M", "3.75"))
AICHECK_PRICE_SEARCH = float(os.environ.get("AICHECK_PRICE_SEARCH", "0.01"))
AICHECK_EUR_PER_USD = float(os.environ.get("AICHECK_EUR_PER_USD", "0.92"))

# When a market cites a source, fetching that page ourselves (a free HTTP GET)
# and injecting its text lets the model READ the source instead of paying to
# web-search for it — each search pulls a full page (~5-15k tokens) into the
# input at input-token price, so this is the single biggest cost lever. With a
# real source in hand the model needs at most a confirming search, not a hunt.
AICHECK_SOURCE_FETCH = os.environ.get("AICHECK_SOURCE_FETCH", "on").strip().lower() != "off"
AICHECK_SOURCE_MAX_CHARS = int(os.environ.get("AICHECK_SOURCE_MAX_CHARS", "6000"))
AICHECK_MAX_SEARCHES_WITH_SOURCE = int(os.environ.get("AICHECK_MAX_SEARCHES_WITH_SOURCE", "1"))
# When the failure is the model hitting its per-turn search cap (it wanted MORE
# searches, not a rate limit), a backoff buys nothing — retry immediately with
# this many extra searches so the second turn can actually finish.
AICHECK_SEARCH_CAP_BUMP = int(os.environ.get("AICHECK_SEARCH_CAP_BUMP", "2"))
APP_CHECK_DELAY_SECONDS = 4       # pause between markets to spread search calls
# After the check, fetch every URL the model cited and confirm it actually
# loads. Fabricated links (the model has invented eurovision.tv and nba.com URLs
# that 404) are the clearest hallucination signal there is. A request that fails
# outright is treated as "unknown", never as proof the link is fake — so the
# sandbox, where all outbound calls fail, does not flag real sources.
AICHECK_VERIFY_URLS = True

# Optional keyless JSON feed of average fuel prices (LEA). There is no stable
# public endpoint today; point this at one when the team has it and resolvers.py
# will inject the real diesel/petrol price into fuel-market checks.
FUEL_PRICE_URL = os.environ.get("FUEL_PRICE_URL", "")
# The official LEA daily fuel-price page. No JSON API, so resolvers.py scrapes
# the average diesel/petrol price out of the HTML as a fallback.
FUEL_LEA_URL = os.environ.get(
    "FUEL_LEA_URL", "https://www.ena.lt/degalu-kainos-degalinese/")

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
    # paused / pristabdyta
    "paused", "pause", "pristabdyta", "pristabdytas", "suspended", "halted",
    # stopped / sustabdyta — trading ended, outcome not paid yet
    "stopped", "stop", "sustabdyta", "sustabdytas", "closed", "close",
    "ended", "finished", "locked", "trading_closed", "expired", "baigta",
    # explicitly waiting for a decision
    "frozen", "uzsaldyta", "užšaldyta", "pending", "pending_resolution",
    "awaiting_resolution", "resolving",
}
# ...but never re-check a market that is already settled. "closed" above is
# deliberately broad, and without this a resolved market would be checked (and
# paid for) on every run.
APP_SETTLED_STATUSES = {
    "resolved", "settled", "paid", "paid_out", "issprestas", "išspręstas",
    "cancelled", "canceled", "void", "atsaukta", "atšaukta", "archived",
}

# ── Paths (relative to repo root) ───────────────────────────────────────────
DB_PATH = "data/arbus.db"
REPORT_DIR = "reports"
EXPORT_DIR = "exports"


# Also check markets whose resolution date has passed while they are still
# trading. Nobody pauses those, so nothing would ever look at them — and under
# an AMM a known outcome trading at a stale price is money leaving the house.
APP_CHECK_OVERDUE = True

# The status `arbus watch --freeze` writes back to stop trading. Must match a
# value the app actually understands — check `arbus app --schema` first.
APP_FREEZE_STATUS = os.environ.get("APP_FREEZE_STATUS", "paused")

# ── Market health (arbus stats) ─────────────────────────────────────────────
# A market nobody trades is a wasted slot and, more usefully, evidence about
# what NOT to generate. A market everybody trades deserves promotion. Both are
# read straight from the app's trades, with no LLM involved — these cost 0.
DEAD_MARKET_DAYS = 7
DEAD_MARKET_MIN_TRADES = 5      # fewer bets than this in the window = dead
IMPORTANT_VOLUME = 15000        # Arbucks traded
IMPORTANT_USERS = 10            # distinct users
# How far our generated starting probability may sit from the market's live
# price before it is worth a look. This is the generator's calibration score:
# a market that opened at 20% and trades at 80% was mispriced at birth.
CALIBRATION_GAP = 0.25
