"""Resolve markets from a reviewed decision file (markets/resolutions/*.json).

For outcomes no feed can settle (a poll, a law, a release, a fight) the
decision is researched, written to a JSON file and reviewed in the repo; a push
then runs the resolve-markets workflow, which has the service_role key. Every
entry is checked against the app first and the whole file is refused if any
entry is wrong, so a typo can never pay the wrong side:

  * the market id must exist and its title must match exactly;
  * every named option label must exist in that market;
  * an already-resolved market or option is skipped (re-running is a no-op).

Entry forms (one per market):

  {"market_id": "...", "title": "...", "resolve": "<winning label>",
   "source": "https://...", "note": "why"}
  {"market_id": "...", "title": "...", "options": {"<date label>": "yes"|"no"},
   "source": "...", "note": "..."}               # date ladders ("... iki...?")
  {"market_id": "...", "title": "...", "split": ["<label>", "<label>"],
   "source": "...", "note": "..."}               # rules' equal-split clause
"""

from __future__ import annotations

import json
from pathlib import Path

from . import app as app_api, notify


def load(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("resolution file must hold a JSON list")
    return data


def _option_by_label(market: dict, label: str) -> dict | None:
    for o in market.get("market_options") or []:
        if str(o.get("label") or "").strip() == label.strip():
            return o
    return None


def plan(entries: list[dict], rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Validate every entry against the live app rows. Returns (actions, errors);
    an action is {kind, market, entry, option(s)}. Any error → do nothing."""
    by_id = {app_api.market_id_of(r): r for r in rows}
    actions, errors = [], []
    for i, e in enumerate(entries, 1):
        mid, title = e.get("market_id", ""), e.get("title", "")
        m = by_id.get(mid)
        if not m:
            errors.append(f"#{i}: market {mid} not found in the app")
            continue
        if app_api.question_of(m).strip() != title.strip():
            errors.append(f"#{i}: title mismatch for {mid}: app has {app_api.question_of(m)!r}")
            continue
        kinds = [k for k in ("resolve", "options", "split") if k in e]
        if len(kinds) != 1:
            errors.append(f"#{i}: give exactly one of resolve / options / split")
            continue
        kind = kinds[0]
        if kind == "resolve":
            o = _option_by_label(m, e["resolve"])
            if not o:
                errors.append(f"#{i}: option {e['resolve']!r} not in {title!r}")
                continue
            if app_api.status_of(m) == "resolved":
                actions.append({"kind": "skip", "market": m, "entry": e, "why": "jau išspręsta"})
                continue
            actions.append({"kind": "resolve", "market": m, "entry": e, "option": o})
        elif kind == "split":
            opts = [_option_by_label(m, lbl) for lbl in e["split"]]
            if not all(opts) or len(opts) < 2:
                errors.append(f"#{i}: split labels {e['split']} not all in {title!r}")
                continue
            if app_api.status_of(m) == "resolved":
                actions.append({"kind": "skip", "market": m, "entry": e, "why": "jau išspręsta"})
                continue
            actions.append({"kind": "split", "market": m, "entry": e, "options": opts})
        else:
            for label, outcome in e["options"].items():
                o = _option_by_label(m, label)
                if not o:
                    errors.append(f"#{i}: option {label!r} not in {title!r}")
                    continue
                if outcome not in ("yes", "no"):
                    errors.append(f"#{i}: outcome for {label!r} must be yes/no")
                    continue
                if o.get("resolved_outcome"):
                    actions.append({"kind": "skip", "market": m, "entry": e,
                                    "why": f"{label} jau {o['resolved_outcome']}"})
                    continue
                actions.append({"kind": "option", "market": m, "entry": e, "option": o,
                                "outcome": outcome})
    return actions, errors


def _describe(a: dict) -> str:
    title = app_api.question_of(a["market"])
    if a["kind"] == "resolve":
        return f"{title} → {a['option']['label']}"
    if a["kind"] == "split":
        return f"{title} → po lygiai: {', '.join(o['label'] for o in a['options'])}"
    if a["kind"] == "option":
        word = "Taip" if a["outcome"] == "yes" else "Ne"
        return f"{title} · {a['option']['label']} → {word}"
    return f"{title} (praleista: {a['why']})"


def execute(a: dict) -> tuple[bool, str]:
    mid = app_api.market_id_of(a["market"])
    if a["kind"] == "resolve":
        return app_api.resolve_market(mid, a["option"]["id"])
    if a["kind"] == "split":
        return app_api.resolve_split(mid, [o["id"] for o in a["options"]])
    if a["kind"] == "option":
        return app_api.resolve_option(a["option"]["id"], a["outcome"])
    return True, "skip"


def run(path: str, *, dry_run: bool, alert: bool = True) -> tuple[list[dict], str]:
    entries = load(path)
    rows, err = app_api.markets(1000)
    if err:
        return [], f"app: {err}"
    actions, errors = plan(entries, rows)
    if errors:
        msg = "❌ Resolve failas atmestas, niekas nekeista:\n" + "\n".join(errors)
        if alert and not dry_run:
            notify.send(msg)
        return [], msg
    reports = []
    for a in actions:
        line = _describe(a)
        if a["kind"] == "skip" or dry_run:
            reports.append({"status": "skip" if a["kind"] == "skip" else "would-resolve",
                            "detail": line})
            continue
        ok, detail = execute(a)
        reports.append({"status": "resolved" if ok else "error",
                        "detail": line if ok else f"{line}: {detail}"})
    if alert and not dry_run and any(r["status"] != "skip" for r in reports):
        done = [r for r in reports if r["status"] == "resolved"]
        bad = [r for r in reports if r["status"] == "error"]
        lines = [f"🧾 Rinkų sprendimai ({Path(path).name}): išspręsta {len(done)}"
                 + (f", klaidų {len(bad)}" if bad else "")]
        lines += [f"✅ {r['detail']}" for r in done] + [f"❌ {r['detail']}" for r in bad]
        notify.send("\n".join(lines))
    return reports, ""
