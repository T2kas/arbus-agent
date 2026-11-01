"""Period fuel-price markets, resolved from the LEA daily averages.

Two shapes the daily fuel bot (fuel.py) leaves alone:

  * period MAXIMUM — "Aukščiausia vidutinė dyzelino kaina spalį?" / "... 2026 m."
    (price buckets, rules "nuo 2026 m. spalio 1 d. iki spalio 31 d. imtinai").
    While the period runs, every bucket entirely below the running maximum can
    no longer win and is eliminated ("Ne"); once the period is over and its last
    bulletin is final, the bucket holding the maximum wins.
  * THRESHOLD ladder — "Vidutinė dyzelino kaina Lietuvoje pasieks 2,40 €/l
    iki...?" (one Taip/Ne per date, counted "nuo 2026 m. rugsėjo 23 d."). The
    first day at/over the threshold settles every date on/after it "Taip"; a
    date whose whole window passed below it settles "Ne".

The prices come from the same dated LEA bulletins as the daily markets (the date
is read from the bulletin URL, never guessed) and are cached in the weather
state file, so each day's bulletin is fetched once, not every five minutes.

A value counts only once it is final under the rules ("paskelbta arba pataisyta
iki kitos kalendorinės dienos 12:00"): a day is used after 12:00 the next day.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from . import app as app_api, config, notify
from .fuel import (bucket_for_price, buckets_from_market, is_publication_day,
                   lea_price_for_date, _fmt, _prices)
from .resolvers import _LT_MONTHS

log = logging.getLogger(__name__)

_MONTH_RE = "|".join(_LT_MONTHS)
# "nuo 2026 m. spalio 1 d. iki spalio 31 d." / "... iki 2026 m. gruodžio 31 d."
_PERIOD_RE = re.compile(
    rf"nuo\s+(20\d\d)\s*m\.\s*({_MONTH_RE})\s+(\d{{1,2}})\s*d\.?\s+iki\s+"
    rf"(?:(20\d\d)\s*m\.\s*)?({_MONTH_RE})\s+(\d{{1,2}})\s*d", re.I)
_START_RE = re.compile(rf"nuo\s+(20\d\d)\s*m\.\s*({_MONTH_RE})\s+(\d{{1,2}})\s*d", re.I)
_DATE_LABEL_RE = re.compile(rf"(20\d\d)\s*m\.\s*({_MONTH_RE})\s+(\d{{1,2}})\s*d", re.I)
_THRESHOLD_RE = re.compile(r"pasieks\s+(\d+(?:[.,]\d+)?)\s*€\s*/\s*l", re.I)

MAX_FETCHES_PER_RUN = 30         # first run back-fills a period; later runs fetch ~1


def _d(y, mo, d) -> date:
    return date(int(y), _LT_MONTHS[mo.lower()], int(d))


def fuel_of(title: str) -> str | None:
    t = title.lower()
    if "dyzel" in t:
        return "dyzelinas"
    if "benzin" in t or "a95" in t:
        return "benzinas"
    return None


def classify(market: dict) -> dict | None:
    """{kind: 'max'|'threshold', fuel, start, end?, threshold?} or None."""
    title = app_api.question_of(market)
    fuel = fuel_of(title)
    if not fuel:
        return None
    rules = market.get("rules") or ""
    if title.lower().startswith("aukščiausia vidutinė"):
        m = _PERIOD_RE.search(rules)
        if not m:
            return {"kind": "max", "fuel": fuel, "error": "taisyklėse nerastas laikotarpis „nuo … iki …“"}
        start = _d(m[1], m[2], m[3])
        end = _d(m[4] or m[1], m[5], m[6])
        return {"kind": "max", "fuel": fuel, "start": start, "end": end}
    th = _THRESHOLD_RE.search(title)
    if th:
        m = _START_RE.search(rules)
        if not m:
            return {"kind": "threshold", "fuel": fuel, "error": "taisyklėse nerasta pradžios data"}
        return {"kind": "threshold", "fuel": fuel, "start": _d(m[1], m[2], m[3]),
                "threshold": float(th[1].replace(",", "."))}
    return None


def option_date(label: str) -> date | None:
    m = _DATE_LABEL_RE.search(label or "")
    return _d(m[1], m[2], m[3]) if m else None


def final_cutoff(day: date, tz: ZoneInfo) -> datetime:
    """When `day`'s value becomes final: 12:00 the next calendar day."""
    return datetime.combine(day + timedelta(days=1), dtime(12, 0), tzinfo=tz)


