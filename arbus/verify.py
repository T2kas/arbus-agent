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
import re
from datetime import date

from . import config, llm
from .schemas import Candidate

log = logging.getLogger(__name__)

VERDICT_RE = re.compile(r"^\s*\**(\d+)\**\s*[:.\-]\s*\**(OPEN|DECIDED|WRONG|UNCLEAR)\b\**\s*[-—:]?\s*(.*)$", re.M)
# Claude often answers in a two-line shape the single-line regex misses:
#   **1. Ukraina Rafale naikintuvai iki 2026 pabaigos**
#   WRONG — Sources confirm this timeline is unrealistic...
# A numbered title line opens an item block; the verdict word appears somewhere
# inside the block. Parsing this natively saves the paid strict-retry call.
ITEM_HEAD_RE = re.compile(r"^\s*\**\s*(\d+)[.):]", re.M)
VERDICT_WORD_RE = re.compile(r"\b(OPEN|DECIDED|WRONG|UNCLEAR)\b\s*[-—:]?\s*(.*)")

SYSTEM = (
    "You are a fact-checker for a Lithuanian prediction-market app. "
    "Verify each item against live web information. Be strict: "
    "if the event already happened or the outcome is publicly known, the verdict is "
    "DECIDED. If the market's setup contradicts the facts, the verdict is WRONG. "
    "If sources conflict or you cannot verify, say UNCLEAR — never guess."
)


