"""The generation pipeline as a callable — shared by the CLI and the Telegram bot."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Callable

from . import config, harvest, llm, report, store, validate, verify
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
        return sum(1 for _, _, v, _ in self.accepted if v == "UNCLEAR")


def run_batch(
    count: int = config.DEFAULT_BATCH_SIZE,
    skip_verify: bool = False,
    progress: Progress = lambda msg: log.info("%s", msg),
) -> BatchResult:
    today = date.today()
    batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
    result = BatchResult(batch_id=batch_id)

    progress("Harvesting Lithuanian headlines...")
    items = harvest.harvest()
    if not items:
        raise RuntimeError("no headlines harvested — check feeds/network")
    headlines = harvest.headlines_block(items)
    system = llm.load_prompt("system")

    # Draft + structure in chunks — one call can't reliably carry a full
    # 35-candidate batch through an ~8K-token output cap.
    candidates: list[Candidate] = []
    remaining = count
    while remaining > 0:
        n = min(remaining, config.DRAFT_CHUNK_SIZE)
        progress(f"Researching & drafting {n} candidates "
                 f"({len(candidates)} done, provider: {llm.provider()})...")
        avoid = ""
        if candidates:
            avoid = ("\nALREADY DRAFTED — do NOT duplicate these questions or topics:\n"
                     + "\n".join(f"- {c.question_lt}" for c in candidates))
        draft_prompt = llm.load_prompt(
            "draft",
            today=today.isoformat(),
            count=str(n),
            headlines=headlines,
            avoid=avoid,
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

    for cand in candidates:
        fixed, reason = validate.validate_candidate(cand, today)
        if reason:
            result.rejected.append((cand, reason))
            continue
        dup = validate.is_duplicate(fixed.question_lt, existing)
        if dup:
            result.rejected.append((fixed, f"duplicate of existing market: {dup!r}"))
            continue
        existing.append(fixed.question_lt)  # dedupe within the batch too
        accepted_cands.append(fixed)

    if skip_verify:
        verdicts = [("UNCLEAR", "verification skipped")] * len(accepted_cands)
    else:
        progress(f"Verifying {len(accepted_cands)} candidates against the live web...")
        verdicts = verify.verify_candidates(accepted_cands, today)

    for cand, (verdict, note) in zip(accepted_cands, verdicts):
        if verdict == "DECIDED":
            result.rejected.append((cand, f"already decided: {note}"))
            store.insert_candidate(conn, cand, batch_id, "rejected",
                                   verify_verdict=verdict, verify_note=note,
                                   reject_reason="already decided")
            continue
        status = "needs_review" if verdict == "UNCLEAR" else "candidate"
        db_id = store.insert_candidate(conn, cand, batch_id, status,
                                       verify_verdict=verdict, verify_note=note)
        result.accepted.append((db_id, cand, verdict, note))

    for cand, reason in result.rejected:
        if not reason.startswith("already decided"):
            store.insert_candidate(conn, cand, batch_id, "rejected", reject_reason=reason)
    conn.commit()
    conn.close()

    result.report_path = str(report.write_batch_report(batch_id, result.accepted, result.rejected))
    result.export_path = str(report.write_batch_export(batch_id, result.accepted))
    progress(f"Done: {len(result.accepted)} accepted ({result.needs_review} need review), "
             f"{len(result.rejected)} rejected.")
    return result