# ── price cache (kept in the weather state) ─────────────────────────────────

def cached_prices(state: dict, fuel: str, start: date, end: date, now: datetime,
                  tz: ZoneInfo, budget: list[int], fetch=lea_price_for_date) -> dict[date, float]:
    """{day: price} for publication days in [start, end] whose value is FINAL
    (see final_cutoff). Fetches only what the cache lacks, within `budget`."""
    cache = state.setdefault("_lea_prices", {}).setdefault(fuel, {})
    gone = state.setdefault("_lea_missing", {}).setdefault(fuel, [])
    out: dict[date, float] = {}
    d = start
    while d <= end:
        if is_publication_day(d) and now >= final_cutoff(d, tz):
            iso = d.isoformat()
            if iso in cache:
                out[d] = cache[iso]
            elif iso not in gone and budget[0] > 0:
                budget[0] -= 1
                try:
                    price, _url = fetch(iso, fuel)
                except Exception as exc:                 # noqa: BLE001
                    log.warning("LEA %s %s: %s", fuel, iso, exc)
                    price = None
                if price is not None:
                    cache[iso] = price
                    out[d] = price
                elif now >= final_cutoff(d + timedelta(days=3), tz):
                    gone.append(iso)                     # LEA skipped this day for good
        d += timedelta(days=1)
    return out


def complete_through(state: dict, fuel: str, start: date, end: date) -> bool:
    """Every publication day in [start, end] is cached or known-missing."""
    cache = state.get("_lea_prices", {}).get(fuel, {})
    gone = set(state.get("_lea_missing", {}).get(fuel, []))
    d = start
    while d <= end:
        if is_publication_day(d) and d.isoformat() not in cache and d.isoformat() not in gone:
            return False
        d += timedelta(days=1)
    return True


# ── decisions (pure) ────────────────────────────────────────────────────────

def max_decision(buckets: list[dict], prices: dict[date, float], period_done: bool
                 ) -> tuple[dict | None, list[dict], float | None]:
    """(winner bucket or None, buckets to eliminate now, running max)."""
    if not prices:
        return None, [], None
    top = max(prices.values())
    if period_done:
        return bucket_for_price(buckets, top), [], top
    dead = [b for b in buckets if b["high"] is not None and b["high"] < top]
    return None, dead, top


def threshold_decisions(options: list[dict], prices: dict[date, float], threshold: float,
                        state: dict, fuel: str, start: date, now: datetime, tz: ZoneInfo
                        ) -> tuple[list[tuple[dict, str]], date | None]:
    """[(option, 'yes'|'no')] for open ladder options that are decided now."""
    hit = min((d for d, p in prices.items() if p >= threshold - 1e-9), default=None)
    out = []
    for o in options:
        if o.get("resolved_outcome"):
            continue
        od = option_date(o.get("label") or "")
        if not od:
            continue
        if hit and hit <= od:
            out.append((o, "yes"))
        elif (now >= final_cutoff(od, tz)
              and complete_through(state, fuel, start, od)
              and not any(d <= od and p >= threshold - 1e-9 for d, p in prices.items())):
            out.append((o, "no"))
    return out, hit


# ── the pass ────────────────────────────────────────────────────────────────

def _send(alert: bool, text: str) -> None:
    if alert:
        notify.send(text)


