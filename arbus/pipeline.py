"""The generation pipeline as a callable — shared by the CLI and the Telegram bot."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from . import (config, feedback, harvest, images, llm, pulse, report, store,
               validate, verify)
from .schemas import Candidate, CandidateBatch

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class BatchResult:
    batch_id: str
    accepted: list[tuple[int, Candidate, str, str]] = field(default_factory=list)
    rejected: list[tuple[Candidate, str]] = field(default_factory=list)
    report_path: str = ""
    export_path: str = ""

    @property
    def needs_review(self) -> int:
        return sum(1 for _, _, v, _ in self.accepted if v in ("UNCLEAR", "NOT_VERIFIED"))


def _theme_chunks(count: int, chunk_size: int) -> list[tuple[int, str, str]]:
    """Allocate the batch across DRAFT_THEMES by share, split to chunk_size.

    Rounding remainders go to the FIRST theme rather than the last: the shares
    are ordered by priority, so a leftover slot should strengthen the
    informative backbone, never inflate the culture tail.
    Returns (n, label, mandate) triples in theme order.
    """
    themes = config.DRAFT_THEMES
    counts = [min(int(count * share), count) for _, share, _ in themes]
    counts[0] += count - sum(counts)          # remainder to the top priority

    chunks: list[tuple[int, str, str]] = []
    for (label, _, focus), n in zip(themes, counts):
        while n > 0:
            take = min(n, chunk_size)
            chunks.append((take, label, focus))
            n -= take
    return chunks


def run_batch(
    count: int = config.DEFAULT_BATCH_SIZE,
    skip_verify: bool = False,
    progress: Progress = lambda msg: log.info("%s", msg),
) -> BatchResult:
    today = date.today()
    batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
    result = BatchResult(batch_id=batch_id)

    min_resolve: date | None = None
    if config.MIN_RESOLVE_DATE:
        launch = date.fromisoformat(config.MIN_RESOLVE_DATE)
        if launch > today:
            min_resolve = launch
    if min_resolve:
        timing = (
            f"- CRITICAL: the app goes live on {min_resolve.isoformat()}. Every market MUST "
            f"resolve on {min_resolve.isoformat()} or later — never earlier — AND its "
            f"outcome must still be genuinely UNKNOWN on that date: the deciding event "
            f"itself must take place on {min_resolve.isoformat()} or later. NEVER draft a "
            "market about an event happening before launch, and NEVER push resolve_by "
            "later to smuggle one in — a market whose answer is already known at launch "
            "is dead on arrival. Spread the dates: most within 2-3 weeks after launch, "
            "~20% a month or more later."
        )
    else:
        timing = ("- Duration mix: ~30% resolving within 48 hours, ~50% within "
                  "days-to-weeks, ~20% one month or later.")

    progress("Harvesting Lithuanian headlines...")
    items = harvest.harvest()
    if not items:
        raise RuntimeError("no headlines harvested — check feeds/network")
    headlines = harvest.headlines_block(items)

    # Stage 1b — the pulse: real search / discussion / pageview signal that news
    # RSS misses. Best-effort; an empty pulse just means news-only for this run.
    progress("Reading the live pulse (searches, discussions, pageviews)...")
    signals = pulse.pulse()
    pulse_text = pulse.pulse_block(signals)
    progress(f"Pulse: {len(signals)} live signals from social/search sources.")

    feedback_text = feedback.feedback_block(feedback.load_feedback())

    system = llm.load_prompt("system")

    # Draft + structure in THEMED chunks: every chunk carries a mandatory theme
    # (state/geopolitics, economy, sport, culture), so batch balance is enforced
    # structurally — the model cannot drift into view-count bait when its chunk
    # mandate is state affairs. Chunking also keeps each call inside the ~8K
    # output cap.
    candidates: list[Candidate] = []
    chunk_size = (config.ZAI_DRAFT_CHUNK_SIZE if llm.provider("draft") == "zai"
                  else config.DRAFT_CHUNK_SIZE)
    for n, theme_label, theme_focus in _theme_chunks(count, chunk_size):
        progress(f"Researching & drafting {n} candidates — {theme_label} "
                 f"({len(candidates)} done, provider: {llm.provider('draft')})...")
        avoid_parts = []
        if config.BLOCKED_SUBJECTS:
            avoid_parts.append(
                "NEVER propose markets about these subjects (team-blocked): "
                + ", ".join(config.BLOCKED_SUBJECTS)
            )
        if candidates:
            avoid_parts.append(
                "ALREADY DRAFTED — do NOT duplicate these questions or topics:\n"
                + "\n".join(f"- {c.question_lt}" for c in candidates)
            )
        avoid = ("\n" + "\n".join(avoid_parts)) if avoid_parts else ""
        draft_prompt = llm.load_prompt(
            "draft",
            today=today.isoformat(),
            count=str(n),
            focus=theme_focus,
            headlines=headlines,
            pulse=pulse_text,
            feedback=feedback_text,
            avoid=avoid,
            timing=timing,
        )
        # A single unparseable chunk must not destroy an entire batch — the
        # work already done is worth keeping, so log and move on.
        try:
            draft_text = llm.research(draft_prompt, system=system,
                                      max_uses=config.SEARCH_MAX_USES_DRAFT,
                                      max_tokens=8000, stage="draft")
            structure_prompt = llm.load_prompt("structure", draft=draft_text)
            batch: CandidateBatch = llm.structure(structure_prompt, CandidateBatch,
                                                  max_tokens=8000)
        except Exception as exc:
            log.warning("chunk failed (%s); skipping it and continuing", exc)
            continue
        if not batch.candidates:
            log.warning("chunk (%s) produced 0 structured candidates; continuing",
                        theme_label)
            continue
        candidates.extend(batch.candidates)

    if not candidates:
        raise RuntimeError(
            "no candidates survived drafting/structuring — check the provider and the "
            "warnings above (a model that ignores the JSON schema is the usual cause)"
        )

    progress(f"Structured {len(candidates)} candidates. Validating...")

    conn = store.connect()
    # Legacy questions that today's rules would reject must not block their own
    # fixed replacements: "Ar Ignitis akcijos pasieks 24 €?" was stored before
    # the time-bound rule existed and was rejecting the corrected
    # "... iki spalio?" as a duplicate.
    existing = [q for q in store.recent_questions(conn)
                if not validate.lint_open_ended(q, "binary")]
    accepted_cands: list[Candidate] = []
    fixables: list[tuple[Candidate, str]] = []

    # Wording problems are fixable — send them for a rewrite instead of binning
    # a good idea. Factual/structural rejections are not repairable.
    REPAIRABLE = ("vague headline", "headline format", "vague/unclear options",
                  "open-ended question")

    def admit(cand: Candidate, reason: str | None, repairable: bool) -> None:
        if reason:
            if repairable and reason.startswith(REPAIRABLE):
                fixables.append((cand, reason))
            else:
                result.rejected.append((cand, reason))
            return
        dup = validate.is_duplicate(cand.question_lt, existing)
        if dup:
            result.rejected.append((cand, f"duplicate of existing market: {dup!r}"))
            return
        existing.append(cand.question_lt)  # dedupe within the batch too
        accepted_cands.append(cand)

    for cand in candidates:
        fixed, reason = validate.validate_candidate(cand, today, min_resolve=min_resolve)
        admit(fixed or cand, reason, repairable=True)

    # A vague headline is a wording problem, not an idea problem — give those
    # candidates one LLM rewrite round before discarding them.
    if fixables:
        progress(f"Repairing {len(fixables)} badly worded headlines...")
        try:
            import json as _json

            items = _json.dumps(
                [{"rejection_reason": r, **c.model_dump()} for c, r in fixables],
                ensure_ascii=False, indent=1,
            )
            repaired = llm.structure(
                llm.load_prompt("repair", items=items), CandidateBatch, max_tokens=8000
            )
            for cand in repaired.candidates:
                fixed, reason = validate.validate_candidate(cand, today, min_resolve=min_resolve)
                admit(fixed or cand, f"repair failed: {reason}" if reason else None,
                      repairable=False)
        except Exception as exc:
            log.warning("repair round failed: %s", exc)
            for cand, reason in fixables:
                result.rejected.append((cand, reason))

    if skip_verify:
        verdicts = [("UNCLEAR", "verification skipped")] * len(accepted_cands)
    else:
        progress(f"Verifying {len(accepted_cands)} candidates against the live web...")
        # The pulse already fetched hard numbers this session (stock closes,
        # chart positions) — hand them to the verifier so it never answers
        # UNCLEAR about a value we are holding in memory.
        live_facts = "\n".join(
            f"- {s.title}: {s.metric}" for s in signals if s.kind in ("stock", "chart")
        )
        verdicts = verify.verify_candidates(accepted_cands, today, live_facts=live_facts,
                                            min_resolve=min_resolve)

    # Images last, and only for candidates that survived every gate — no point
    # fetching pictures for markets about to be discarded.
    if config.IMAGES_ENABLED and accepted_cands:
        keep = [c for c, (v, _) in zip(accepted_cands, verdicts)
                if v not in ("DECIDED", "WRONG")]
        progress(f"Fetching images for {len(keep)} markets...")
        got = images.attach_images(keep)
        progress(f"Images: {got}/{len(keep)} markets illustrated.")

    for cand, (verdict, note) in zip(accepted_cands, verdicts):
        if verdict in ("DECIDED", "WRONG"):
            label = "already decided" if verdict == "DECIDED" else "factually flawed"
            result.rejected.append((cand, f"{label}: {note}"))
            store.insert_candidate(conn, cand, batch_id, "rejected",
                                   verify_verdict=verdict, verify_note=note,
                                   reject_reason=label)
            continue
        status = "needs_review" if verdict in ("UNCLEAR", "NOT_VERIFIED") else "candidate"
        db_id = store.insert_candidate(conn, cand, batch_id, status,
                                       verify_verdict=verdict, verify_note=note)
        result.accepted.append((db_id, cand, verdict, note))

    for cand, reason in result.rejected:
        if not reason.startswith(("already decided", "factually flawed")):
            store.insert_candidate(conn, cand, batch_id, "rejected", reject_reason=reason)
    conn.commit()
    conn.close()

    result.report_path = str(report.write_batch_report(batch_id, result.accepted, result.rejected))
    result.export_path = str(report.write_batch_export(batch_id, result.accepted))
    progress(f"Done: {len(result.accepted)} accepted ({result.needs_review} need review), "
             f"{len(result.rejected)} rejected.")
    return result
