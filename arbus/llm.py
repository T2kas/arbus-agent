"""LLM backends: Perplexity Sonar (default when only that key is set) and
Claude (Anthropic API).

Two operations, provider-agnostic:
  - research(): web-grounded free-text generation. Perplexity models search
    natively; the Claude path uses the server-side web_search tool.
  - structure(): convert free text into a validated Pydantic object.
    Perplexity via response_format json_schema (with a plain-JSON fallback),
    Claude via client.messages.parse().
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Type, TypeVar

import requests
from pydantic import BaseModel, ValidationError

from . import config

log = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

T = TypeVar("T", bound=BaseModel)


def provider(stage: str | None = None) -> str:
    """Which backend runs a stage.

    Stages can be pointed at different providers, which is what makes a batch
    affordable: drafting is many long, search-heavy calls and is cheap on a
    search-native provider, while verification is the short judgement call worth
    paying a stronger model for. Set LLM_PROVIDER_DRAFT / LLM_PROVIDER_VERIFY to
    override LLM_PROVIDER for one stage.
    """
    if stage:
        per_stage = os.environ.get(f"LLM_PROVIDER_{stage.upper()}", "").strip().lower()
        if per_stage in ("anthropic", "perplexity", "zai"):
            return per_stage
    forced = os.environ.get("LLM_PROVIDER", "").strip().lower()
    if forced in ("anthropic", "perplexity", "zai"):
        return forced
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("PERPLEXITY_API_KEY"):
        return "perplexity"
    if os.environ.get("ZAI_API_KEY"):
        return "zai"
    raise RuntimeError(
        "Set PERPLEXITY_API_KEY, ANTHROPIC_API_KEY or ZAI_API_KEY "
        "(or LLM_PROVIDER + the matching key)"
    )


def load_prompt(name: str, **kwargs: str) -> str:
    text = (PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8")
    return text.format(**kwargs) if kwargs else text


# ── Perplexity backend ──────────────────────────────────────────────────────

def perplexity_chat(
    user: str,
    system: str | None = None,
    model: str = config.PERPLEXITY_MODEL,
    max_tokens: int = 8000,
    response_format: dict | None = None,
) -> str:
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": user}
    ]
    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if response_format:
        payload["response_format"] = response_format
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"},
        json=payload,
        timeout=600,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _extract_json(text: str) -> str:
    """Pull the outermost JSON object out of possibly-chatty model output."""
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return text[start : end + 1]


def _match_brace(text: str, start: int) -> int | None:
    """Index of the `}` closing the `{` at `start`, or None if never closed."""
    depth = 0
    in_str = esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def _json_objects(text: str) -> list[str]:
    """Every complete balanced {...} block, outermost first.

    Scanning at any depth is what makes truncated output salvageable: when a
    response is cut off mid-array the outer container never closes, so the
    outer brace is skipped and the complete candidate objects inside are
    recovered instead of losing the whole chunk.
    """
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.M)
    out: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == "{":
            end = _match_brace(text, i)
            if end is not None:
                out.append(text[i : end + 1])
                i = end + 1
                continue
        i += 1
    return out


def _list_field(output_model: Type[BaseModel]) -> str | None:
    """Name of the model's single list field, if it has exactly one."""
    names = [
        name for name, field in output_model.model_fields.items()
        if str(field.annotation).startswith(("list[", "typing.List["))
    ]
    return names[0] if len(names) == 1 else None


def _validate_flexible(content: str, output_model: Type[T]) -> T:
    """Validate model output, tolerating the shapes chat models actually emit.

    Some models (GLM notably) ignore the container and stream one bare object
    per item — "{...}\\n{...}" — which is not valid JSON as a whole and used to
    fail with "trailing characters". Accept that by collecting the top-level
    objects and wrapping them into the target model's list field.
    """
    objs = _json_objects(content)
    if not objs:
        raise ValueError("no JSON object found in model output")

    first_error: Exception | None = None
    try:
        return output_model.model_validate_json(objs[0])
    except ValidationError as exc:
        first_error = exc

    field = _list_field(output_model)
    if field:
        items = []
        for obj in objs:
            try:
                items.append(json.loads(obj))
            except json.JSONDecodeError:
                continue
        if items:
            log.info("model emitted %d bare objects; wrapping into %r", len(items), field)
            return output_model.model_validate({field: items})
    raise first_error


