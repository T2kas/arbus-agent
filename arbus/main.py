"""CLI entry point.

    python -m arbus generate [--count 35] [--dry-run] [--skip-verify]
    python -m arbus promote <market_id> [<market_id> ...]
    python -m arbus list [--status candidate|needs_review|rejected|promoted]
    python -m arbus feedback "mažiau ekonomikos rinkų"   # teach future batches
    python -m arbus bot            # Telegram long-polling bot (/markets)
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date

from . import config, feedback, harvest, llm, notify, pipeline, pulse, report, store
from .schemas import FullSpec

log = logging.getLogger("arbus")


def cmd_generate(args: argparse.Namespace) -> int:
    if args.dry_run:
        items = harvest.harvest()
        print(harvest.headlines_block(items))
        signals = pulse.pulse()
        print("\n\n=== PULSE (live social/search signal) ===\n")
        print(pulse.pulse_block(signals))
        print(f"\n# {len(items)} headlines, {len(signals)} pulse signals", file=sys.stderr)
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


def cmd_rebuild_db(_args: argparse.Namespace) -> int:
    conn = store.connect()
    imported, skipped = store.rebuild_from_exports(conn)
    total = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    conn.close()
    print(f"Imported {imported} markets from exports/ ({skipped} skipped as "
          f"already present or unreadable).")
    print(f"Database now holds {total} markets for duplicate checking.")
    return 0


def cmd_publish(args: argparse.Namespace) -> int:
    from . import publish

    conn = store.connect()
    exit_code = 0
    for market_id in args.market_ids:
        row = store.get_market(conn, market_id)
        if row is None:
            log.error("market #%d not found", market_id)
            exit_code = 1
            continue
        if row["status"] == "rejected":
            log.error("#%d was rejected — refusing to publish", market_id)
            exit_code = 1
            continue
        if row["published_at"] and not args.force:
            print(f"#{market_id} already published at {row['published_at']} "
                  f"— use --force to send again")
            continue

        if args.dry_run:
            print(json.dumps(publish.market_payload(row), ensure_ascii=False, indent=2))
            continue

        ok, detail = publish.publish_market(row)
        if ok:
            publish.mark_published(conn, market_id, detail)
            conn.commit()
            print(f"#{market_id} published ({detail})")
        else:
            log.error("#%d publish failed: %s", market_id, detail)
            exit_code = 1

    conn.close()
    return exit_code


def cmd_feedback(args: argparse.Namespace) -> int:
    note = " ".join(args.text)
    line = feedback.append_feedback(note)
    if not line:
        print("Nothing to record (empty note).")
        return 1
    print(f"Saved to {feedback.FEEDBACK_PATH.name}: {line}")
    print("This guidance applies to every future batch.")
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

    rb = sub.add_parser("rebuild-db",
                        help="rebuild the duplicate-check history from exports/")
    rb.set_defaults(func=cmd_rebuild_db)

    pub = sub.add_parser("publish", help="push selected markets to the Arbus app API")
    pub.add_argument("market_ids", type=int, nargs="+")
    pub.add_argument("--dry-run", action="store_true",
                     help="print the payload instead of sending it")
    pub.add_argument("--force", action="store_true",
                     help="publish again even if already published")
    pub.set_defaults(func=cmd_publish)

    fb = sub.add_parser("feedback", help="record a note that guides every future batch")
    fb.add_argument("text", nargs="+", help='e.g. arbus feedback "mažiau ekonomikos rinkų"')
    fb.set_defaults(func=cmd_feedback)

    b = sub.add_parser("bot", help="run the Telegram bot (long polling)")
    b.set_defaults(func=cmd_bot)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
