"""Recurring weekly markets that create themselves one after another.

Two "series" so far, both resolved by the EXISTING resolvers that already run in
`weather.run` every pass:

  • cinema — "Kuris filmas bus žiūrimiausias Lietuvoje?" (LKC weekly ADM report)
  • music  — "Kuri daina bus klausomiausia Lietuvoje?"   (AGATA weekly singles #1)

Only the CREATION side lives here. The lifecycle is deliberately serial, the way
the team asked for: at most one live market per series, and the next week's
market is created only once the current one has resolved. So this module never
runs ahead of itself — a run that finds a live market of a series does nothing
for that series and spends nothing.

Unlike the weather markets (a pure forecast→bucket formula), a "most-watched"
market's outcomes are specific films/songs, so the options and their starting
probabilities come from a small web-grounded LLM research call (`llm.research` +
`llm.structure`). The call is metered and its EUR cost is reported to Telegram,
because it is the one recurring spend here.

The RULES text is built deterministically (not by the model): the resolvers read
the evaluation period straight out of the rules, so the exact date span / week
number must be ours, phrased the way `resolvers._cinema_period` /
`resolvers._agata_target` parse it. The model only fills the option list and a
short context paragraph.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from . import app as app_api, config, llm, notify, resolvers
from .resolvers import _cnorm, _LKC_REPORTS_URL
from .weather import _LT_MONTH_GEN

log = logging.getLogger(__name__)

_AGATA_TOP_URL = "https://www.agata.lt/lt/naujienos/?cat=top-100"


# ── LLM output contract ──────────────────────────────────────────────────────

class SeriesOption(BaseModel):
    label: str = Field(description="Outcome label in Lithuanian (a film title or "
                                   "an 'Artist – „Song“' string)")
    probability: float = Field(description="Chance this outcome wins, 0-100")


class SeriesDraft(BaseModel):
    options: list[SeriesOption] = Field(
        description="3-5 most likely named outcomes plus the catch-all bucket")
    context: str = Field(description="1-2 short Lithuanian sentences of context")


# ── probability helpers ──────────────────────────────────────────────────────

def _pct_ints(weights: list[float]) -> list[int]:
    """Integer percentages, each ≥1, summing to exactly 100 — the app requires
    every option in a 3+ way market to be strictly between 0 and 100."""
    total = sum(weights) or 1.0
    exact = [max(w, 0.0) / total * 100 for w in weights]
    pct = [max(int(math.floor(x)), 1) for x in exact]
    order = sorted(range(len(exact)),
                   key=lambda i: exact[i] - math.floor(exact[i]), reverse=True)
    i = 0
    while sum(pct) < 100:
        pct[order[i % len(order)]] += 1
        i += 1
    while sum(pct) > 100:
        big = max(range(len(pct)), key=lambda k: pct[k])
        if pct[big] <= 1:
            break
        pct[big] -= 1
    return pct


def _is_catch_all(label: str) -> bool:
    n = _cnorm(label)
    return n.startswith("kitas") or n.startswith("kita ") or n.startswith("kito")


def finalize_options(draft: SeriesDraft, catch_all_label: str,
                     max_named: int = 5) -> list[dict] | None:
    """Clean the model's options into a valid app option list: dedupe, cap the
    named ones, guarantee exactly one catch-all, normalise to integer % summing
    to 100. Returns None if fewer than two distinct named outcomes came back
    (too thin to be an interesting market)."""
    named: list[tuple[str, float]] = []
    catch_weight: float | None = None
    seen: set[str] = set()
    for o in draft.options:
        label = (o.label or "").strip().strip("—-·").strip()
        key = _cnorm(label)
        if not label or key in seen:
            continue
        seen.add(key)
        if _is_catch_all(label):
            catch_weight = max(float(o.probability), 1.0)
            continue
        named.append((label, max(float(o.probability), 0.5)))
    if len(named) < 2:
        return None
    named = named[:max_named]
    # Give the catch-all a sensible default when the model omitted it: a bit
    # below the weakest named outcome, so an unlisted winner is a real tail risk
    # but not the favourite.
    if catch_weight is None:
        catch_weight = max(min(w for _, w in named) * 0.6, 3.0)
    items = named + [(catch_all_label, catch_weight)]
    pcts = _pct_ints([w for _, w in items])
    return [{"label": label, "probability": p}
            for (label, _), p in zip(items, pcts)]


# ── period maths ─────────────────────────────────────────────────────────────

def _tz() -> ZoneInfo:
    return ZoneInfo(config.WEATHER_TZ)


def _lt_span_text(s: date, e: date) -> str:
    """'rugsėjo 15 d. – rugsėjo 21 d.' — the month is repeated on BOTH days on
    purpose: resolvers._cinema_period parses the span from two `(month day)`
    pairs, so a shared-month shorthand ('rugsėjo 15 – 21 d.') would expose only
    one pair and the market would never resolve."""
    return (f"{_LT_MONTH_GEN[s.month]} {s.day} d. – "
            f"{_LT_MONTH_GEN[e.month]} {e.day} d.")


def cinema_target_span(today: date) -> tuple[str, str] | None:
    """(start_iso, end_iso) of the cinema week to open a market for — the running
    week after the latest PUBLISHED LKC report, rolled forward to contain today
    so a backlog does not open a stale week. Falls back to today's Mon–Sun."""
    wk = resolvers.latest_lkc_week()
    if wk:
        s = date.fromisoformat(wk[0]) + timedelta(days=7)
        e = date.fromisoformat(wk[1]) + timedelta(days=7)
    else:
        iso = today.isocalendar()
        s = date.fromisocalendar(iso[0], iso[1], 1)
        e = s + timedelta(days=6)
    while e < today:                                    # catch up to the live week
        s += timedelta(days=7)
        e += timedelta(days=7)
    return s.isoformat(), e.isoformat()


