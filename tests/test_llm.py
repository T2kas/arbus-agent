"""Offline tests for provider selection and the Z.AI backend."""

import pytest
import requests

from arbus import config, llm


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for var in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "PERPLEXITY_API_KEY",
                "ZAI_API_KEY", "OPENROUTER_API_KEY", "OPENAI_API_KEY"):
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


def test_aicheck_uses_a_perplexity_reasoning_model(monkeypatch):
    """Resolution needs reasoning (wrong-year/namesake errors); drafting stays
    on the cheaper search model."""
    seen = {}

    def fake_px(user, system=None, model=None, max_tokens=8000, **k):
        seen["model"] = model
        return "ok"

    monkeypatch.setattr(llm, "perplexity_chat", fake_px)
    monkeypatch.setattr(llm, "provider", lambda stage=None: "perplexity")

    llm.research("p", system="s", stage="aicheck")
    assert seen["model"] == config.PERPLEXITY_AICHECK_MODEL == "sonar-reasoning-pro"

    llm.research("p", system="s", stage="draft")
    assert seen["model"] == config.PERPLEXITY_MODEL      # cheaper, for drafting


def test_perplexity_strips_reasoning_think_block(monkeypatch):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"choices": [{"message": {"content":
                "<think>let me search and reason about this</think>\n"
                "REZULTATAS: žinomas\nSIŪLOMA BAIGTIS: Taip"}}]}

    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-x")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Resp())
    out = llm.perplexity_chat("q", model="sonar-reasoning-pro")
    assert "<think>" not in out and out.startswith("REZULTATAS")


# ── OpenRouter backend (OpenAI/DeepSeek/… + web search via one key) ─────────

def test_openrouter_adds_online_suffix_and_search_plugin(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-x")
    monkeypatch.setattr(llm.config, "OPENROUTER_SEARCH_ENGINE", "exa")
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url; seen["json"] = json; seen["headers"] = headers
        return _Resp(content="REZULTATAS: žinomas")

    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.openrouter_chat("q", model="openai/gpt-5", web_search=True)
    assert "openrouter.ai" in seen["url"]
    assert seen["json"]["model"] == "openai/gpt-5:online"      # web search on
    assert seen["json"]["plugins"][0]["engine"] == "exa"
    assert seen["headers"]["Authorization"] == "Bearer or-x"
    assert out == "REZULTATAS: žinomas"


def test_openrouter_strips_reasoning_block(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-x")
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: _Resp(
        content="<think>deepseek reasoning...</think>\nSIŪLOMA BAIGTIS: Taip"))
    out = llm.openrouter_chat("q", model="deepseek/deepseek-r1")
    assert "<think>" not in out and out.startswith("SIŪLOMA BAIGTIS")


def test_research_routes_aicheck_to_openrouter_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "openrouter_chat",
                        lambda *a, model=None, web_search=False, **k: seen.update(
                            model=model, web_search=web_search) or "ok")
    monkeypatch.setattr(llm, "provider", lambda stage=None: "openrouter")
    llm.research("p", system="s", stage="aicheck")
    assert seen["model"] == config.OPENROUTER_AICHECK_MODEL and seen["web_search"]


def test_provider_detects_openrouter_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-x")
    assert llm.provider() == "openrouter"
    assert "openrouter" in llm.available_providers()


# ── OpenAI backend (native key, Responses API + web search) ─────────────────

def test_openai_uses_responses_api_with_localized_web_search(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    monkeypatch.setattr(llm.config, "ANTHROPIC_SEARCH_CITY", "Vilnius")
    monkeypatch.setattr(llm.config, "OPENAI_SEARCH_COUNTRY", "LT")
    seen = {}

    class R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"output_text": "REZULTATAS: žinomas\nSIŪLOMA BAIGTIS: Taip"}

    def fake_post(url, headers=None, json=None, timeout=None):
        seen["url"] = url; seen["json"] = json; seen["headers"] = headers
        return R()

    monkeypatch.setattr(llm.requests, "post", fake_post)
    out = llm.openai_chat("q", system="s", model="gpt-5", max_tokens=1200,
                          web_search=True)
    assert seen["url"].endswith("/v1/responses")
    assert seen["json"]["tools"][0]["type"] == "web_search"
    assert seen["json"]["tools"][0]["user_location"]["country"] == "LT"
    assert seen["json"]["tools"][0]["user_location"]["city"] == "Vilnius"
    assert seen["json"]["max_output_tokens"] >= 4000        # reasoning floor
    assert seen["headers"]["Authorization"] == "Bearer sk-oa"
    assert out.startswith("REZULTATAS")


def test_openai_output_text_falls_back_to_output_array():
    data = {"output": [
        {"type": "reasoning", "content": []},
        {"type": "web_search_call"},
        {"type": "message", "content": [
            {"type": "output_text", "text": "SIŪLOMA BAIGTIS: Ne"}]},
    ]}
    assert llm._openai_output_text(data) == "SIŪLOMA BAIGTIS: Ne"


def test_research_routes_aicheck_to_openai_model(monkeypatch):
    seen = {}
    monkeypatch.setattr(llm, "openai_chat",
                        lambda *a, model=None, web_search=False, **k: seen.update(
                            model=model, web_search=web_search) or "ok")
    monkeypatch.setattr(llm, "provider", lambda stage=None: "openai")
    llm.research("p", system="s", stage="aicheck")
    assert seen["model"] == config.OPENAI_AICHECK_MODEL and seen["web_search"]


def test_provider_detects_openai_key(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    assert llm.provider() == "openai"
    assert "openai" in llm.available_providers()


def test_aicheck_target_shows_provider_and_model(monkeypatch):
    """The line that catches OPENAI_AICHECK_MODEL set but LLM_PROVIDER_AICHECK
    still perplexity — the provider decides, the model just names the variant."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx")
    monkeypatch.setenv("LLM_PROVIDER_AICHECK", "perplexity")   # the mismatch
    assert llm.aicheck_target() == ("perplexity", config.PERPLEXITY_AICHECK_MODEL)

    monkeypatch.setenv("LLM_PROVIDER_AICHECK", "openai")       # the fix
    assert llm.aicheck_target() == ("openai", config.OPENAI_AICHECK_MODEL)


def test_env_loader_strips_inline_comments_and_quotes():
    """The Sonnet 404: `ANTHROPIC_AICHECK_MODEL=claude-sonnet-5  # cheaper`
    became the model name, comment and all, and the API rejected it."""
    clean = config._clean_env_value
    assert clean("claude-sonnet-5   # → per pusę pigiau") == "claude-sonnet-5"
    assert clean("off          # → Opus 5") == "off"
    assert clean('"sk-ant-xyz"') == "sk-ant-xyz"          # surrounding quotes
    assert clean("sk-ant-xyz") == "sk-ant-xyz"            # untouched when clean
    # a '#' with no space before it (URL fragment) is kept
    assert clean("https://x.lt/a#frag") == "https://x.lt/a#frag"
