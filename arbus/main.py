"""CLI entry point.

    python -m arbus generate [--count 35] [--dry-run] [--skip-verify]
    python -m arbus promote <market_id> [<market_id> ...]
    python -m arbus list [--status candidate|needs_review|rejected|promoted]
    python -m arbus resolve [--apply]                     # Job 2: close markets
    python -m arbus check                                 # AI-check + Telegram alert
    python -m arbus settle                                # pay out after the undo window
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


def cmd_resolve(args: argparse.Namespace) -> int:
    from . import resolve

    today = date.today()
    conn = store.connect()
    rows = resolve.due_markets(conn, today, limit=args.limit)
    if not rows:
        print("No markets are due for resolution.")
        conn.close()
        return 0

    print(f"Checking {len(rows)} market(s) whose resolution date has passed...\n")
    verdicts = resolve.check_markets(rows, today)

    freezing = leaving = 0
    for i, row in enumerate(rows, 1):
        v = verdicts[i]
        options = json.loads(row["options_json"])
        freeze, detail = resolve.should_freeze(v, options)
        flag = "🧊" if freeze else "·"
        print(f"{flag} #{row['id']} {row['question_lt']}")
        print(f"    {v['verdict']}"
              + (f" → {detail}" if freeze else "")
              + f" | {v['confidence']} | {v['reason']}")
        if v["source"]:
            print(f"    source: {v['source']}")
        if not freeze:
            print(f"    left trading: {detail}")
        if args.apply:
            resolve.record(conn, row["id"], v, freeze=freeze, note=v["reason"])
        freezing += freeze
        leaving += not freeze
        print()

    if args.apply:
        conn.commit()
        print(f"Froze {freezing} market(s) for admin review; left {leaving} trading.")
        print("Nothing was settled — the admin decides in the dashboard, and "
              "settlement waits for the undo window.")
    else:
        print(f"Dry run — nothing written. Would freeze {freezing}, "
              f"leave {leaving} trading. Re-run with --apply.")
    conn.close()
    return 0


def cmd_settle(args: argparse.Namespace) -> int:
    """Pay out markets whose undo window has expired.

    Run this on a short schedule (every minute or two). Until it runs, an
    admin's decision is still reversible — which is the whole point of the
    delay, but it also means settlement never happens without this.
    """
    from . import resolution

    conn = store.connect()
    pending = resolution.due_for_settlement(conn)
    if not pending:
        waiting = conn.execute(
            "SELECT COUNT(*) FROM markets WHERE resolution_state = 'RESOLVING'"
        ).fetchone()[0]
        print(f"Nothing to settle. {waiting} market(s) still inside the "
              f"{config.SETTLEMENT_DELAY_MINUTES}-minute undo window.")
        conn.close()
        return 0

    if args.dry_run:
        for row in pending:
            print(f"would settle #{row['id']} — {row['admin_decision']} "
                  f"{row['resolution_option']}: {row['question_lt']}")
        conn.close()
        return 0

    for row in pending:
        summary = resolution.settle(conn, row["id"])
        paid = sum(amount for _, amount in summary["paid"])
        lost = sum(amount for _, amount in summary["forfeited"])
        print(f"#{row['id']} {summary['decision']} {summary['option']} — "
              f"paid {paid} Arbucks, forfeited {lost}")
    conn.commit()
    conn.close()
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Run the advisory AI check on frozen markets and alert Telegram.

    This is the step between "a user reported the outcome" and "the admin
    decides": it reads the cited source and sends the team a summary with the
    evidence. It decides nothing — the dashboard is where the decision happens.
    """
    from . import aicheck, resolution

    conn = store.connect()
    resolution.init(conn)          # picks up the ai_summary columns on old DBs
    alert = not args.no_telegram

    requests_ = ([conn.execute("SELECT * FROM resolution_requests WHERE id = ?",
                               (rid,)).fetchone() for rid in args.request_ids]
                 if args.request_ids else aicheck.pending_requests(conn, args.limit))
    requests_ = [r for r in requests_ if r is not None]

    freezes = [] if args.request_ids else aicheck.pending_freezes(conn, args.limit)

    if not requests_ and not freezes:
        print("Nothing frozen is waiting for a check.")
        conn.close()
        return 0

    for req in requests_:
        print(f"\n— request #{req['id']} on market #{req['market_id']} "
              f"({req['user_id']} → {req['proposed_option']})")
        summary = aicheck.review_request(conn, req["id"], alert=alert)
        conn.commit()
        print(summary)

    for market in freezes:
        print(f"\n— market #{market['id']} frozen: {market['freeze_reason']}")
        print(aicheck.review_freeze(conn, market["id"], alert=alert))

    conn.commit()
    conn.close()
    print(f"\nChecked {len(requests_)} request(s) and {len(freezes)} freeze(s)."
          + ("" if alert else " Telegram alerts skipped (--no-telegram)."))
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

    rs = sub.add_parser("resolve",
                        help="sweep markets past their date and freeze the decided ones")
    rs.add_argument("--apply", action="store_true",
                    help="record findings and freeze (default is a dry run)")
    rs.add_argument("--limit", type=int, default=25)
    rs.set_defaults(func=cmd_resolve)

    st = sub.add_parser("settle",
                        help="pay out decisions whose undo window has expired")
    st.add_argument("--dry-run", action="store_true")
    st.set_defaults(func=cmd_settle)

    ck = sub.add_parser("check",
                        help="AI-check frozen markets and alert Telegram")
    ck.add_argument("request_ids", type=int, nargs="*",
                    help="specific resolution request ids (default: all pending)")
    ck.add_argument("--limit", type=int, default=10)
    ck.add_argument("--no-telegram", action="store_true",
                    help="print the summary without sending it to the team")
    ck.set_defaults(func=cmd_check)

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
