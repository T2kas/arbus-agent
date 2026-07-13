"""Offline tests for the deterministic validation gates."""

from datetime import date

from arbus import validate
from arbus.schemas import Candidate

TODAY = date(2026, 7, 13)


def make(**overrides) -> Candidate:
    base = dict(
        question_lt="Ar Žalgiris liepos 20 d. laimės rungtynes prieš Rytą?",
        market_type="binary",
        options_lt=["Taip", "Ne"],
        probabilities=[0.6, 0.4],
        category="sports",
        resolve_by="2026-07-20",
        duration_class="medium",
        resolution_hint_lt="Pagal oficialų LKL rungtynių rezultatą.",
        sources=["https://www.basketnews.lt/example"],
        rationale_en="Big rivalry game this week.",
    )
    base.update(overrides)
    return Candidate(**base)


def test_valid_candidate_passes():
    fixed, reason = validate.validate_candidate(make(), TODAY)
    assert reason is None
    assert fixed.duration_class == "medium"


def test_gambling_language_rejected():
    cand = make(question_lt="Ar lažybų koeficientas kris?")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "gambling" in reason


def test_bare_bet_conjunction_is_allowed():
    # "bet" = "but" in Lithuanian; must NOT trip the linter
    assert validate.lint_gambling("Komanda pralaimėjo, bet kovojo iki galo") == []


def test_english_question_rejected():
    cand = make(question_lt="Will the team win the game?")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "Lithuanian" in reason


def test_past_resolve_date_rejected():
    cand = make(resolve_by="2026-07-10")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "future" in reason


def test_duration_reclassified_from_date():
    cand = make(resolve_by="2026-07-14", duration_class="long")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None and fixed.duration_class == "short"


def test_probabilities_normalized():
    cand = make(probabilities=[0.99, 0.11])
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None
    assert abs(sum(fixed.probabilities) - 1.0) < 0.01
    assert all(0.02 <= p <= 0.98 for p in fixed.probabilities)


def test_multi_needs_3_to_6_options():
    cand = make(
        market_type="multi",
        options_lt=["A", "B"],
        probabilities=[0.5, 0.5],
    )
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "3-6" in reason


def test_vague_headline_rejected():
    for bad in [
        "Ar Vyriausybė priims panašaus lygio sprendimą?",
        "Ar artimiausią savaitę bus karšta?",
        "Ar bus paskelbtas sprendimas (pvz., naujas ambasadorius)?",
    ]:
        fixed, reason = validate.validate_candidate(make(question_lt=bad), TODAY)
        assert fixed is None and "vague" in reason, bad


def test_non_url_sources_rejected():
    cand = make(sources=["LRT", "Lrytas"])
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "source" in reason


def test_mixed_sources_filtered_to_urls():
    cand = make(sources=["LRT", "https://www.lrt.lt/x"])
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None and fixed.sources == ["https://www.lrt.lt/x"]


def test_duplicate_detected_despite_wording():
    existing = ["Ar Žalgiris liepos 20 d. laimės LKL rungtynes prieš Rytą?"]
    assert validate.is_duplicate("Ar rungtynes prieš Rytą liepos 20 d. laimės Žalgiris?", existing)
    assert validate.is_duplicate("Ar Vilniuje liepą bus 35 laipsniai karščio?", existing) is None
