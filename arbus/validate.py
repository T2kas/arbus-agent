"""Stage 4 — deterministic validation gates. No LLM involved.

Each gate either fixes a candidate in place (normalization) or rejects it
with a machine-readable reason. The philosophy: the prompt asks nicely,
the validators enforce.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date, timedelta

from rapidfuzz import fuzz

from . import config
from .schemas import Candidate

LT_MARKERS = set("ąčęėįšųūž")
LT_WORDS = re.compile(r"\b(ar|kas|kiek|kada|kur|iki|bus|per|daugiau|mažiau|taip|ne)\b", re.I)
EN_WORDS = re.compile(r"\b(will|the|who|what|when|there|more|less)\b", re.I)


def lint_gambling(text: str) -> list[str]:
    """Return banned gambling-language stems found in user-facing text.

    Stems are regex patterns (word boundaries allowed), so "statymas" is
    caught while "įstatymas" (law) passes.
    """
    low = text.lower()
    return [stem for stem in config.BANNED_STEMS if re.search(stem, low)]


def lint_blocked(question: str) -> list[str]:
    """Return team-blocked subjects mentioned in the question."""
    low = question.lower()
    return [s for s in config.BLOCKED_SUBJECTS if s in low]


def lint_vague(question: str) -> list[str]:
    """Return vague-phrasing stems found in the market question (headline)."""
    low = question.lower()
    return [stem for stem in config.VAGUE_STEMS if stem in low]


def looks_lithuanian(text: str) -> bool:
    has_lt = any(c in LT_MARKERS for c in text.lower()) or bool(LT_WORDS.search(text))
    clearly_en = bool(EN_WORDS.search(text)) and not LT_WORDS.search(text)
    return has_lt and not clearly_en


def classify_duration(resolve_by: date, today: date) -> str:
    days = (resolve_by - today).days
    if days <= config.SHORT_MAX_DAYS:
        return "short"
    if days <= config.MEDIUM_MAX_DAYS:
        return "medium"
    return "long"


def normalize_probabilities(probs: list[float]) -> list[float]:
    """Clamp to (0.02, 0.98) and renormalize to sum 1.0."""
    clamped = [min(max(p, 0.02), 0.98) for p in probs]
    total = sum(clamped)
    return [round(p / total, 3) for p in clamped]


def _norm_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text.lower())
    return re.sub(r"[^\w\s]", " ", text)


def is_duplicate(question: str, existing_questions: list[str]) -> str | None:
    """Return the matching existing question if `question` is a near-duplicate."""
    norm = _norm_text(question)
    for other in existing_questions:
        if fuzz.token_set_ratio(norm, _norm_text(other)) >= config.DEDUPE_SIMILARITY:
            return other
    return None


def validate_candidate(
    cand: Candidate, today: date, min_resolve: date | None = None
) -> tuple[Candidate | None, str | None]:
    """Validate and normalize one candidate.

    Returns (fixed_candidate, None) on success or (None, reason) on rejection.
    """
    user_facing = f"{cand.question_lt} {' '.join(cand.options_lt)} {cand.resolution_hint_lt}"

    hits = lint_gambling(user_facing)
    if hits:
        return None, f"gambling language: {', '.join(hits)}"

    blocked = lint_blocked(cand.question_lt)
    if blocked:
        return None, f"blocked subject: {', '.join(blocked)}"

    vague = lint_vague(cand.question_lt)
    if vague:
        return None, f"vague headline wording: {', '.join(vague)}"

    if not looks_lithuanian(cand.question_lt):
        return None, "question does not look Lithuanian"

    try:
        resolve_by = date.fromisoformat(cand.resolve_by)
    except ValueError:
        return None, f"unparseable resolve_by: {cand.resolve_by!r}"
    if resolve_by <= today:
        return None, f"resolve_by {cand.resolve_by} is not in the future"
    if min_resolve and resolve_by < min_resolve:
        return None, f"resolve_by {cand.resolve_by} is before app launch ({min_resolve})"
    if resolve_by > today + timedelta(days=365):
        return None, f"resolve_by {cand.resolve_by} is more than a year out"

    if cand.market_type == "binary":
        if [o.strip().lower() for o in cand.options_lt] != ["taip", "ne"]:
            cand.options_lt = ["Taip", "Ne"]
            if len(cand.probabilities) != 2:
                return None, "binary market without 2 probabilities"
    else:
        if not (3 <= len(cand.options_lt) <= 6):
            return None, f"multi market needs 3-6 options, got {len(cand.options_lt)}"

    if len(cand.probabilities) != len(cand.options_lt):
        return None, "probabilities/options length mismatch"
    if not 0.85 <= sum(cand.probabilities) <= 1.15:
        return None, f"probabilities sum to {sum(cand.probabilities):.2f}"
    cand.probabilities = normalize_probabilities(cand.probabilities)

    # Recompute duration from the date — the model's label is advisory only.
    cand.duration_class = classify_duration(resolve_by, today)

    # Sources must be actual links, not outlet names like "LRT"
    cand.sources = [s for s in cand.sources if s.startswith("http")]
    if not cand.sources:
        return None, "no grounding source URLs"

    return cand, None