def music_target_week(today: date) -> tuple[int, int]:
    """(ISO year, ISO week) of the running week — the AGATA chart to be published."""
    iso = today.isocalendar()
    return iso[0], iso[1]


# ── rules text (deterministic; the resolvers parse the period from here) ──────

def cinema_rules(start_iso: str, end_iso: str) -> str:
    s, e = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    return (
        "Rinka sprendžiama pagal Lietuvos kino centro (LKC) savaitės TOP ("
        f"Weekly) ataskaitą už {s.year} m. {_lt_span_text(s, e)} laikotarpį "
        "(Lietuvos laiku).\n\n"
        "Laimi filmas, kurį tą savaitę Lietuvos kino teatruose pamatė daugiausiai "
        "žiūrovų pagal LKC stulpelį „Žiūrovų skaičius (ADM)“ (ne bendras/Total "
        "kaupiamasis skaičius). Jei daugiausiai žiūrovų surinkęs filmas nėra tarp "
        "išvardintų variantų, laimi „Kitas filmas“.\n\n"
        "Įskaitomi visi tą savaitę rodyti filmai (nepriklausomai nuo kilmės šalies). "
        "Jei dvi juostos surenka vienodai žiūrovų arba ataskaita laiku nepaskelbiama, "
        "rinką rankiniu būdu sprendžia administratorius pagal pirmą oficialią LKC "
        f"ataskaitą. Rezultato šaltinis — Lietuvos kino centras ({_LKC_REPORTS_URL})."
    )


def music_rules(year: int, week: int, start_iso: str, end_iso: str) -> str:
    s, e = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    return (
        f"Rinka sprendžiama pagal AGATA {year} m. {week}-os savaitės klausomiausių "
        f"kūrinių sąrašą (SINGLŲ TOP 100) už {s.year} m. {_lt_span_text(s, e)} "
        "laikotarpį (Lietuvos laiku).\n\n"
        "Laimi daina, kuri tą savaitę AGATA SINGLŲ TOP 100 sąraše užima pirmą "
        "(Nr. 1) vietą. Vertinamas būtent singlų, o ne albumų sąrašas. Jei Nr. 1 "
        "daina nėra tarp išvardintų variantų, laimi „Kita daina“.\n\n"
        "Jei sąrašas laiku nepaskelbiamas arba kyla neaiškumų, rinką rankiniu būdu "
        "sprendžia administratorius pagal pirmą oficialų AGATA sąrašą. Rezultato "
        f"šaltinis — AGATA ({_AGATA_TOP_URL})."
    )


