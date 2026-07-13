"""Stage 5 — 'already decided?' verification against the live web.

Uses Perplexity Sonar when PERPLEXITY_API_KEY is set (search-native, cited),
otherwise Claude + web search. Candidates are checked in chunks to bound cost.

Verdicts:
  OPEN     — outcome genuinely undecided, setup factually sound -> keep
  DECIDED  — already resolved / event already happened          -> reject
  WRONG    — market setup contradicts facts (bad date/options)  -> reject
  UNCLEAR  — could not verify / sources conflict                -> keep but flag
"""

from __future__ import annotations

import logging
import os
import re
from datetime import date

from . import config, llm
from .schemas import Candidate

log = logging.getLogger(__name__)

VERDICT_RE = re.compile(r"^\s*\**(\d+)\**\s*[:.\-]\s*\**(OPEN|DECIDED|WRONG|UNCLEAR)\b\**\s*[-—:]?\s*(.*)$", re.M)

SYSTEM = (
    "You are a fact-checker for a Lithuanian prediction-market app. "
    "Verify each item against live web information. Be strict: "
    "if the event already happened or the outcome is publicly known, the verdict is "
    "DECIDED. If the market's setup contradicts the facts, the verdict is WRONG. "
    "If sources conflict or you cannot verify, say UNCLEAR — never guess."
)


def _verify_prompt(cands: list[Candidate], today: date) -> str:
    lines = [
        f"Today is {today.isoformat()}. For each numbered prediction-market question below, "
        "check the live web and decide:\n",
        "- DECIDED: the outcome is already known / the event already happened or was cancelled.",
        "- WRONG: the setup contradicts facts — e.g. the resolution date does not match the "
        "actual event schedule (event ends earlier or later), the listed options are not "
        "factually valid or exhaustive as of today (teams already eliminated, wrong "
        "participants, misnamed entities), or the premise is false.",
        "- OPEN: genuinely undecided and the setup is factually sound.",
        "- UNCLEAR: cannot verify.\n",
        "Answer with EXACTLY one line per item, format:",
        "N: OPEN|DECIDED|WRONG|UNCLEAR — short reason with source\n",
    ]
    for i, c in enumerate(cands, 1):
        opts = ""
        if c.market_type == "multi":
            opts = f"; options: {' / '.join(c.options_lt)}"
        lines.append(
            f"{i}. {c.question_lt} (resolution: {c.resolution_hint_lt}; "
            f"resolves by {c.resolve_by}{opts})"
        )
    return "\n".join(lines)


def _parse_verdicts(text: str, n: int) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for m in VERDICT_RE.finditer(text):
        idx = int(m.group(1))
        if 1 <= idx <= n:
            out[idx] = (m.group(2), m.group(3).strip())
    return out


def verify_candidates(cands: list[Candidate], today: date) -> list[tuple[str, str]]:
    """Return one (verdict, note) per candidate, in order."""
    results: list[tuple[str, str]] = []
    use_perplexity = bool(os.environ.get("PERPLEXITY_API_KEY"))

    for start in range(0, len(cands), config.VERIFY_CHUNK_SIZE):
        chunk = cands[start : start + config.VERIFY_CHUNK_SIZE]
        prompt = _verify_prompt(chunk, today)
        try:
            if use_perplexity:
                text = llm.perplexity_chat(prompt, system=SYSTEM, max_tokens=4000)
            else:
                text = llm.research(prompt, system=SYSTEM, max_uses=10, max_tokens=8000)
            verdicts = _parse_verdicts(text, len(chunk))
        except Exception as exc:
            log.warning("verification chunk failed: %s", exc)
            verdicts = {}
        for i in range(1, len(chunk) + 1):
            results.append(verdicts.get(i, ("UNCLEAR", "verification unavailable")))
    return results