def _verify_prompt(cands: list[Candidate], today: date, live_facts: str = "") -> str:
    lines = [
        f"Today is {today.isoformat()}. For each numbered prediction-market question below, "
        "check the live web and decide:\n",
    ]
    if live_facts:
        lines += [
            "LIVE DATA fetched from primary sources THIS SESSION — treat these as the "
            "current authoritative values; do NOT answer UNCLEAR about a value listed "
            "here, and judge plausibility against it:",
            live_facts,
            "",
        ]
    lines += [
        "- DECIDED: the outcome is already known / the event already happened or was cancelled.",
        "- WRONG: the setup contradicts facts — e.g. the resolution date does not match the "
        "actual event schedule: ALWAYS look up the event's real date; if the event happens "
        "before the market's resolve date (a sold-out market for an event taking place "
        "today, resolving weeks later), the verdict is WRONG. Also WRONG when the listed "
        "options are not "
        "factually valid or exhaustive as of today (teams already eliminated, wrong "
        "participants, misnamed entities), or the premise is false. For social-metric "
        "markets (followers, subscribers, views), look up the CURRENT metric value: if the "
        "target is implausible relative to it (e.g. 100k followers by autumn for an account "
        "at 7k today) or the entity is niche/unknown to a mass audience, the verdict is WRONG.",
        "- WRONG also when the market has NO Lithuanian connection: this app is for a "
        "Lithuanian audience, so a market about a foreign athlete, club or company with "
        "no Lithuanian participant, team or direct consequence for Lithuania is invalid "
        "no matter how famous the subject is (e.g. how many points a US player scores "
        "for a US club is WRONG; the same player joining a Lithuanian club is fine).",
        "- OPEN: genuinely undecided and the setup is factually sound.",
        "- UNCLEAR: cannot verify.\n",
        "MANDATORY: READ THE SOURCES listed under each item BEFORE judging it, and take "
        "the NEWEST reporting as the truth. A market is DECIDED whenever its own source "
        "already reports the outcome — e.g. an article headlined 'X elected permanent "
        "leader' means a market asking whether X will stay leader is DECIDED, not OPEN. "
        "Never contradict the item's own source based on older knowledge; check each "
        "article's publication date and prefer the most recent one.",
        "MANDATORY FIRST STEP for every item: search the CURRENT status of each named "
        "subject before judging — which club/team an athlete signed with most recently, "
        "whether a company still operates or has already closed/exited the market, "
        "whether an official already took or left a post, whether an event already took "
        "place. Recent transfers, closures and resignations are the most common way a "
        "market is born already dead: if a player has just signed elsewhere, a market "
        "about him joining another club is WRONG, and if the shops in question have "
        "already shut, a market asking whether they will shut is DECIDED.",
        "Do NOT answer UNCLEAR merely because the future outcome is unknown — that is "
        "what OPEN means. UNCLEAR is only for when you could not establish the CURRENT "
        "facts. Prefer a decisive verdict whenever a search settles the present state.\n",
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
        # The candidate's own sources are the fastest way to catch a market that
        # is already decided — without them the checker searches blind and can
        # contradict the very article the market was built from.
        if c.sources:
            lines.append(f"   sources: {' | '.join(c.sources[:3])}")
    return "\n".join(lines)


def _parse_verdicts(text: str, n: int) -> dict[int, tuple[str, str]]:
    out: dict[int, tuple[str, str]] = {}
    for m in VERDICT_RE.finditer(text):
        idx = int(m.group(1))
        if 1 <= idx <= n:
            out[idx] = (m.group(2), m.group(3).strip())
    if len(out) == n:
        return out
    # Second pass: block format — numbered title line, verdict on a later line.
    # Only plausible item numbers count as block heads, so a wrapped line that
    # happens to start with a year ("2026.") cannot split a block.
    heads = [m for m in ITEM_HEAD_RE.finditer(text) if 1 <= int(m.group(1)) <= n]
    for i, head in enumerate(heads):
        idx = int(head.group(1))
        if idx in out:
            continue
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        vm = VERDICT_WORD_RE.search(text[head.end():end])
        if vm:
            out[idx] = (vm.group(1), vm.group(2).strip())
    return out


def _ask(prompt: str, _unused: bool = False) -> str:
    """Route verification through the configured provider.

    This used to branch on whether PERPLEXITY_API_KEY existed, which silently
    verified via Perplexity even when LLM_PROVIDER selected another backend —
    so a provider comparison never actually compared the verification stage.
    """
    return llm.research(prompt, system=SYSTEM, max_uses=config.SEARCH_MAX_USES_VERIFY,
                        max_tokens=8000, stage="verify")


def verify_candidates(cands: list[Candidate], today: date,
                      live_facts: str = "") -> list[tuple[str, str]]:
    """Return one (verdict, note) per candidate, in order.

    A silent parse failure used to look identical to a genuine "cannot verify",
    which made a broken verification stage indistinguishable from an honestly
    uncertain one. Now unparsed items get one strict retry and, if that also
    fails, are labelled NOT_VERIFIED so the report shows a technical problem
    rather than an editorial verdict.
    """
    results: list[tuple[str, str]] = []
    use_perplexity = llm.provider("verify") == "perplexity"

    for start in range(0, len(cands), config.VERIFY_CHUNK_SIZE):
        chunk = cands[start : start + config.VERIFY_CHUNK_SIZE]
        prompt = _verify_prompt(chunk, today, live_facts)
        verdicts: dict[int, tuple[str, str]] = {}
        try:
            text = _ask(prompt, use_perplexity)
            verdicts = _parse_verdicts(text, len(chunk))
            if len(verdicts) < len(chunk):
                log.warning("verification: parsed %d/%d verdicts; retrying strictly. "
                            "Raw head: %s", len(verdicts), len(chunk), text[:300])
                strict = (
                    prompt
                    + "\n\nIMPORTANT: output ONLY the verdict lines, one per item, "
                      "numbered 1.." + str(len(chunk)) + ", each starting with the "
                      "number then a colon then one of OPEN/DECIDED/WRONG/UNCLEAR. "
                      "No preamble, no summary, no extra prose."
                )
                retry = _parse_verdicts(_ask(strict, use_perplexity), len(chunk))
                verdicts = {**retry, **verdicts}  # keep the first pass where it parsed
        except Exception as exc:
            log.warning("verification chunk failed: %s", exc)
        missing = [i for i in range(1, len(chunk) + 1) if i not in verdicts]
        if missing:
            log.error("verification produced no verdict for %d item(s) in this chunk — "
                      "these are flagged NOT_VERIFIED, not judged", len(missing))
        for i in range(1, len(chunk) + 1):
            results.append(verdicts.get(i, ("NOT_VERIFIED", "verification stage failed "
                                                            "(technical, not an editorial verdict)")))
    return results
