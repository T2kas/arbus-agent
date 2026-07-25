"""Pydantic schemas — these double as the structured-output contracts for Claude."""

from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


class Candidate(BaseModel):
    """Quick-mode market candidate (one line + rough probability)."""

    question_lt: str = Field(description="Market question in Lithuanian, e.g. 'Ar ...?'")
    market_type: Literal["binary", "multi"]
    options_lt: List[str] = Field(
        description='Outcome options in Lithuanian. Binary markets must be exactly ["Taip", "Ne"].'
    )
    probabilities: List[float] = Field(
        description="Rough probability estimate per option, same order as options_lt, summing to ~1.0"
    )
    category: str = Field(
        description="Loose category slug in English: sports, politics, culture, influencers, economics, weather, other"
    )
    resolve_by: str = Field(description="Latest resolution date, ISO format YYYY-MM-DD")
    duration_class: Literal["short", "medium", "long"]
    resolution_hint_lt: str = Field(
        description="One sentence in Lithuanian: what observable event decides this market"
    )
    sources: List[str] = Field(description="URLs of the news articles this candidate is grounded in")
    rationale_en: str = Field(
        description="Internal note in English: why 16-35yo Lithuanians would argue about this right now"
    )
    # Filled in deterministically after validation (arbus/images.py), never by
    # the model — a hallucinated image URL is worse than no image.
    image_url: str = ""
    image_source: str = ""


class CandidateBatch(BaseModel):
    candidates: List[Candidate]


class FullSpec(BaseModel):
    """Full-mode spec, generated only for candidates selected to launch."""

    question_lt: str
    trigger_sentence_lt: str = Field(
        description="Precise, unambiguous trigger sentence in Lithuanian defining exactly when the market resolves Taip"
    )
    definitions_lt: List[str] = Field(
        description="Definitions of every ambiguous term used in the question, in Lithuanian"
    )
    edge_cases_lt: List[str] = Field(
        description="Named edge cases and how each resolves, in Lithuanian"
    )
    primary_source: str = Field(description="URL/name of the primary resolution source")
    backup_source: str = Field(
        description="Independent backup source, not owned by the same outlet as the primary"
    )
    early_resolution_logic_lt: str = Field(
        description="Early/step-wise resolution logic in Lithuanian, or 'Netaikoma' if not applicable"
    )
    freeze_time_hint: str = Field(
        description="When trading should freeze (ISO datetime or rule, e.g. 'rungtynių pradžia')"
    )
