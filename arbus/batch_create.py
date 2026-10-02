"""Create a prepared batch of markets from a JSON file (e.g. markets/2026-10.json).

Used for hand-curated batches (the monthly markets) that need the service_role
key, which lives only in GitHub Actions: the batch is reviewed in the repo, then
a workflow_dispatch run creates it. Every spec is validated first and nothing is
created if any spec is invalid; a market whose exact title already exists in the
app (and is not resolved) is skipped, so re-running the workflow cannot create
duplicates.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import app as app_api, notify

REQUIRED = ("title", "category", "image_url", "liquidity", "rules", "context",
            "options", "closes_at")


def load(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("batch file must hold a JSON list of market specs")
    return data


def validate(spec: dict, now: datetime | None = None) -> list[str]:
    """Problems with one spec ([] = valid)."""
    now = now or datetime.now(timezone.utc)
    errs = [f"missing {k}" for k in REQUIRED if spec.get(k) in (None, "", [])]
    if errs:
        return errs
    opts = spec["options"]
    if len(opts) < 2:
        errs.append("needs at least 2 options")
    labels = [str(o.get("label") or "").strip() for o in opts]
    if any(not label for label in labels):
        errs.append("an option has no label")
    if len(set(labels)) != len(labels):
        errs.append("duplicate option labels")
    probs = [o.get("probability") for o in opts]
    if any(not isinstance(p, (int, float)) or not 0 < p < 100 for p in probs):
        errs.append("every probability must be strictly between 0 and 100")
    elif abs(sum(probs) - 100) > 0.01:
        errs.append(f"probabilities sum to {sum(probs)}, not 100")
    try:
        closes = datetime.fromisoformat(str(spec["closes_at"]))
        if closes.tzinfo is None:
            errs.append("closes_at has no timezone")
        elif closes <= now:
            errs.append(f"closes_at {spec['closes_at']} is not in the future")
    except ValueError:
        errs.append(f"closes_at {spec['closes_at']!r} is not ISO 8601")
    if not str(spec["image_url"]).startswith("https://"):
        errs.append("image_url must be an https URL")
    if not isinstance(spec["liquidity"], int) or spec["liquidity"] <= 0:
        errs.append("liquidity must be a positive integer")
    return errs


def existing_titles(rows: list[dict]) -> set[str]:
    """Titles of app markets that are not resolved — a new one with the same
    title would be a duplicate."""
    return {app_api.question_of(r).strip() for r in rows
            if app_api.status_of(r) != "resolved"}


def run(path: str, *, dry_run: bool, alert: bool = True) -> tuple[list[dict], str]:
    specs = load(path)
    problems = {s.get("title", f"#{i}"): validate(s) for i, s in enumerate(specs)}
    bad = {t: e for t, e in problems.items() if e}
    if bad:
        lines = [f"{t}: {'; '.join(e)}" for t, e in bad.items()]
        return [], "batch invalid — nothing created:\n" + "\n".join(lines)
    rows, err = app_api.markets(500)
    if err:
        return [], f"cannot read the app: {err}"
    have = existing_titles(rows)
    reports = []
    for s in specs:
        title = s["title"].strip()
        if title in have:
            reports.append({"status": "skipped", "title": title, "detail": "already exists"})
            continue
        if dry_run:
            reports.append({"status": "would-create", "title": title,
                            "detail": " / ".join(f"{o['label']} {o['probability']}%"
                                                 for o in s["options"])})
            continue
        ok, detail = app_api.create_market(s)
        reports.append({"status": "created" if ok else "error", "title": title,
                        "detail": detail})
        if ok:
            have.add(title)
    if alert and not dry_run:
        created = [r for r in reports if r["status"] == "created"]
        errors = [r for r in reports if r["status"] == "error"]
        skipped = [r for r in reports if r["status"] == "skipped"]
        lines = [f"🆕 ĮKELTOS RINKOS ({Path(path).name}): {len(created)} sukurta, "
                 f"{len(skipped)} praleista, {len(errors)} klaidų", ""]
        lines += [f"✅ {r['title']}" for r in created]
        lines += [f"⏭️ {r['title']} (jau yra)" for r in skipped]
        lines += [f"❌ {r['title']}: {str(r['detail'])[:150]}" for r in errors]
        notify.send("\n".join(lines))
    return reports, ""
