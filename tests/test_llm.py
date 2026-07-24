"""Offline tests for provider selection and the Z.AI backend."""

import pytest
import requests

from arbus import llm


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


def test_provider_without_any_key_explains_all_options():
    with pytest.raises(RuntimeError, match="ZAI_API_KEY"):
        llm.provider()


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
