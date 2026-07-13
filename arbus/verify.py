"""Stage 5 — 'already decided?' verification against the live web.

Uses Perplexity Sonar when PERPLEXITY_API_KEY is set (cited fact-checking,
cheap), otherwise falls back to Claude + web search. Candidates are checked
in chunks to bound cost.

Verdicts:
  OPEN     — outcome genuinely undecided as of now  -> keep
  DECIDED  — already resolved / event already happened -> reject
  UNCLEAR  — could not verify / sources conflict     -> keep but flag
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date

import requests

from . import config, llm
from .schemas import Candidate

log = logging.getLogger(__name__)

VERDICT_RE = re.compile(r"^\s*(\d+)\s*[:.\-]\s*(OPEN|DECIDED|UNCLEAR)\b\s*[-—:]?\s*(.*)$", re.M)


def _verify_prompt(cands: list[Candidate], today: date) -> str:
    lines = [
        f"Today is {today.isoformat()}. For each numbered prediction-market question below, "
        "check the live web and decide whether its outcome is still genuinely open, already "
        "decided (event happened / result known / event cancelled), or unverifiable.\n",
        "Answer with EXACTLY one line per item, format:",
        "N: OPEN|DECIDED|UNCLEAR — short reason with source\n",
    ]
    for i, c in enumerate(cands, 1):
        lines.append(f"{i}. {c.question_lt} (resolution: {c.resolution_hint_lt}; resolves by {c.resolve_by})")
    return "\n".join(lines)


def _parse_verdicts(text: str, n: int) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for m in VERDICT_RE.finditer(text):
        idx = int(m.group(1))
        if 1 <= idx <= n:
            out[idx] = (m.group(2), m.group(3).strip())
    return out


def _via_perplexity(prompt: str) -> str:
    resp = requests.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['PERPLEXITY_API_KEY']}"},
        json={
            "model": config.PERPLEXITY_MODEL,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=180,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def _via_claude(prompt: str) -> str:
    system = (
        "You are a fact-checker for a Lithuanian prediction-market app. "
        "Use web search to verify current status. Be strict: if the event already "
        "happened or the outcome is publicly known, the verdict is DECIDED. "
        "If sources conflict or you cannot verify, say UNCLEAR — never guess."
    )
    return llm.research(prompt, system=system, max_uses=10, max_tokens=8000)


def verify_candidates(cands: list[Candidate], today: date) -> list[tuple[str, str]]:
    """Return one (verdict, note) per candidate, in order."""
    results: list[tuple[str, str]] = []
    use_perplexity = bool(os.environ.get("PERPLEXITY_API_KEY"))

    for start in range(0, len(cands), config.VERIFY_CHUNK_SIZE):
        chunk = cands[start : start + config.VERIFY_CHUNK_SIZE]
        prompt = _verify_prompt(chunk, today)
        try:
            text = _via_perplexity(prompt) if use_perplexity else _via_claude(prompt)
            verdicts = _parse_verdicts(text, len(chunk))
        except Exception as exc:
            log.warning("verification chunk failed: %s", exc)
            verdicts = {}
        for i in range(1, len(chunk) + 1):
            results.append(verdicts.get(i, ("UNCLEAR", "verification unavailable")))
    return results
