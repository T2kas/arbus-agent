"""Market audit: what needs resolving, and which prices have gone stale.

Two layers, so the frequent run costs nothing:

  * FREE checks (every day) — pure data, no LLM:
      - a market past its close date and still unresolved;
      - a market still OPEN after its close date (anyone can still trade it);
      - a date-ladder option ("... iki...?") whose date has passed, unresolved;
      - the aurora market: GFZ Kp >= 6- (5.67) since the market opened means
        "Taip" is already decided (the 2026-10-31 storm went unnoticed).
  * AI review (Mondays, --llm) — every open long-running market, a few per web-
    search call: "is it already decided?" and "is the price still realistic?".
    It sees the market's live prices, its rules and any authoritative fact the
    resolvers can fetch (stock price, LEA fuel, LKC, AGATA). Markets the bots
    already run (daily weather/fuel, single games, weekly film/song) are skipped.

The AI decides nothing: the report goes to Telegram for the admins. Its cost is
measured and printed with the report.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from . import app as app_api, config, llm, notify, resolvers

log = logging.getLogger(__name__)

BATCH = 6                         # markets per AI call
STALE_PP = 15                     # flag when the AI's price differs by >= this
KP_DECIDED = 5.667                # GFZ "6-"
GFZ_URL = "https://kp.gfz.de/app/json/?start={start}&end={end}&index=Kp"


def _dt(s) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None


def _opts(m: dict) -> list[dict]:
    return sorted(m.get("market_options") or [], key=lambda o: o.get("sort_order") or 0)


def is_bot_managed(m: dict) -> bool:
    """Markets a bot already prices and resolves on its own."""
    t = app_api.question_of(m)
    return (m.get("category") == config.WEATHER_CATEGORY
            or " vs " in t
            or t.startswith("Vidutinė benzino A95 kaina Lietuvoje 2026 m.")
            or t.startswith("Vidutinė dyzelino kaina Lietuvoje 2026 m.")
            or t in ("Kuris filmas bus žiūrimiausias Lietuvoje?",
                     "Kuri daina bus klausomiausia Lietuvoje?"))


# ── free checks ─────────────────────────────────────────────────────────────

_LT_DATE = re.compile(r"(20\d\d)\s*m\.\s*(" + "|".join(resolvers._LT_MONTHS) + r")\s+(\d{1,2})\s*d", re.I)
_ISO_DATE = re.compile(r"^(20\d\d)-(\d\d)-(\d\d)$")


def option_deadline(label: str) -> datetime | None:
    m = _LT_DATE.search(label or "")
    if m:
        d = datetime(int(m[1]), resolvers._LT_MONTHS[m[2].lower()], int(m[3]), 23, 59)
    else:
        m = _ISO_DATE.match((label or "").strip())
        if not m:
            return None
        d = datetime(int(m[1]), int(m[2]), int(m[3]), 23, 59)
    return d.replace(tzinfo=timezone(timedelta(hours=2)))


def free_findings(rows: list[dict], now: datetime, grace_hours: int = 36) -> list[dict]:
    out = []
    for m in rows:
        status = app_api.status_of(m)
        if status in config.APP_SETTLED_STATUSES:
            continue
        title = app_api.question_of(m)
        closes = _dt(m.get("closes_at"))
        managed = is_bot_managed(m)
        if closes and closes < now:
            late = now - closes
            if status == "open":
                # trades_require_open_market refuses a buy after closes_at, so this
                # is untidy rather than exploitable — but it still needs a result.
                out.append({"level": "orange" if late < timedelta(days=7) else "red", "title": title,
                            "what": f"uždarymo data {closes:%Y-%m-%d} praėjo, rinka dar „open“, neišspręsta"})
            elif not managed or late > timedelta(hours=grace_hours):
                out.append({"level": "red" if late > timedelta(days=7) else "orange", "title": title,
                            "what": f"uždaryta, laukia sprendimo {late.days} d."})
        if (m.get("outcome_structure") or "") in ("cumulative_date", "survival"):
            for o in _opts(m):
                d = option_deadline(o.get("label") or "")
                if d and d + timedelta(hours=grace_hours) < now and not o.get("resolved_outcome"):
                    out.append({"level": "red", "title": title,
                                "what": f"data „{o['label']}“ praėjo, baigtis neišspręsta"})
    return out


def kp_max(start: datetime, end: datetime, fetch=requests.get) -> tuple[float, str] | None:
    url = GFZ_URL.format(start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                         end=end.strftime("%Y-%m-%dT%H:%M:%SZ"))
    try:
        data = fetch(url, timeout=30).json()
    except Exception as exc:                              # noqa: BLE001
        log.warning("GFZ Kp fetch failed: %s", exc)
        return None
    pairs = list(zip(data.get("datetime") or [], data.get("Kp") or []))
    if not pairs:
        return None
    t, kp = max(pairs, key=lambda p: p[1])
    return float(kp), t


def aurora_findings(rows: list[dict], now: datetime, fetch=requests.get) -> list[dict]:
    out = []
    for m in rows:
        if app_api.status_of(m) in config.APP_SETTLED_STATUSES:
            continue
        if "pašvaist" not in app_api.question_of(m).lower():
            continue
        start = _dt(m.get("created_at")) or now - timedelta(days=30)
        got = kp_max(start, now, fetch)
        if got and got[0] >= KP_DECIDED - 1e-3:
            open_opts = [o["label"] for o in _opts(m) if not o.get("resolved_outcome")]
            out.append({"level": "red", "title": app_api.question_of(m),
                        "what": (f"JAU AIŠKU: GFZ Kp {got[0]:.2f} ({got[1]}) ≥ 6− → „Taip“ visoms datoms "
                                 f"po jos: {', '.join(open_opts)}. Uždaryk ir išspręsk.")})
    return out


# ── AI review ───────────────────────────────────────────────────────────────

SYSTEM = (
    "Tu esi prognozių rinkų auditorius. Kiekvienai rinkai patikrink internete (lietuviški "
    "šaltiniai: LRT, 15min, Delfi, BNS, oficialūs puslapiai), ar rezultatas JAU aiškus pagal "
    "taisykles, ir ar dabartinės tikimybės realistiškos šiandien. Būk griežtas dėl datų: "
    "įvykis turi būti įvykęs rinkos laikotarpiu. Jei nežinai – sakyk, kad nežinai."
)


def _market_block(m: dict, now: datetime) -> str:
    opts = "; ".join(
        f"{o['label']} = {float(o.get('probability') or 0):.0f}%"
        + (f" (jau išspręsta: {o['resolved_outcome']})" if o.get("resolved_outcome") else "")
        for o in _opts(m))
    rules = re.sub(r"\s+", " ", m.get("rules") or "")[:1500]
    fact = resolvers.facts_for(app_api.question_of(m), str(m.get("closes_at") or ""), m.get("rules") or "")
    return (f"ID: {app_api.market_id_of(m)}\nKlausimas: {app_api.question_of(m)}\n"
            f"Struktūra: {m.get('outcome_structure')}\nUždaroma: {m.get('closes_at') or 'pagal datas baigtyse'}\n"
            f"Dabartinės tikimybės: {opts}\nTaisyklės: {rules}"
            + (f"\nOficialūs duomenys (patikimi): {fact}" if fact else ""))


def _prompt(batch: list[dict], now: datetime) -> str:
    blocks = "\n\n---\n\n".join(_market_block(m, now) for m in batch)
    return (
        f"Šiandien {now:%Y-%m-%d}. Patikrink šias {len(batch)} rinkas.\n\n{blocks}\n\n"
        "Atsakyk TIK JSON masyvu, po vieną objektą kiekvienai rinkai:\n"
        '[{"id": "...", "decided": true|false, "outcome": "laimėjusi baigtis arba null", '
        '"source": "URL arba null", "fair": {"<baigtis>": <realistiška % šiandien>, ...}, '
        '"note": "vienas sakinys lietuviškai: kas pasikeitė / kodėl"}]\n'
        "„fair“ – tik neišspręstoms baigtims, sveiki procentai. „decided“ = true tik jei "
        "rezultatas pagal taisykles jau neabejotinai žinomas ir turi šaltinį."
    )


def _parse(text: str) -> list[dict]:
    m = re.search(r"\[[\s\S]*\]", text or "")
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return []
    return [d for d in data if isinstance(d, dict) and d.get("id")]


def ai_findings(rows: list[dict], now: datetime, research=None, limit: int | None = None
                ) -> tuple[list[dict], float]:
    research = research or (lambda p: llm.research(p, system=SYSTEM, max_uses=10,
                                                   max_tokens=8000, stage="audit"))
    todo = [m for m in rows if app_api.status_of(m) not in config.APP_SETTLED_STATUSES
            and not is_bot_managed(m)]
    if limit:
        todo = todo[:limit]
    by_id = {app_api.market_id_of(m): m for m in todo}
    llm.reset_usage()
    out = []
    for i in range(0, len(todo), BATCH):
        batch = todo[i:i + BATCH]
        try:
            answers = _parse(research(_prompt(batch, now)))
        except Exception as exc:                          # noqa: BLE001
            log.warning("audit batch failed: %s", exc)
            continue
        for a in answers:
            m = by_id.get(str(a["id"]))
            if not m:
                continue
            title = app_api.question_of(m)
            if a.get("decided"):
                out.append({"level": "red", "title": title,
                            "what": f"AI: jau aišku → {a.get('outcome')} ({a.get('source') or 'be šaltinio'}). {a.get('note') or ''}"})
                continue
            fair = a.get("fair") or {}
            diffs = []
            for o in _opts(m):
                if o.get("resolved_outcome") or o["label"] not in fair:
                    continue
                try:
                    f = float(fair[o["label"]])
                except (TypeError, ValueError):
                    continue
                cur = float(o.get("probability") or 0)
                if abs(f - cur) >= STALE_PP:
                    diffs.append(f"{o['label']} {cur:.0f}% → ~{f:.0f}%")
            if diffs:
                out.append({"level": "orange", "title": title,
                            "what": "pasenusi kaina: " + "; ".join(diffs) + (f". {a['note']}" if a.get("note") else "")})
    return out, llm.usage_cost_eur()


# ── report ──────────────────────────────────────────────────────────────────

def report(findings: list[dict], cost: float | None, checked: int) -> str:
    red = [f for f in findings if f["level"] == "red"]
    orange = [f for f in findings if f["level"] == "orange"]
    lines = [f"🔎 Rinkų patikra: {len(red)} reikia veiksmo, {len(orange)} verta pažiūrėti "
             f"(peržiūrėta {checked})"]
    if red:
        lines += ["", "🔴 REIKIA VEIKSMO"] + [f"· {f['title']}\n  {f['what']}" for f in red]
    if orange:
        lines += ["", "🟠 PAŽIŪRĖTI"] + [f"· {f['title']}\n  {f['what']}" for f in orange]
    if not red and not orange:
        lines.append("✅ Viskas tvarkoj.")
    if cost is not None:
        lines += ["", f"💶 AI patikros kaina ~{cost:.2f} €"]
    return "\n".join(lines)


def run(*, use_llm: bool, alert: bool = True, limit: int | None = None,
        now: datetime | None = None) -> tuple[str, list[dict]]:
    now = now or datetime.now(timezone.utc)
    rows, err = app_api.markets(1000)
    if err:
        return f"app: {err}", []
    findings = free_findings(rows, now) + aurora_findings(rows, now)
    cost = None
    if use_llm:
        ai, cost = ai_findings(rows, now, limit=limit)
        findings += ai
    seen, uniq = set(), []
    for f in findings:
        key = (f["title"], f["what"])
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    checked = sum(1 for m in rows if app_api.status_of(m) not in config.APP_SETTLED_STATUSES)
    text = report(uniq, cost, checked)
    # The daily free run stays quiet when there is nothing to do.
    if alert and (use_llm or any(f["level"] == "red" for f in uniq)):
        notify.send(text)
    return text, uniq
