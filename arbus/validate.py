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


def lint_unresolvable(question: str, options: list[str]) -> list[str]:
    """Return markers that a market cannot be objectively resolved.

    Two failure modes, both resolution nightmares:
      1. Multi-outcome options describing intent / tempo / tone / ambiguity
         ("aktyviai stumti", "ant lėto", "padėta į stalčių") rather than
         concrete, checkable outcomes.
      2. "The main <decision/stance/message>" framings, where deciding which
         outcome is 'the main' one is itself subjective.
    """
    hits: list[str] = []
    opts_low = " || ".join(o.lower() for o in options)
    for stem in config.SUBJECTIVE_OPTION_STEMS:
        if stem in opts_low:
            hits.append(f"subjective option: {stem!r}")
    if config.MAIN_STANCE_RE.search(question):
        hits.append("undefined 'pagrindinis sprendimas/pozicija' framing")
    if config.CAUSAL_RE.search(question):
        hits.append("unverifiable causal link (predict the event itself)")
    return hits


def lint_headline_format(question: str) -> list[str]:
    """Return headline-format violations: detail that belongs in the rules.

    Parentheses are banned (parenthetical caveats go in the resolution rules),
    as are rules-only qualifier words like "viešai" (public-by-definition once
    something is announced).
    """
    problems: list[str] = []
    if "(" in question or ")" in question:
        problems.append("parentheses (move detail to the rules)")
    if config.SOURCE_ATTR_RE.search(question):
        problems.append("data-source attribution (move to rules)")
    if config.HEADLINE_DATE_RE.search(question):
        problems.append("day-precision date (use a month/event or drop it)")
    low = question.lower()
    for word in config.HEADLINE_NOISE_WORDS:
        if re.search(rf"\b{re.escape(word)}\w*", low):
            problems.append(f"rules-only word {word!r}")
    return problems


def lint_open_ended(question: str, market_type: str) -> str | None:
    """Reject binary questions with no time anchor at all.

    "Ar rinktinė paskelbs galutinį sąrašą?" WILL happen eventually — the only
    real question is "by when", so a headline without any time reference is
    meaningless. Two anchors satisfy the rule: a coarse time hint (month,
    season, "šiemet", "iki 20xx") or an event scope (rungtynės, čempionatas,
    festivalis — the event defines its own window). Multi-outcome titles are
    exempt: "Naujas Palangos meras" is scoped by the election itself.
    """
    if market_type != "binary":
        return None
    low = question.lower()
    if any(stem in low for stem in config.TIME_HINT_STEMS):
        return None
    if config.TIME_HINT_RE.search(question):
        return None
    if any(stem in low for stem in config.EVENT_SCOPE_STEMS):
        return None
    return "open-ended question: add a coarse time bound (e.g. 'iki spalio', 'šiemet')"


def looks_lithuanian(text: str) -> bool:
    # Reject only text that is CLEARLY English. Short Lithuanian titles without
    # diacritics or function words ("Naujas Palangos meras") are legitimate now
    # that headlines may be titles, so absence of LT markers is not a failure.
    clearly_en = bool(EN_WORDS.search(text)) and not LT_WORDS.search(text)
    return not clearly_en


def normalize_category(category: str, question: str = "") -> str:
    """Map free-text model categories onto config.CATEGORIES.

    Models emit things like "Ekonomika & finansai (atlyginimai, valstybės
    statistika)", which breaks report grouping and app-side filtering. Match on
    the category text first, then fall back to the question itself.
    """
    # The question is checked first: it states what the market is actually
    # about, while the model's own label is often the themed-chunk name. A
    # sanctions market drafted under the economy mandate is geopolitics.
    for haystack in (question.lower(), category.lower()):
        for canonical, keywords in config.CATEGORIES.items():
            if any(kw in haystack for kw in keywords):
                return canonical
    return config.DEFAULT_CATEGORY


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


_QUOTED_RE = re.compile(r"[„\"“']([^„\"“']{2,60})[\"“”']")


def _quoted_titles(text: str) -> set[str]:
    """Quoted names in a question — song titles, clubs, companies."""
    return {m.strip().lower() for m in _QUOTED_RE.findall(text)}


