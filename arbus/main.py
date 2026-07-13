"""CLI entry point.

    python -m arbus generate [--count 35] [--dry-run] [--skip-verify]
    python -m arbus promote <market_id> [<market_id> ...]
    python -m arbus list [--status candidate|needs_review|rejected|promoted]
    python -m arbus bot            # Telegram long-polling bot (/markets)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from . import config, harvest, llm, notify, pipeline, report, store
from .schemas import FullSpec

log = logging.getLogger("arbus")


def cmd_generate(args: argparse.Namespace) -> int:
    if args.dry_run:
        items = harvest.harvest()
        print(harvest.headlines_block(items))
        print(f"\n# {len(items)} headlines", file=sys.stderr)
        return 0

    result = pipeline.run_batch(count=args.count, skip_verify=args.skip_verify)
    notify.notify_batch(result.batch_id, len(result.accepted),
                        result.needs_review, result.report_path)
    print(f"\nBatch {result.batch_id}: {len(result.accepted)} accepted "
          f"({result.needs_review} need review), {len(result.rejected)} rejected")
    print(f"Report: {result.report_path}\nExport: {result.export_path}")
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


def cmd_bot(_args: argparse.Namespace) -> int:
    from . import bot

    return bot.run()


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

    b = sub.add_parser("bot", help="run the Telegram bot (long polling)")
    b.set_defaults(func=cmd_bot)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
