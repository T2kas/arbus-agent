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

# App goes live in August — until then no market may resolve earlier than this.
# Set to "" once live (then only "must be in the future" applies).
MIN_RESOLVE_DATE = "2026-08-01"
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
]

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
]

# Undefined-superlative framings: "the main / primary <decision / stance /
# message / signal / reaction>" — who decides which is 'the main' one is itself
# subjective, so the market is unresolvable. Match a "pagrindin*" superlative
# close to a subjective-stance noun in the QUESTION. Kept narrow (proximity +
# specific nouns) so legit uses like "pagrindinis prizas/favoritas" are safe.
MAIN_STANCE_RE = re.compile(
    r"pagrindin\w*(?:\W+\w+){0,6}?\W+"
    r"(sprendim|pozicij|nuostat|žinut|zinut|signal|reakcij|krypt|žingsn|zingsn)",
    re.IGNORECASE,
)

# ── Dedupe ──────────────────────────────────────────────────────────────────
DEDUPE_SIMILARITY = 87        # rapidfuzz token_set_ratio threshold (0-100)
DEDUPE_LOOKBACK_DAYS = 60     # compare against markets created in this window

# ── Verification ────────────────────────────────────────────────────────────
VERIFY_CHUNK_SIZE = 8         # candidates per verification LLM call
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