def is_duplicate(question: str, existing_questions: list[str]) -> str | None:
    """Return the matching existing question if `question` is a near-duplicate.

    Questions built from the same template about different named works read as
    near-identical to a token-ratio check — two markets about different Jessica
    Shy songs were rejected as duplicates of each other. When both questions
    name quoted titles and none of them overlap, they are about different
    things regardless of how similar the wording is.
    """
    norm = _norm_text(question)
    titles = _quoted_titles(question)
    for other in existing_questions:
        if fuzz.token_set_ratio(norm, _norm_text(other)) < config.DEDUPE_SIMILARITY:
            continue
        other_titles = _quoted_titles(other)
        if titles and other_titles and not (titles & other_titles):
            continue  # same shape, different named subject
        return other
    return None


def ensure_fallback_clause(hint: str, market_type: str) -> str:
    """Guarantee the rules say what happens if the event never happens.

    A market whose rules are silent about cancellation is the reason
    prediction markets end up voided, and a void means handing stakes back and
    telling users the rules were incomplete. Polymarket and Kalshi write the
    non-occurrence outcome into the rules instead; this appends our default
    when the model forgot to.
    """
    if config.FALLBACK_RE.search(hint or ""):
        return hint
    default = (config.FALLBACK_BINARY if market_type == "binary"
               else config.FALLBACK_MULTI)
    hint = (hint or "").strip()
    if hint and not hint.endswith((".", "!", "?")):
        hint += "."
    return f"{hint} {default}".strip()


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

    # Options get the same clarity bar as the headline — no vague filler.
    vague_opts = lint_vague(" ".join(cand.options_lt))
    if vague_opts:
        return None, f"vague/unclear options: {', '.join(vague_opts)}"

    unresolvable = lint_unresolvable(cand.question_lt, cand.options_lt)
    if unresolvable:
        return None, f"unresolvable market: {', '.join(unresolvable)}"

    fmt = lint_headline_format(cand.question_lt)
    if fmt:
        return None, f"headline format: {', '.join(fmt)}"

    if not looks_lithuanian(cand.question_lt):
        return None, "question does not look Lithuanian"

    open_ended = lint_open_ended(cand.question_lt, cand.market_type)
    if open_ended:
        return None, open_ended

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

    # A Taip/Ne market must ASK something. A bare statement headline
    # ("LeBrono Jameso sezonas „76ers" klube") leaves "Taip" ambiguous — the
    # title asserts the thing the user is supposed to be predicting. Title-style
    # headlines stay legal for multi-outcome markets ("Naujas Palangos meras").
    if cand.market_type == "binary" and "?" not in cand.question_lt:
        return None, "binary market must be phrased as a question (missing '?')"

    if cand.market_type == "binary":
        if [o.strip().lower() for o in cand.options_lt] != ["taip", "ne"]:
            cand.options_lt = ["Taip", "Ne"]
            if len(cand.probabilities) != 2:
                return None, "binary market without 2 probabilities"
    else:
        # 2 named options are legitimate (head-to-head duels, "pakeis / paliks");
        # only a degenerate single option or an unreadable pile is rejected.
        if not (2 <= len(cand.options_lt) <= 6):
            return None, f"multi market needs 2-6 options, got {len(cand.options_lt)}"

    if len(cand.probabilities) != len(cand.options_lt):
        return None, "probabilities/options length mismatch"
    if not 0.85 <= sum(cand.probabilities) <= 1.15:
        return None, f"probabilities sum to {sum(cand.probabilities):.2f}"
    cand.probabilities = normalize_probabilities(cand.probabilities)

    # Recompute duration from the date — the model's label is advisory only.
    cand.duration_class = classify_duration(resolve_by, today)
    cand.category = normalize_category(cand.category, cand.question_lt)

    cand.resolution_hint_lt = ensure_fallback_clause(
        cand.resolution_hint_lt, cand.market_type)

    # Sources must be actual links, not outlet names like "LRT"
    cand.sources = [s for s in cand.sources if s.startswith("http")]
    if not cand.sources:
        return None, "no grounding source URLs"

    return cand, None
