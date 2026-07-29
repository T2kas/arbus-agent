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


def cmd_app(args: argparse.Namespace) -> int:
    """Show the markets the Arbus app itself is serving.

    This is the connection test: if it lists your app's markets, the generator
    can read the app, and every future batch will avoid duplicating them.
    """
    from . import publish

    if not config.ARBUS_API_URL:
        print("ARBUS_API_URL is not set in .env — nothing to connect to.")
        return 1

    rows, error = publish.fetch_app_markets(args.limit)
    if error:
        print(f"❌ Could not read the app: {error}")
        return 1

    print(f"✅ Connected. The app is serving {len(rows)} market(s).\n")
    for row in rows:
        question = publish.question_of(row) or "(no question field found)"
        created = str(row.get("created_at", ""))[:10]
        status = row.get("status") or row.get("state") or ""
        options = row.get("market_options") or row.get("options") or []
        print(f"· {created} {question}")
        if status:
            print(f"    status: {status}")
        if isinstance(options, list) and options:
            labels = [str(o.get("label") or o.get("name") or o.get("title") or o)
                      if isinstance(o, dict) else str(o) for o in options]
            print(f"    options: {' / '.join(labels[:6])}")

    if args.schema:
        from . import app as app_api

        print("\n── Columns each endpoint returns ──")
        if rows:
            print(f"markets: {', '.join(sorted(rows[0].keys()))}")
            statuses = sorted({app_api.status_of(r) for r in rows if app_api.status_of(r)})
            print(f"statuses in use: {', '.join(statuses) or '(none)'}")
            frozen = ", ".join(sorted(config.APP_FROZEN_STATUSES & set(statuses))) or "(none)"
            print(f"of those, treated as frozen: {frozen}")
        for label, fetch in (("option_price_history", app_api.price_history),
                             ("admin_recent_trades", app_api.recent_trades),
                             ("admin_list_profiles", app_api.profiles)):
            data, err = fetch()
            if err:
                print(f"{label}: ⚠️ {err}")
            elif data:
                print(f"{label}: {', '.join(sorted(data[0].keys()))}")
            else:
                print(f"{label}: (reachable, no rows yet)")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """Circuit breaker against live app data.

    The breaker logic existed but had nothing to look at — price history and
    trades live in the app. This reads both, finds markets where the price
    swung AND several distinct users pushed it, and tells the team. It changes
    nothing in the app: stopping a market is still a human's click.
    """
    from . import app as app_api, notify

    if not config.ARBUS_API_URL:
        print("ARBUS_API_URL is not set in .env — no live data to watch.")
        return 1

    if args.interval:
        import time

        print(f"Watching every {args.interval}s — Ctrl+C to stop.")
        while True:
            _watch_once(args)
            time.sleep(args.interval)

    return _watch_once(args)


