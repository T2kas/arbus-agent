"""The generation pipeline as a callable — shared by the CLI and the Telegram bot."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from . import config, feedback, harvest, llm, pulse, report, store, validate, verify
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
            f"resolve on {min_resolve.isoformat()} or later — never earlier. Spread the "
            "dates: most within 2-3 weeks after that day, ~20% a month or more later."
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

    # Draft + structure in chunks — one call can't reliably carry a full
    # 35-candidate batch through an ~8K-token output cap.
    candidates: list[Candidate] = []
    remaining = count
    while remaining > 0:
        n = min(remaining, config.DRAFT_CHUNK_SIZE)
        progress(f"Researching & drafting {n} candidates "
                 f"({len(candidates)} done, provider: {llm.provider()})...")
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
            headlines=headlines,
            pulse=pulse_text,
            feedback=feedback_text,
            avoid=avoid,
            timing=timing,
        )
        draft_text = llm.research(draft_prompt, system=system, max_uses=16, max_tokens=8000)
        structure_prompt = llm.load_prompt("structure", draft=draft_text)
        batch: CandidateBatch = llm.structure(structure_prompt, CandidateBatch, max_tokens=8000)
        if not batch.candidates:
            log.warning("chunk produced 0 structured candidates; stopping early")
            break
        candidates.extend(batch.candidates)
        remaining -= n

    progress(f"Structured {len(candidates)} candidates. Validating...")

    conn = store.connect()
    existing = store.recent_questions(conn)
    accepted_cands: list[Candidate] = []
    fixables: list[tuple[Candidate, str]] = []

    # Wording problems are fixable — send them for a rewrite instead of binning
    # a good idea. Factual/structural rejections are not repairable.
    REPAIRABLE = ("vague headline", "headline format", "vague/unclear options")

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
        verdicts = verify.verify_candidates(accepted_cands, today)

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