def _structure_openai_compatible(
    chat, model: str, text: str, output_model: Type[T], max_tokens: int
) -> T:
    """Structure free text with any OpenAI-compatible chat endpoint.

    Tries native schema-constrained decoding, falls back to plain JSON with the
    schema inlined, then gives the model one round to repair invalid output.
    Shared by the Perplexity and Z.AI backends.
    """
    schema = output_model.model_json_schema()
    rf = {"type": "json_schema", "json_schema": {"schema": schema}}
    try:
        content = chat(text, model=model, max_tokens=max_tokens, response_format=rf)
    except requests.HTTPError as exc:
        log.warning("json_schema mode failed (%s); falling back to plain JSON", exc)
        content = chat(
            text
            + "\n\nRespond with ONLY a JSON object (no prose, no code fences) matching this "
            + "JSON schema:\n" + json.dumps(schema),
            model=model, max_tokens=max_tokens,
        )
    try:
        return _validate_flexible(content, output_model)
    except (ValidationError, ValueError) as exc:
        # One repair round: show the model its own output and the error.
        log.warning("structure output invalid (%s); attempting one repair round", exc)
        repaired = chat(
            "Fix this JSON so it validates against the schema. Respond with ONLY the "
            f"corrected JSON object.\n\nSCHEMA:\n{json.dumps(schema)}\n\n"
            f"ERROR:\n{exc}\n\nJSON:\n{content}",
            model=model, max_tokens=max_tokens,
        )
        return _validate_flexible(repaired, output_model)


# ── Z.AI (Zhipu GLM) backend ────────────────────────────────────────────────

def zai_chat(
    user: str,
    system: str | None = None,
    model: str = config.ZAI_MODEL,
    max_tokens: int = 8000,
    response_format: dict | None = None,
    web_search: bool = False,
) -> str:
    """Call Z.AI's OpenAI-compatible endpoint.

    GLM has no built-in search, so web grounding comes from the server-side
    web_search tool. Older/other deployments reject that tool shape, so a 4xx
    with tools attached is retried once without them rather than failing the
    batch — the caller still gets an answer, just an ungrounded one.
    """
    messages = ([{"role": "system", "content": system}] if system else []) + [
        {"role": "user", "content": user}
    ]
    payload: dict = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if response_format:
        payload["response_format"] = response_format
    if web_search and config.ZAI_WEB_SEARCH:
        payload["tools"] = [{"type": "web_search",
                             "web_search": {"enable": True, "search_result": True}}]

    def _post(body: dict) -> requests.Response:
        return requests.post(
            config.ZAI_BASE_URL,
            headers={"Authorization": f"Bearer {os.environ['ZAI_API_KEY']}",
                     "Content-Type": "application/json"},
            json=body, timeout=600,
        )

    resp = _post(payload)
    if resp.status_code >= 400 and "tools" in payload:
        log.warning("z.ai rejected the web_search tool (%s); retrying WITHOUT web "
                    "grounding — answers may be from memory", resp.status_code)
        payload.pop("tools")
        resp = _post(payload)
    resp.raise_for_status()
    msg = resp.json()["choices"][0]["message"]
    # GLM returns None/"" in `content` when it answers as a reasoning trace or
    # asks for an unresolved tool call. Falling through with None used to blow
    # up the caller (verification then reported every item as NOT_VERIFIED), so
    # take the reasoning text when that is all there is, and say so loudly.
    content = msg.get("content") or msg.get("reasoning_content") or ""
    if not content:
        log.warning("z.ai returned an empty message (tool_calls=%s) — treating as "
                    "no answer", bool(msg.get("tool_calls")))
    return content


# ── Anthropic backend ───────────────────────────────────────────────────────

def _anthropic_client():
    import anthropic

    return anthropic.Anthropic()


def _web_search_tool(max_uses: int, with_location: bool = True) -> dict:
    """Anthropic's server-side web search.

    Localizing the search to Lithuania matters a lot for resolution: without it
    the tool returns US-centric results and misses LRT/Delfi pages that Google
    surfaces instantly (M.A.M.A., Eurovision LT results). `user_location` fields
    are all optional, and only the `country` field rejects unsupported codes
    ("Country code LT is not supported", which kills the batch) — so we localize
    with CITY and timezone, which Lithuania is free to use, and only add country
    when config names a supported one.
    """
    tool: dict = {
        "type": "web_search_20260209",
        "name": "web_search",
        "max_uses": max_uses,
    }
    loc: dict = {"type": "approximate"}
    if with_location and config.ANTHROPIC_SEARCH_CITY:
        loc["city"] = config.ANTHROPIC_SEARCH_CITY
    if with_location and config.ANTHROPIC_SEARCH_REGION:
        loc["region"] = config.ANTHROPIC_SEARCH_REGION
    if with_location and config.ANTHROPIC_SEARCH_COUNTRY:
        loc["country"] = config.ANTHROPIC_SEARCH_COUNTRY
    if with_location and config.ANTHROPIC_SEARCH_TIMEZONE:
        loc["timezone"] = config.ANTHROPIC_SEARCH_TIMEZONE
    if len(loc) > 1:                       # more than just the type
        tool["user_location"] = loc
    return tool


