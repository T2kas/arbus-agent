"""Claude API helpers: web-search research calls and structured-output calls.

Two-phase design on purpose:
  - research(): Claude + server-side web search, returns free text. Web-search
    responses carry citations, which are incompatible with structured outputs,
    so this call never constrains the format.
  - structure(): no tools, converts research text into a validated Pydantic
    object via client.messages.parse().
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Type, TypeVar

import anthropic
from pydantic import BaseModel

from . import config

log = logging.getLogger(__name__)

_client: anthropic.Anthropic | None = None

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

T = TypeVar("T", bound=BaseModel)


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def load_prompt(name: str, **kwargs: str) -> str:
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    return text.format(**kwargs) if kwargs else text


def _web_search_tool(max_uses: int) -> dict:
    return {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": max_uses,
        "user_location": {
            "type": "approximate",
            "country": "LT",
            "city": "Vilnius",
            "timezone": "Europe/Vilnius",
        },
    }


def research(user_prompt: str, system: str, max_uses: int = 12, max_tokens: int = 32000) -> str:
    """Run a web-search-enabled research turn; return the concatenated text output."""
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    for attempt in range(6):
        with client().messages.stream(
            model=config.MODEL,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            tools=[_web_search_tool(max_uses)],
            messages=messages,
        ) as stream:
            resp = stream.get_final_message()

        if resp.stop_reason == "pause_turn":
            # Server-side tool loop hit its iteration limit — resume where it left off.
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            log.info("research: pause_turn, resuming (attempt %d)", attempt + 1)
            continue
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude refused the research request")
        if resp.stop_reason == "max_tokens":
            log.warning("research: hit max_tokens; output may be truncated")

        return "".join(b.text for b in resp.content if b.type == "text")

    raise RuntimeError("research: too many pause_turn continuations")


def structure(text: str, output_model: Type[T], system: str | None = None, max_tokens: int = 16000) -> T:
    """Convert free text into a validated instance of `output_model`."""
    resp = client().messages.parse(
        model=config.MODEL,
        max_tokens=max_tokens,
        system=system or anthropic.NOT_GIVEN,
        messages=[{"role": "user", "content": text}],
        output_format=output_model,
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"structure: no parsed output (stop_reason={resp.stop_reason})")
    return resp.parsed_output