# ── the series registry ──────────────────────────────────────────────────────

def _cinema_research(start_iso: str, end_iso: str) -> str:
    s, e = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    span = f"{s.year} m. {_lt_span_text(s, e)}"
    return (
        "Tu esi „Arbus“ prognozių rinkų analitikas. Reikia paruošti rinką "
        "„Kuris filmas bus žiūrimiausias Lietuvoje?“ konkrečiai savaitei: "
        f"{span} (nuo {start_iso} iki {end_iso}).\n\n"
        "Atlik dabartinę interneto paiešką (LKC ataskaitos, kino.lt, "
        "forumcinemas.lt, multikino.lt repertuaras ir naujos premjeros) ir "
        "nustatyk, kurie filmai tą savaitę bus rodomi Lietuvos kino teatruose ir "
        "kuris surinks daugiausiai žiūrovų pagal LKC (ADM).\n\n"
        "Pateik 3–5 realiausius filmus (lietuviškais pavadinimais, kaip jie "
        "vadinami Lietuvos repertuare) su tikimybėmis (procentais, iš viso ~100 su "
        "„Kitas filmas“), pridėk baigtį „Kitas filmas“, ir 1–2 trumpus lietuviškus "
        "konteksto sakinius, kodėl būtent tokie favoritai. Nesvarbu kilmės šalis — "
        "įskaitomi visi filmai. Nekurk neegzistuojančių filmų."
    )


def _music_research(year: int, week: int, start_iso: str, end_iso: str) -> str:
    s, e = date.fromisoformat(start_iso), date.fromisoformat(end_iso)
    span = f"{s.year} m. {_lt_span_text(s, e)}"
    return (
        "Tu esi „Arbus“ prognozių rinkų analitikas. Reikia paruošti rinką "
        "„Kuri daina bus klausomiausia Lietuvoje?“ konkrečiai savaitei: "
        f"{year} m. {week}-a savaitė ({span}).\n\n"
        "Atlik dabartinę interneto paiešką (AGATA SINGLŲ TOP 100 naujausi sąrašai "
        "agata.lt, taip pat Spotify Lietuva Top 50, YouTube/radijo grojimai) ir "
        "nustatyk, kuri daina tą savaitę bus AGATA singlų sąrašo Nr. 1.\n\n"
        "Pateik 3–5 realiausius kandidatus formatu „Atlikėjas – „Dainos "
        "pavadinimas““ su tikimybėmis (procentais, iš viso ~100 su „Kita daina“), "
        "pridėk baigtį „Kita daina“, ir 1–2 trumpus lietuviškus konteksto sakinius. "
        "Vertink SINGLUS, ne albumus. Nekurk neegzistuojančių dainų."
    )


# Each series: how to detect its markets in the app, the target period, the
# research prompt, rules/titles, and the catch-all outcome label.
def _cinema_series() -> dict:
    return {
        "key": "cinema",
        "label": "Kino rinka",
        "marker": "ziurimiausias",                       # _cnorm of the title word
        "catch_all": "Kitas filmas",
        "category": config.SERIES_CINEMA_CATEGORY,
        "liquidity": config.SERIES_CINEMA_LIQUIDITY,
        "image_default": config.SERIES_CINEMA_IMAGE,
        "title": "Kuris filmas bus žiūrimiausias Lietuvoje?",
        "enabled": config.SERIES_CINEMA_ENABLED,
    }


def _music_series() -> dict:
    return {
        "key": "music",
        "label": "Muzikos rinka",
        "marker": "klausomiausia",
        "catch_all": "Kita daina",
        "category": config.SERIES_MUSIC_CATEGORY,
        "liquidity": config.SERIES_MUSIC_LIQUIDITY,
        "image_default": config.SERIES_MUSIC_IMAGE,
        "title": "Kuri daina bus klausomiausia Lietuvoje?",
        "enabled": config.SERIES_MUSIC_ENABLED,
    }


def series_registry() -> list[dict]:
    return [_cinema_series(), _music_series()]


