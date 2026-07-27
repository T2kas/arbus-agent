"""Offline tests for provider selection and the Z.AI backend."""

import pytest
import requests

from arbus import config, llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY", "ZAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_provider_detects_zai_key(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "k")
    assert llm.provider() == "zai"


def test_llm_provider_override_wins(monkeypatch):
    monkeypatch.setenv("PERPLEXITY_API_KEY", "p")
    monkeypatch.setenv("LLM_PROVIDER", "zai")
    assert llm.provider() == "zai"


def test_per_stage_provider_overrides_global(monkeypatch):
    """The cheap-draft / sharp-verify split that keeps a batch affordable."""
    monkeypatch.setenv("PERPLEXITY_API_KEY", "p")
    monkeypatch.setenv("LLM_PROVIDER", "perplexity")
    monkeypatch.setenv("LLM_PROVIDER_VERIFY", "anthropic")
    assert llm.provider("draft") == "perplexity"
    assert llm.provider("verify") == "anthropic"
    assert llm.provider() == "perplexity"


def test_unknown_stage_value_falls_back(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "perplexity")
    monkeypatch.setenv("LLM_PROVIDER_VERIFY", "nonsense")
    assert llm.provider("verify") == "perplexity"


def test_provider_without_any_key_explains_all_options():
    with pytest.raises(RuntimeError, match="ZAI_API_KEY"):
        llm.provider()


def test_web_search_tool_localizes_by_city_never_by_bad_country(monkeypatch):
    # "LT" is not an accepted country code, but city/timezone are free-form and
    # localize the search to Lithuania — the fix for missed LRT/Delfi results.
    monkeypatch.setattr(llm.config, "ANTHROPIC_SEARCH_CITY", "Vilnius")
    monkeypatch.setattr(llm.config, "ANTHROPIC_SEARCH_REGION", "Vilnius")
    monkeypatch.setattr(llm.config, "ANTHROPIC_SEARCH_COUNTRY", "")
    monkeypatch.setattr(llm.config, "ANTHROPIC_SEARCH_TIMEZONE", "Europe/Vilnius")
    loc = llm._web_search_tool(10)["user_location"]
    assert loc["city"] == "Vilnius" and "country" not in loc
    assert loc["timezone"] == "Europe/Vilnius"


def test_web_search_tool_adds_country_only_when_supported(monkeypatch):
    monkeypatch.setattr(llm.config, "ANTHROPIC_SEARCH_COUNTRY", "US")
    assert llm._web_search_tool(10)["user_location"]["country"] == "US"


def test_web_search_tool_location_suppressed_on_retry(monkeypatch):
    monkeypatch.setattr(llm.config, "ANTHROPIC_SEARCH_CITY", "Vilnius")
    monkeypatch.setattr(llm.config, "ANTHROPIC_SEARCH_COUNTRY", "US")
    assert "user_location" not in llm._web_search_tool(10, with_location=False)


def test_json_objects_splits_concatenated_output():
    text = '{"a": 1}\n{"a": {"nested": "}"}}\n'
    assert llm._json_objects(text) == ['{"a": 1}', '{"a": {"nested": "}"}}']


def test_validate_flexible_accepts_proper_container():
    from arbus.schemas import CandidateBatch
    payload = '{"candidates": []}'
    assert llm._validate_flexible(payload, CandidateBatch).candidates == []


def test_validate_flexible_wraps_bare_objects():
    """GLM emits one bare candidate per object instead of the batch container."""
    from arbus.schemas import CandidateBatch
    item = (
        '{"question_lt": "Ar Žalgiris laimės?", "market_type": "binary",'
        ' "options_lt": ["Taip", "Ne"], "probabilities": [0.6, 0.4],'
        ' "category": "sports", "resolve_by": "2026-09-01",'
        ' "duration_class": "long", "resolution_hint_lt": "Pagal LKL.",'
        ' "sources": ["https://x.lt/a"], "rationale_en": "test"}'
    )
    out = llm._validate_flexible(f"```json\n{item}\n{item}\n```", CandidateBatch)
    assert len(out.candidates) == 2
    assert out.candidates[0].question_lt == "Ar Žalgiris laimės?"


def test_validate_flexible_salvages_truncated_container():
    """max_tokens cut the response mid-array — keep the complete candidates."""
    from arbus.schemas import CandidateBatch
    item = (
        '{"question_lt": "Ar Žalgiris laimės?", "market_type": "binary",'
        ' "options_lt": ["Taip", "Ne"], "probabilities": [0.6, 0.4],'
        ' "category": "sports", "resolve_by": "2026-09-01",'
        ' "duration_class": "long", "resolution_hint_lt": "Pagal LKL.",'
        ' "sources": ["https://x.lt/a"], "rationale_en": "test"}'
    )
    truncated = '{"candidates": [' + item + ", " + item + ', {"question_lt": "Ar L'
    out = llm._validate_flexible(truncated, CandidateBatch)
    assert len(out.candidates) == 2  # the two complete ones survive


def test_json_objects_skips_unclosed_outer_brace():
    objs = llm._json_objects('{"candidates": [{"a": 1}, {"b": 2}')
    assert objs == ['{"a": 1}', '{"b": 2}']


def test_validate_flexible_raises_when_nothing_usable():
    from arbus.schemas import CandidateBatch
    with pytest.raises(ValueError):
        llm._validate_flexible("no json here", CandidateBatch)


class _Resp:
    def __init__(self, status=200, content="ok"):
        self.status_code = status
        self._content = content

    def json(self):
        return {"choices": [{"message": {"content": self._content}}]}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")


def test_zai_sends_web_search_tool(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "k")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(json)
        return _Resp()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    llm.zai_chat("hi", web_search=True)
    assert seen["tools"][0]["type"] == "web_search"


def test_zai_retries_without_tools_when_rejected(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "k")
    calls = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append(dict(json))
        # first call (with tools) is rejected, second succeeds
        return _Resp(400) if "tools" in json else _Resp(content="fallback")

    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.zai_chat("hi", web_search=True)
    assert out == "fallback"
    assert len(calls) == 2 and "tools" not in calls[1]


def test_zai_error_without_tools_still_raises(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "k")
    monkeypatch.setattr(llm.requests, "post",
                        lambda *a, **k: _Resp(401))
    with pytest.raises(requests.HTTPError):
        llm.zai_chat("hi", web_search=False)


def test_aicheck_uses_the_stronger_model_by_default(monkeypatch):
    """Resolution checks decide payouts that cannot be clawed back, so they get
    the accurate model even though drafting runs on the cheap one."""
    seen = {}

    def fake(prompt, system, max_uses, max_tokens, model=None):
        seen["model"] = model
        return "ok"

    monkeypatch.setattr(llm, "_research_anthropic", fake)
    monkeypatch.setattr(llm, "provider", lambda stage=None: "anthropic")

    llm.research("p", system="s", stage="aicheck")
    assert seen["model"] == config.AICHECK_MODEL == "claude-opus-5"

    llm.research("p", system="s", stage="draft")
    assert seen["model"] is None          # drafting stays on the cheap default
