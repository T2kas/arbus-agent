"""Central configuration for the Arbus market generator."""

from __future__ import annotations

import os
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
    "statym",       # statymas, statymai ...
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
    "bent vienoje didž",
]

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