# ── reading the app for this series ──────────────────────────────────────────

def _is_series_row(row: dict, series: dict) -> bool:
    return series["marker"] in _cnorm(app_api.question_of(row))


def series_period_of(row: dict, series: dict) -> tuple | None:
    """The evaluation period a given series market is about, so a run can tell
    whether the target period already has a market."""
    q, rules = app_api.question_of(row), str(row.get("rules") or "")
    if series["key"] == "cinema":
        p = resolvers._cinema_period(q, rules)
        return (p[1], p[2]) if p and p[0] == "weekly" else None
    return resolvers._agata_target(q, rules)


def _has_live(rows: list[dict], series: dict) -> bool:
    """True if a market of this series is still open (not resolved, not settled)
    — the serial gate that stops us running a week ahead."""
    for r in rows:
        if not _is_series_row(r, series):
            continue
        if app_api.winning_option_of(r):
            continue
        if app_api.status_of(r) in config.APP_SETTLED_STATUSES:
            continue
        return True
    return False


def _newest_image(rows: list[dict], series: dict) -> str:
    for r in rows:                                      # markets() returns newest-first
        if _is_series_row(r, series):
            img = str(r.get("image_url") or "")
            if img:
                return img
    return series["image_default"]


def target_period(series: dict, today: date) -> tuple | None:
    if series["key"] == "cinema":
        return cinema_target_span(today)
    return music_target_week(today)


# ── building one market spec via research ────────────────────────────────────

def _week_bounds(series: dict, period: tuple) -> tuple[str, str]:
    if series["key"] == "cinema":
        return period[0], period[1]
    year, week = period
    mon = date.fromisocalendar(year, week, 1)
    return mon.isoformat(), (mon + timedelta(days=6)).isoformat()


def build_spec(series: dict, period: tuple, image: str,
               tz: ZoneInfo) -> tuple[dict | None, dict]:
    """Research the options and assemble the create payload for one series+period.
    Returns (spec | None, meta). `meta` always carries provider/model/cost so the
    spend is reported even when the draft is too thin to use."""
    start_iso, end_iso = _week_bounds(series, period)
    if series["key"] == "cinema":
        prompt = _cinema_research(start_iso, end_iso)
        rules = cinema_rules(start_iso, end_iso)
        subtitle = f"{date.fromisoformat(start_iso).year} m. " \
                   f"{_lt_span_text(date.fromisoformat(start_iso), date.fromisoformat(end_iso))}"
    else:
        year, week = period
        prompt = _music_research(year, week, start_iso, end_iso)
        rules = music_rules(year, week, start_iso, end_iso)
        subtitle = f"{year} m. {week} savaitė"

    llm.reset_usage()
    system = ("Rašyk aiškia lietuvių kalba, naudok lietuviškas kabutes, remkis tik "
              "realiais, patikrinamais faktais ir nekurk neegzistuojančių pavadinimų.")
    try:
        text = llm.research(prompt, system=system,
                            max_uses=config.SERIES_RESEARCH_SEARCHES,
                            max_tokens=config.SERIES_RESEARCH_MAX_TOKENS, stage="draft")
        draft = llm.structure(text, SeriesDraft)
    except Exception as exc:                            # noqa: BLE001 — never crash a run
        log.warning("series %s research failed: %s", series["key"], exc)
        return None, _meta(f"tyrimas nepavyko: {exc}")

    meta = _meta("")
    options = finalize_options(draft, series["catch_all"])
    if not options:
        meta["reason"] = "per mažai aiškių variantų (tyrimas negrąžino kandidatų)"
        return None, meta

    close_dt = datetime.combine(date.fromisoformat(end_iso),
                                dtime(hour=config.SERIES_CLOSE_HOUR,
                                      minute=config.SERIES_CLOSE_MINUTE), tzinfo=tz)
    spec = {
        "series": series["key"],
        "period": period,
        "title": series["title"],
        "subtitle": subtitle,
        "category": series["category"],
        "image_url": image,
        "liquidity": series["liquidity"],
        "rules": rules,
        "context": (draft.context or "").strip(),
        "options": options,
        "closes_at": close_dt.isoformat(),
    }
    return spec, meta