def _watch_once(args: argparse.Namespace) -> int:
    from . import app as app_api, notify

    rows, error = app_api.breaker_candidates(window_minutes=args.window)
    if error:
        print(f"❌ {error}")
        return 1
    if not rows:
        print(f"No price movement in the last {args.window} min.")
        return 0

    tripped = [r for r in rows if r["tripped"]]
    for item in rows[:15]:
        flag = "🚨" if item["tripped"] else "·"
        question = app_api.question_of(item["market"]) or app_api.market_id_of(item["market"])
        print(f"{flag} {item['move']:+.0%} move, {item['users']} user(s) — {question}")

    print(f"\n{len(tripped)} market(s) trip the breaker "
          f"(≥{config.CB_PRICE_MOVE:.0%} move AND ≥{config.CB_MIN_DISTINCT_USERS} users "
          f"in {args.window} min).")

    frozen_now: list[str] = []
    if tripped and args.freeze:
        for item in tripped:
            mid = app_api.market_id_of(item["market"])
            ok, detail = app_api.set_status(mid, config.APP_FREEZE_STATUS)
            print(f"   {'🧊' if ok else '❌'} {mid}: {detail}")
            if ok:
                frozen_now.append(mid)

    if tripped and not args.no_telegram:
        lines = ["🚨 ĮTARTINAS SRAUTAS — verta sustabdyti prekybą", ""]
        for item in tripped:
            lines.append(f"· {item['move']:+.0%}, {item['users']} vartotojai — "
                         f"{app_api.question_of(item['market'])}")
        lines += ["", "Kainos šuolis + keli skirtingi vartotojai ta pačia kryptimi "
                      "dažniausiai reiškia, kad kažkas jau žino rezultatą."]
        lines.append(
            f"🧊 Botas jau sustabdė {len(frozen_now)} rinką (-as) appe. "
            "Paleisk `arbus check`."
            if frozen_now else
            "Botas nieko nestabdo — sustabdyk dashboarde ir paleisk `arbus check`.")
        notify.send("\n".join(lines))
        print("Telegram alert sent.")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """Market health from the app's own trades. No LLM, no cost.

    Four questions, all answerable from data the app already exposes:
    which markets nobody bets on (and should therefore stop being generated),
    which ones are big enough to promote, which are trading past their own
    deadline (the expensive one under an AMM), and where our opening price was
    far from where the market actually settled.
    """
    from . import app as app_api, notify

    if not config.ARBUS_API_URL:
        print("ARBUS_API_URL is not set in .env — no app data to read.")
        return 1

    market_rows, error = app_api.markets(200)
    if error:
        print(f"❌ Could not read the app: {error}")
        return 1
    trades, trade_error = app_api.recent_trades()
    if trade_error:
        print(f"⚠️  Trades unavailable ({trade_error}) — dead/important skipped.")
    stats = app_api.trade_stats(trades, days=args.days)

    dead, important = [], []
    for row in market_rows:
        if not app_api.is_open(row):
            continue
        stat = app_api.market_stat(stats, row)
        # Lifetime volume lives on the market itself; recent bet count comes
        # from the trade window. Important = a lot of money AND many people.
        lifetime_volume = app_api.volume_of(row)
        if lifetime_volume >= config.IMPORTANT_VOLUME and stat["users"] >= config.IMPORTANT_USERS:
            important.append((row, {**stat, "volume": lifetime_volume}))
        elif trades and stat["trades"] < config.DEAD_MARKET_MIN_TRADES:
            dead.append((row, stat))

    overdue = app_api.overdue_markets(market_rows)

    print(f"🔥 SVARBIOS ({len(important)}) — ≥{config.IMPORTANT_VOLUME} Arbukų "
          f"ir ≥{config.IMPORTANT_USERS} vartotojų")
    for row, stat in important:
        print(f"  · {stat['volume']:.0f} Arbukų, {stat['users']} vartotojų — "
              f"{app_api.question_of(row)}")

    print(f"\n💀 NEGYVOS ({len(dead)}) — <{config.DEAD_MARKET_MIN_TRADES} statymų "
          f"per {args.days} d.")
    for row, stat in dead[:15]:
        print(f"  · {stat['trades']} statymai — {app_api.question_of(row)}")

    print(f"\n⏰ PRAVĖLUOTOS ({len(overdue)}) — terminas praėjo, o prekyba vyksta")
    for row in overdue:
        print(f"  · {app_api._pick(row, 'resolve_by', 'closes_at', default='?')} "
              f"{app_api.question_of(row)}")

    if not args.no_telegram and (important or overdue or dead):
        lines = ["📊 Arbus rinkų sveikata", ""]
        if overdue:
            lines.append(f"⏰ {len(overdue)} rinkos prekiauja po savo termino "
                         f"— tai tiesioginis AMM nuostolis:")
            lines += [f"  · {app_api.question_of(r)}" for r in overdue[:5]]
        if important:
            lines.append(f"\n🔥 {len(important)} rinkos verta iškelti į „breaking“:")
            lines += [f"  · {s['volume']:.0f} Arbukų, {s['users']} vartotojų — "
                      f"{app_api.question_of(r)}" for r, s in important[:5]]
        if dead:
            lines.append(f"\n💀 {len(dead)} rinkos be statymų per {args.days} d. "
                         f"— tokių nebegeneruoti.")
            lines += [f"  · {app_api.question_of(r)}" for r, _ in dead[:5]]
        notify.send("\n".join(lines))
        print("\nTelegram summary sent.")

    if dead and args.teach:
        note = ("Vengti temų, kurios negauna statymų: "
                + "; ".join(app_api.question_of(r) for r, _ in dead[:5]))
        feedback.append_feedback(note)
        print("Dead markets recorded in feedback.md for future batches.")
    return 0