def resolve_markets(rows: list[dict], now: datetime, tz: ZoneInfo, state: dict,
                    *, alert: bool, do_resolve: bool, fetch=lea_price_for_date
                    ) -> tuple[list[dict], bool]:
    reports, changed = [], False
    budget = [MAX_FETCHES_PER_RUN]
    today = now.astimezone(tz).date()
    for m in rows:
        if app_api.status_of(m) in config.APP_SETTLED_STATUSES or app_api.winning_option_of(m):
            continue
        spec = classify(m)
        if not spec:
            continue
        mid = app_api.market_id_of(m)
        title = app_api.question_of(m)
        st = state.setdefault(mid, {})
        if spec.get("error"):
            if not st.get("period_alerted"):
                _send(alert, f"⚠️ DEGALŲ RINKA — negaliu automatiškai išspręsti\n\n· {title}\n  {spec['error']}. "
                             "Pataisyk taisykles app'e arba spręsk rankiniu būdu.")
                st["period_alerted"] = True
                changed = True
            reports.append({"status": "error", "market_id": mid, "reason": spec["error"]})
            continue
        fuel, start = spec["fuel"], spec["start"]
        before = len(str(state.get("_lea_prices")))
        if spec["kind"] == "max":
            end = spec["end"]
            prices = cached_prices(state, fuel, start, min(end, today), now, tz, budget, fetch)
            done = now >= final_cutoff(end, tz) and complete_through(state, fuel, start, end)
            buckets = buckets_from_market(m)
            open_ids = {str(o["id"]) for o in m.get("market_options") or [] if not o.get("resolved_outcome")}
            winner, dead, top = max_decision(buckets, prices, done)
            dead = [b for b in dead if b["option_id"] in open_ids]
            if winner and do_resolve:
                ok, detail = app_api.resolve_market(mid, winner["option_id"])
                reports.append({"status": "resolved" if ok else "error", "market_id": mid,
                                "option": winner["label"], "via": "fuel-max"})
                if ok:
                    st.update({"resolved": True, "resolved_label": winner["label"], "resolved_price": top})
                    _send(alert, f"⛽ DEGALŲ RINKA IŠSPRĘSTA\n\n· {title}\n  📈 Laikotarpio maksimumas: "
                                 f"{_fmt(top)} €/l\n  🏆 Laimi: {winner['label']}")
                elif not st.get("fail_alerted"):
                    _send(alert, f"⚠️ DEGALŲ RINKA: nepavyko resolvinti ({detail[:150]}) — {title}")
                    st["fail_alerted"] = True
                changed = True
            elif winner:
                reports.append({"status": "would-resolve", "market_id": mid, "option": winner["label"],
                                "note": f"max {_fmt(top)} €/l"})
            elif dead and do_resolve and config.WEATHER_ELIMINATE:
                ok, detail = app_api.eliminate_options(mid, [b["option_id"] for b in dead])
                labels = ", ".join(b["label"] for b in dead)
                reports.append({"status": "eliminated" if ok else "error", "market_id": mid,
                                "option": labels})
                if ok:
                    _send(alert, f"⛽ {title}\n  Kaina jau pasiekė {_fmt(top)} €/l — atmesta: {labels}")
                elif not st.get("fail_alerted"):
                    _send(alert, f"⚠️ {title}: nepavyko atmesti {labels} ({detail[:150]})")
                    st["fail_alerted"] = True
                changed = True
            elif top is not None:
                reports.append({"status": "waiting", "market_id": mid,
                                "note": f"max kol kas {_fmt(top)} €/l"})
        else:
            th = spec["threshold"]
            last = max((option_date(o.get("label") or "") or start) for o in m.get("market_options") or [])
            prices = cached_prices(state, fuel, start, min(last, today), now, tz, budget, fetch)
            decided, hit = threshold_decisions(m.get("market_options") or [], prices, th,
                                               state, fuel, start, now, tz)
            for o, outcome in decided:
                word = "Taip" if outcome == "yes" else "Ne"
                if not do_resolve:
                    reports.append({"status": "would-resolve", "market_id": mid,
                                    "option": f"{o['label']} → {word}"})
                    continue
                ok, detail = app_api.resolve_option(str(o["id"]), outcome)
                reports.append({"status": "resolved" if ok else "error", "market_id": mid,
                                "option": f"{o['label']} → {word}", "via": "fuel-threshold"})
                why = (f"riba {_fmt(th)} €/l pasiekta {hit.isoformat()} ({_fmt(prices[hit])} €/l)"
                       if outcome == "yes" else f"iki {o['label']} riba {_fmt(th)} €/l nepasiekta")
                if ok:
                    _send(alert, f"⛽ {title}\n  {o['label']} → {word}: {why}")
                elif not st.get("fail_alerted"):
                    _send(alert, f"⚠️ {title}: nepavyko išspręsti {o['label']} ({detail[:150]})")
                    st["fail_alerted"] = True
                changed = True
        changed = changed or len(str(state.get("_lea_prices"))) != before
    return reports, changed
