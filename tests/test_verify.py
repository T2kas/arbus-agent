"""Offline tests for verdict parsing and verification robustness."""

from datetime import date

from arbus import verify
from arbus.schemas import Candidate

TODAY = date(2026, 7, 24)


def _cand(q: str) -> Candidate:
    return Candidate(
        question_lt=q, market_type="binary", options_lt=["Taip", "Ne"],
        probabilities=[0.5, 0.5], category="sports", resolve_by="2026-09-01",
        duration_class="long", resolution_hint_lt="Pagal oficialius rezultatus.",
        sources=["https://example.lt/a"], rationale_en="test",
    )


def test_prompt_includes_candidate_sources():
    """Without the sources the checker can contradict the market's own article."""
    prompt = verify._verify_prompt([_cand("Ar X liks lyderiu?")], TODAY)
    assert "https://example.lt/a" in prompt
    assert "READ THE SOURCES" in prompt
    assert "NEWEST reporting" in prompt


def test_prompt_includes_live_facts_when_given():
    prompt = verify._verify_prompt([_cand("Ar akcijos pasieks 24 €?")], TODAY,
                                   live_facts="- Ignitis grupė: 22,45 € · savaitė +1,2 %")
    assert "22,45 €" in prompt
    assert "do NOT answer UNCLEAR about a value listed here" in prompt
    # the live-data block must come before the verdict instructions
    assert prompt.index("22,45") < prompt.index("DECIDED")


def test_prompt_has_no_live_block_by_default():
    prompt = verify._verify_prompt([_cand("Ar X?")], TODAY)
    assert "LIVE DATA" not in prompt


def test_parses_plain_and_decorated_lines():
    text = (
        "1: OPEN — genuinely undecided\n"
        "**2**. DECIDED — already happened per LRT\n"
        "3 - WRONG: player already signed elsewhere\n"
        "4: UNCLEAR — sources conflict\n"
    )
    out = verify._parse_verdicts(text, 4)
    assert out[1][0] == "OPEN"
    assert out[2][0] == "DECIDED"
    assert out[3][0] == "WRONG"
    assert out[4][0] == "UNCLEAR"


def test_unparsed_items_become_not_verified(monkeypatch):
    # Model returns prose with no verdict lines, twice — must not be judged.
    monkeypatch.setattr(verify, "_ask", lambda *a, **k: "I could not complete this task.")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "x")
    out = verify.verify_candidates([_cand("Ar A laimės?"), _cand("Ar B laimės?")], TODAY)
    assert [v for v, _ in out] == ["NOT_VERIFIED", "NOT_VERIFIED"]
    assert "technical" in out[0][1]


def test_strict_retry_recovers_missing_verdicts(monkeypatch):
    calls = {"n": 0}

    def fake_ask(prompt, use_perplexity):
        calls["n"] += 1
        if calls["n"] == 1:
            return "1: OPEN — fine"          # only 1 of 2 parsed
        return "1: OPEN — fine\n2: DECIDED — already resolved"

    monkeypatch.setattr(verify, "_ask", fake_ask)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "x")
    out = verify.verify_candidates([_cand("Ar A?"), _cand("Ar B?")], TODAY)
    assert calls["n"] == 2                    # retried
    assert [v for v, _ in out] == ["OPEN", "DECIDED"]


def test_exception_does_not_crash_batch(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(verify, "_ask", boom)
    monkeypatch.setenv("PERPLEXITY_API_KEY", "x")
    out = verify.verify_candidates([_cand("Ar A?")], TODAY)
    assert out[0][0] == "NOT_VERIFIED"