def _meta(reason: str) -> dict:
    snap = llm.usage_snapshot()
    prov = llm._USAGE_PROVIDER or ""
    return {
        "provider": prov,
        "model": llm.model_for(prov) if prov else "",
        "cost_eur": llm.usage_cost_eur(snap, prov) if any(snap.values()) else 0.0,
        "reason": reason,
    }


# ── planning + creating ──────────────────────────────────────────────────────

def plan(rows: list[dict], today: date) -> list[dict]:
    """Which series need a new market this run. Serial: a series with a live
    market, or whose target period already exists, is skipped."""
    todo = []
    for series in series_registry():
        if not series["enabled"]:
            continue
        if _has_live(rows, series):
            continue
        period = target_period(series, today)
        if period is None:
            continue
        existing = [r for r in rows if _is_series_row(r, series)]
        if any(series_period_of(r, series) == period for r in existing):
            continue
        todo.append({"series": series, "period": period,
                     "image": _newest_image(rows, series)})
    return todo


def _created_message(series: dict, spec: dict, meta: dict, market_id: str) -> str:
    opts = "\n".join(f"   {o['probability']:>3}%  {o['label']}" for o in spec["options"])
    cost = meta.get("cost_eur") or 0.0
    cost_line = (f"  💶 idėjų kaina ~{cost:.2f} € ({meta.get('model') or '?'})"
                 if cost else "")
    lines = [
        f"🆕 SUKURTA SAVAITĖS RINKA — {series['label']}",
        "",
        f"· {spec['title']}",
        f"  {spec['subtitle']} | likvidumas {spec['liquidity']} | "
        f"uždaroma {spec['closes_at'][:16].replace('T', ' ')}",
        opts,
    ]
    if cost_line:
        lines.append(cost_line)
    return "\n".join(lines)


def create(todo: list[dict], tz: ZoneInfo, *, alert: bool,
           do_create: bool) -> list[dict]:
    reports = []
    for item in todo:
        series, period, image = item["series"], item["period"], item["image"]
        spec, meta = build_spec(series, period, image, tz)
        if spec is None:
            if alert and meta.get("reason"):
                notify.send(f"⚠️ {series['label']}: rinka nesukurta — {meta['reason']}")
            reports.append({"status": "error", "series": series["key"],
                            "period": period, "reason": meta.get("reason", "no spec"),
                            "cost_eur": meta.get("cost_eur", 0.0)})
            continue
        if not do_create:
            reports.append({"status": "would-create", "series": series["key"],
                            "period": period, "options": spec["options"],
                            "title": spec["title"], "cost_eur": meta.get("cost_eur", 0.0),
                            "context": spec["context"]})
            continue
        ok, detail = app_api.create_market(spec)
        if ok and alert:
            notify.send(_created_message(series, spec, meta, detail))
        elif not ok and alert:
            notify.send(f"⚠️ {series['label']}: nepavyko sukurti rinkos — {detail[:180]}")
        reports.append({"status": "created" if ok else "error", "series": series["key"],
                        "period": period, "options": spec["options"],
                        "title": spec["title"], "detail": detail,
                        "cost_eur": meta.get("cost_eur", 0.0)})
    return reports


def run(now: datetime | None = None, *, alert: bool = True,
        do_create: bool = True, limit: int = 200) -> tuple[list[dict], str]:
    """One pass: create the next weekly market for any series that has none live.
    Resolution is handled elsewhere (weather.run's cinema/music resolvers)."""
    now = now or datetime.now(timezone.utc)
    if not config.ARBUS_API_URL:
        return [], "ARBUS_API_URL nenustatytas — nėra ką skaityti."
    try:
        tz = _tz()
    except Exception as exc:                            # noqa: BLE001
        return [], f"laiko juostos '{config.WEATHER_TZ}' nėra: {exc}"
    rows, err = app_api.markets(limit)
    if err:
        return [], err
    today = now.astimezone(tz).date()
    todo = plan(rows, today)
    if not todo:
        return [], ""
    return create(todo, tz, alert=alert, do_create=do_create), ""
