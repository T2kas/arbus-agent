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

MODEL = "claude-opus-4-8"

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
    # Economy / business / geopolitics — the informative side of the batch.
    {"name": "LRT Verslas",  "url": "https://www.lrt.lt/naujienos/verslas?rss"},
    {"name": "15min Verslas","url": "https://www.15min.lt/rss/verslas"},
    {"name": "Delfi Verslas","url": "https://www.delfi.lt/rss/feeds/verslas.xml"},
    {"name": "LRT Pasaulis", "url": "https://www.lrt.lt/naujienos/pasaulyje?rss"},
]

HARVEST_DAYS = 4          # look-back window for headlines
HARVEST_MAX_HEADLINES = 120

# ── PULSE: live social / attention signal (stage 1b) ────────────────────────
# News RSS shows what newsrooms published; the pulse shows what Lithuanians are
# actually searching, discussing and looking up — the culture/hype layer news
# misses. Each source is resilient (a failure is logged and skipped) and every
# signal carries a checkable attention number for the attention gate. See
# arbus/pulse.py. Turn the whole stage off with PULSE_ENABLED=False.
PULSE_ENABLED = True
PULSE_MAX_PER_SOURCE = 12       # cap signals kept per source (keeps the prompt tight)
GOOGLE_TRENDS_GEO = "LT"        # also used as YouTube regionCode
REDDIT_SUBS = ["lietuva", "Lithuania"]  # lietuva = LT-language, Lithuania = mixed/expat
# TikTok Creative Center (best-effort; may be empty if LT is unsupported there).
TIKTOK_COUNTRY = "LT"           # try "" or a bigger market as a fallback if LT is empty
TIKTOK_PERIOD = 7               # trend window in days: 7, 30 or 120

# Optional keyed sources — inert until the key is set, so enabling them is safe.
#   YOUTUBE_API_KEY  free from Google Cloud (YouTube Data API v3) -> Trending LT.
# Spotify Top 50 Lietuva is a documented future source (client-credentials);
# add a _spotify() fetcher to pulse.SOURCES when you wire a key.

# ── Batch shape ─────────────────────────────────────────────────────────────
DEFAULT_BATCH_SIZE = 35

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

# ── Dedupe ──────────────────────────────────────────────────────────────────
DEDUPE_SIMILARITY = 87        # rapidfuzz token_set_ratio threshold (0-100)
DEDUPE_LOOKBACK_DAYS = 60     # compare against markets created in this window

# ── Verification ────────────────────────────────────────────────────────────
VERIFY_CHUNK_SIZE = 8         # candidates per verification LLM call
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

# ── Paths (relative to repo root) ───────────────────────────────────────────
DB_PATH = "data/arbus.db"
REPORT_DIR = "reports"
EXPORT_DIR = "exports"