def cmd_calibration(args: argparse.Namespace) -> int:
    """Where our opening probability sat versus the app's live price.

    This is the generator's own score. A market we opened at 20% that trades at
    80% was mispriced at birth, and the pattern across a batch says more about
    the prompt than any single review does.
    """
    from . import app as app_api

    if not config.ARBUS_API_URL:
        print("ARBUS_API_URL is not set in .env.")
        return 1

    market_rows, error = app_api.markets(200)
    if error:
        print(f"❌ {error}")
        return 1
    history, hist_error = app_api.price_history(500)
    if hist_error:
        print(f"❌ price history unavailable: {hist_error}")
        return 1
    prices = app_api.latest_prices(history)

    conn = store.connect()
    ours = {q.strip().lower(): p for q, p in conn.execute(
        "SELECT question_lt, probabilities_json FROM markets").fetchall()}
    conn.close()

    gaps = []
    for row in market_rows:
        question = app_api.question_of(row)
        stored = ours.get(question.strip().lower())
        if not stored:
            continue
        try:
            opening = json.loads(stored)[0]
        except (ValueError, IndexError):
            continue
        options = row.get("market_options") or row.get("options") or []
        live = next((prices[str(o.get("id"))] for o in options
                     if isinstance(o, dict) and str(o.get("id")) in prices), None)
        if live is None:
            continue
        gaps.append((abs(live - opening), opening, live, question))

    if not gaps:
        print("No overlap yet between our stored markets and live prices.")
        return 0

    gaps.sort(reverse=True)
    print(f"Comparing {len(gaps)} market(s); flagging gaps ≥ "
          f"{config.CALIBRATION_GAP:.0%}\n")
    for gap, opening, live, question in gaps[:args.limit]:
        flag = "⚠️" if gap >= config.CALIBRATION_GAP else " ·"
        print(f"{flag} mūsų {opening:.0%} → rinka {live:.0%}  ({gap:+.0%})  {question}")
    average = sum(g for g, _, _, _ in gaps) / len(gaps)
    print(f"\nAverage gap: {average:.1%} "
          f"({sum(1 for g, *_ in gaps if g >= config.CALIBRATION_GAP)} flagged)")
    return 0


def cmd_facts(args: argparse.Namespace) -> int:
    """Show what the deterministic data feeds return for a market question.

    This is the diagnostic for "the check said it couldn't find the Ignitis
    price / Vilnius temperature": those should come from a feed, not the model.
    A ✅ means the number is fetched and will be handed to the check; a ❌ with
    an error means the feed is unreachable from here (then it is a network/URL
    problem, not the model's fault).
    """
    from . import resolvers

    question = " ".join(args.question)
    rows = resolvers.diagnose(question)
    if not rows:
        print("Nė vienas duomenų feed'as netinka šiai rinkai — "
              "bus ieškoma per modelį (akcijos/oras/degalai netaikomi).")
        return 0
    for feed, fact, error in rows:
        if fact:
            print(f"✅ {feed}: {fact}")
        else:
            print(f"❌ {feed}: NEPAVYKO — {error}")
    ok = sum(1 for _, fact, _ in rows if fact)
    print(f"\n{ok}/{len(rows)} feed'ai atidavė duomenis. "
          + ("Šie faktai bus paduoti patikrai." if ok else
             "Nė vienas nepavyko — patikrink internetą/URL (žr. klaidą aukščiau)."))
    return 0


