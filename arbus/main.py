"""CLI entry point.

    python -m arbus generate [--count 35] [--dry-run] [--skip-verify]
    python -m arbus promote <market_id> [<market_id> ...]
    python -m arbus list [--status candidate|needs_review|rejected|promoted]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timezone

from . import config, harvest, llm, notify, report, store, validate, verify
from .schemas import CandidateBatch, FullSpec

log = logging.getLogger("arbus")


def cmd_generate(args: argparse.Namespace) -> int:
    today = date.today()
    batch_id = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")

    log.info("harvesting headlines...")
    items = harvest.harvest()
    log.info("harvested %d headlines from %d feeds", len(items), len(config.FEEDS))
    if not items:
        log.error("no headlines harvested — check feed URLs / network")
        return 1
    if args.dry_run:
        print(harvest.headlines_block(items))
        return 0

    log.info("researching + drafting %d candidates (web search enabled)...", args.count)
    draft_prompt = llm.load_prompt(
        "draft",
        today=today.isoformat(),
        count=str(args.count),
        headlines=harvest.headlines_block(items),
    )
    system = llm.load_prompt("system")
    draft_text = llm.research(draft_prompt, system=system, max_uses=16, max_tokens=48000)

    log.info("structuring draft into schema...")
    structure_prompt = llm.load_prompt("structure", draft=draft_text)
    # 16K output is plenty for ~35 candidates and stays under the SDK's
    # non-streaming timeout guard.
    batch = llm.structure(structure_prompt, CandidateBatch, max_tokens=16000)
    log.info("structured %d candidates", len(batch.candidates))

    conn = store.connect()
    existing = store.recent_questions(conn)
    accepted_cands = []
    rejected: list = []

    for cand in batch.candidates:
        fixed, reason = validate.validate_candidate(cand, today)
        if reason:
            rejected.append((cand, reason))
            continue
        dup = validate.is_duplicate(fixed.question_lt, existing)
        if dup:
            rejected.append((fixed, f"duplicate of existing market: {dup!r}"))
            continue
        existing.append(fixed.question_lt)  # dedupe within the batch too
        accepted_cands.append(fixed)

    log.info("validation: %d accepted, %d rejected", len(accepted_cands), len(rejected))

    if args.skip_verify:
        verdicts = [("UNCLEAR", "verification skipped")] * len(accepted_cands)
    else:
        log.info("verifying %d candidates against the live web...", len(accepted_cands))
        verdicts = verify.verify_candidates(accepted_cands, today)

    accepted = []
    for cand, (verdict, note) in zip(accepted_cands, verdicts):
        if verdict == "DECIDED":
            rejected.append((cand, f"already decided: {note}"))
            store.insert_candidate(conn, cand, batch_id, "rejected",
                                   verify_verdict=verdict, verify_note=note,
                                   reject_reason="already decided")
            continue
        status = "needs_review" if verdict == "UNCLEAR" else "candidate"
        db_id = store.insert_candidate(conn, cand, batch_id, status,
                                       verify_verdict=verdict, verify_note=note)
        accepted.append((db_id, cand, verdict, note))

    for cand, reason in rejected:
        if not reason.startswith("already decided"):
            store.insert_candidate(conn, cand, batch_id, "rejected", reject_reason=reason)
    conn.commit()
    conn.close()

    report_path = report.write_batch_report(batch_id, accepted, rejected)
    export_path = report.write_batch_export(batch_id, accepted)
    needs_review = sum(1 for _, _, v, _ in accepted if v == "UNCLEAR")
    notify.notify_batch(batch_id, len(accepted), needs_review, str(report_path))

    print(f"\nBatch {batch_id}: {len(accepted)} accepted ({needs_review} need review), "
          f"{len(rejected)} rejected")
    print(f"Report: {report_path}\nExport: {export_path}")
    return 0


def cmd_promote(args: argparse.Namespace) -> int:
    today = date.today()
    conn = store.connect()
    system = llm.load_prompt("system")
    exit_code = 0

    for market_id in args.market_ids:
        row = store.get_market(conn, market_id)
        if row is None:
            log.error("market #%d not found", market_id)
            exit_code = 1
            continue
        candidate_json = json.dumps(
            {k: row[k] for k in row.keys() if k not in ("id",)}, ensure_ascii=False, indent=2
        )
        log.info("promoting market #%d: %s", market_id, row["question_lt"])
        prompt = llm.load_prompt("promote", today=today.isoformat(), candidate=candidate_json)
        research_text = llm.research(prompt, system=system, max_uses=10, max_tokens=16000)
        spec = llm.structure(
            "Convert this full-mode market specification into the structured format, "
            "copying Lithuanian text faithfully:\n\n" + research_text,
            FullSpec,
        )
        store.save_full_spec(conn, market_id, spec)
        conn.commit()
        path = report.append_full_spec(row["batch_id"], market_id, spec)
        print(f"#{market_id} promoted — spec appended to {path}")

    conn.close()
    return exit_code


def cmd_list(args: argparse.Namespace) -> int:
    conn = store.connect()
    rows = store.list_markets(conn, status=args.status)
    for r in rows:
        flag = {"needs_review": "⚠️", "rejected": "✗", "promoted": "★"}.get(r["status"], " ")
        print(f"{flag} #{r['id']:>4} [{r['status']:<12}] {r['resolve_by']} {r['question_lt']}")
    conn.close()
    return 0


def main() -> int:
    # Windows consoles default to cp1252, which cannot print Lithuanian text
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="arbus", description="Arbus market generator")
    sub = parser.add_subparsers(dest="command", required=True)

    g = sub.add_parser("generate", help="generate a new candidate batch")
    g.add_argument("--count", type=int, default=config.DEFAULT_BATCH_SIZE)
    g.add_argument("--dry-run", action="store_true", help="harvest headlines only, no LLM calls")
    g.add_argument("--skip-verify", action="store_true", help="skip the already-decided web check")
    g.set_defaults(func=cmd_generate)

    p = sub.add_parser("promote", help="write full-mode specs for selected markets")
    p.add_argument("market_ids", type=int, nargs="+")
    p.set_defaults(func=cmd_promote)

    ls = sub.add_parser("list", help="list stored markets")
    ls.add_argument("--status", choices=["candidate", "needs_review", "rejected", "promoted"])
    ls.set_defaults(func=cmd_list)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