def _research_anthropic(user_prompt: str, system: str, max_uses: int, max_tokens: int,
                        model: str | None = None) -> str:
    client = _anthropic_client()
    messages: list[dict] = [{"role": "user", "content": user_prompt}]
    with_location = True
    # The system prompt is identical across every chunk and every verification
    # call, so caching it turns repeated full-price input into cache reads.
    system_blocks = [{"type": "text", "text": system,
                      "cache_control": {"type": "ephemeral"}}]
    kwargs: dict = {}
    if config.ANTHROPIC_THINKING != "off":
        kwargs["thinking"] = {"type": config.ANTHROPIC_THINKING}
    for attempt in range(6):
        try:
            with client.messages.stream(
                model=model or config.MODEL,
                max_tokens=max_tokens,
                system=system_blocks,
                tools=[_web_search_tool(max_uses, with_location)],
                messages=messages,
                **kwargs,
            ) as stream:
                resp = stream.get_final_message()
        except Exception as exc:
            # An unsupported user_location country is a configuration problem,
            # not a reason to lose the batch — drop the hint and search globally.
            text = str(exc)
            if with_location and ("user_location" in text or "Country code" in text):
                log.warning("web search rejected the configured location (%s); "
                            "retrying without it", text[:120])
                with_location = False
                continue
            raise

        if resp.stop_reason == "pause_turn":
            messages = [messages[0], {"role": "assistant", "content": resp.content}]
            log.info("research: pause_turn, resuming (attempt %d)", attempt + 1)
            continue
        if resp.stop_reason == "refusal":
            raise RuntimeError("Claude refused the research request")
        if resp.stop_reason == "max_tokens":
            log.warning("research: hit max_tokens; output may be truncated")
        return "".join(b.text for b in resp.content if b.type == "text")

    raise RuntimeError("research: too many pause_turn continuations")


def _structure_anthropic(text: str, output_model: Type[T], max_tokens: int) -> T:
    client = _anthropic_client()
    resp = client.messages.parse(
        model=config.MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": text}],
        output_format=output_model,
    )
    if resp.parsed_output is None:
        raise RuntimeError(f"structure: no parsed output (stop_reason={resp.stop_reason})")
    return resp.parsed_output


# ── Public API ──────────────────────────────────────────────────────────────

def available_providers() -> list[str]:
    """Providers whose API key is actually configured, in preference order."""
    out = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        out.append("anthropic")
    if os.environ.get("PERPLEXITY_API_KEY"):
        out.append("perplexity")
    if os.environ.get("ZAI_API_KEY"):
        out.append("zai")
    return out


def research(user_prompt: str, system: str, max_uses: int = 12, max_tokens: int = 32000,
             stage: str | None = None, force_provider: str | None = None) -> str:
    """Web-grounded free-text generation. `force_provider` overrides the stage's
    configured provider (used by aicheck to fall back when the primary fails)."""
    prov = force_provider or provider(stage)
    if prov == "perplexity":
        return perplexity_chat(
            user_prompt, system=system, model=config.PERPLEXITY_MODEL, max_tokens=max_tokens
        )
    if prov == "zai":
        return zai_chat(user_prompt, system=system, model=config.ZAI_MODEL,
                        max_tokens=max_tokens, web_search=True)
    model = None
    if stage == "verify" and config.VERIFY_MODEL:
        model = config.VERIFY_MODEL
    elif stage == "aicheck" and config.AICHECK_MODEL:
        model = config.AICHECK_MODEL
    return _research_anthropic(user_prompt, system, max_uses, max_tokens, model=model)


def structure(text: str, output_model: Type[T], max_tokens: int = 16000) -> T:
    """Convert free text into a validated instance of `output_model`."""
    prov = provider()
    if prov == "perplexity":
        return _structure_openai_compatible(
            perplexity_chat, config.PERPLEXITY_STRUCTURE_MODEL, text, output_model, max_tokens
        )
    if prov == "zai":
        return _structure_openai_compatible(
            zai_chat, config.ZAI_STRUCTURE_MODEL, text, output_model,
            max(max_tokens, config.ZAI_STRUCTURE_MIN_TOKENS),
        )
    return _structure_anthropic(text, output_model, max_tokens)