def cmd_feeds(_args: argparse.Namespace) -> int:
    """Show which news feeds are alive and how much each contributes."""
    rows = harvest.probe_feeds()
    for name, url, fresh, error in rows:
        flag = "✓" if fresh else "✗"
        print(f"{flag} {name:<16} {fresh:>4} straipsniai  {url}")
        if error:
            print(f"    {error[:120]}")
    dead = [n for n, _, f, _ in rows if not f]
    print(f"\n{len(rows) - len(dead)}/{len(rows)} feeds alive."
          + (f" Dead: {', '.join(dead)}" if dead else ""))
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """Run the advisory AI check on frozen markets and alert Telegram.

    This is the step between "a user reported the outcome" and "the admin
    decides": it reads the cited source and sends the team a summary with the
    evidence. It decides nothing — the dashboard is where the decision happens.
    """
    from . import aicheck, llm, resolution

    # Show exactly which provider + model will run the check. This is the line
    # that catches the classic mix-up: setting OPENAI_AICHECK_MODEL but leaving
    # LLM_PROVIDER_AICHECK=perplexity means the check still runs on Perplexity.
    prov, model = llm.aicheck_target()
    print(f"🔎 Resolution check: provider={prov}, model={model} "
          f"(set by LLM_PROVIDER_AICHECK / {prov.upper()}_AICHECK_MODEL)\n")

    conn = store.connect()
    resolution.init(conn)          # picks up the ai_summary columns on old DBs
    alert = not args.no_telegram

    requests_ = ([conn.execute("SELECT * FROM resolution_requests WHERE id = ?",
                               (rid,)).fetchone() for rid in args.request_ids]
                 if args.request_ids else aicheck.pending_requests(conn, args.limit))
    requests_ = [r for r in requests_ if r is not None]

    freezes = [] if args.request_ids else aicheck.pending_freezes(conn, args.limit)

    # The app is the other half of the truth: an admin pausing a market in the
    # dashboard leaves no trace in this database, which is why "nothing frozen"
    # used to be printed while the app had stopped markets waiting.
    app_frozen: list[dict] = []
    if not args.request_ids and not args.no_app and config.ARBUS_API_URL:
        app_frozen, app_error = aicheck.pending_app_markets(conn, limit=args.limit)
        if app_error:
            print(f"⚠️  Could not read the app: {app_error}")
        if args.match:
            from . import app as app_api
            needle = args.match.lower()
            app_frozen = [m for m in app_frozen
                          if needle in app_api.question_of(m).lower()]
            print(f"(--match {args.match!r}: checking {len(app_frozen)} matching "
                  "market(s))")

    if not requests_ and not freezes and not app_frozen:
        print("Nothing frozen is waiting for a check.")
        if not config.ARBUS_API_URL:
            print("(ARBUS_API_URL is not set, so markets frozen in the app are "
                  "invisible to this command — see README.)")
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

    from . import app as app_api
    import time
    for idx, market in enumerate(app_frozen):
        # Pace the checks: firing every market's web searches back-to-back
        # rate-limits the search tool (later markets come back "limit exceeded").
        if idx and config.APP_CHECK_DELAY_SECONDS:
            time.sleep(config.APP_CHECK_DELAY_SECONDS)
        print(f"\n— app market {app_api.market_id_of(market)} "
              f"[{app_api.status_of(market)}]: {app_api.question_of(market)}")
        print(aicheck.review_app_market(conn, market, alert=alert))
        conn.commit()

    conn.commit()
    conn.close()
    print(f"\nChecked {len(requests_)} request(s), {len(freezes)} local freeze(s) "
          f"and {len(app_frozen)} app-frozen market(s)."
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

    ap = sub.add_parser("app", help="list the markets the Arbus app is serving")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--schema", action="store_true",
                    help="also print the column names the app returns")
    ap.set_defaults(func=cmd_app)

    fd = sub.add_parser("feeds", help="check which news feeds still work")
    fd.set_defaults(func=cmd_feeds)

    ft = sub.add_parser("facts",
                        help="show what the data feeds return for a market question")
    ft.add_argument("question", nargs="+",
                    help='e.g. arbus facts "Ar Ignitis akcija pakils virš 23 Eur?"')
    ft.set_defaults(func=cmd_facts)

    ck = sub.add_parser("check",
                        help="AI-check frozen markets and alert Telegram")
    ck.add_argument("request_ids", type=int, nargs="*",
                    help="specific resolution request ids (default: all pending)")
    ck.add_argument("--limit", type=int, default=10)
    ck.add_argument("--no-telegram", action="store_true",
                    help="print the summary without sending it to the team")
    ck.add_argument("--no-app", action="store_true",
                    help="skip markets frozen in the app, check local ones only")
    ck.add_argument("--match", metavar="TEXT",
                    help="only check app markets whose title contains TEXT "
                         '(case-insensitive), e.g. --match Eurovizij — much '
                         "faster than checking every frozen market")
    ck.set_defaults(func=cmd_check)

    wt = sub.add_parser("watch",
                        help="scan live prices and trades for circuit-breaker hits")
    wt.add_argument("--window", type=int, default=config.CB_WINDOW_MINUTES,
                    help="minutes of history to consider")
    wt.add_argument("--no-telegram", action="store_true")
    wt.add_argument("--freeze", action="store_true",
                    help="also stop trading in the app (needs a service_role key)")
    wt.add_argument("--interval", type=int, default=0,
                    help="keep running, scanning every N seconds (0 = once)")
    wt.set_defaults(func=cmd_watch)

    stt = sub.add_parser("stats", help="market health: dead, important, overdue")
    stt.add_argument("--days", type=int, default=config.DEAD_MARKET_DAYS)
    stt.add_argument("--no-telegram", action="store_true")
    stt.add_argument("--teach", action="store_true",
                     help="write the dead markets into feedback.md")
    stt.set_defaults(func=cmd_stats)

    cal = sub.add_parser("calibration",
                         help="our opening probability vs the app's live price")
    cal.add_argument("--limit", type=int, default=20)
    cal.set_defaults(func=cmd_calibration)

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
