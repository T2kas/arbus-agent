"""Daily fuel-price markets — the same shape as the weather bot, for the LEA
national daily average petrol (A95) / diesel price.

One bucketed market per fuel per day ("Vidutinė benzino A95 kaina Lietuvoje
2026 m. rugsėjo 23 d.?"), resolved from the ena.lt (Lietuvos energetikos
agentūra) bulletin published for that EXACT date, and the next day's market
created the same way — same image and rules, five price buckets around the
latest price, probabilities from a normal spread.

Correctness is money here, so the resolver is deliberately strict: it resolves
ONLY when it finds an LEA bulletin whose own stated date equals the market's
target date (never a different day's price), reads that fuel's national average,
and maps it to the bucket that contains it. If it cannot confirm the date, it
waits. Only the recurring DAILY markets are managed — ladders, "highest price
2026" and threshold markets are left untouched.

Pure parsing/mapping is split from the network fetchers so the decision logic
stays testable offline, the same discipline as weather.py and resolvers.py.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from . import app as app_api, config, notify, resolvers
from .resolvers import UA, _LT_MONTHS
from .weather import bucket_probabilities, _LT_MONTH_GEN

log = logging.getLogger(__name__)

_PRICE_RE = re.compile(r"(\d)[.,](\d{2,3})")            # 1,919 / 2.230
_ISO_RE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_LT_DATE_RE = re.compile(r"(20\d{2})\s*m\.?\s*(" + "|".join(_LT_MONTHS) + r")\s+(\d{1,2})",
                         re.I)


# ── pure: prices, buckets ────────────────────────────────────────────────────

def _prices(text: str) -> list[float]:
    """Every €/l-style price in a string, as floats (1,919 → 1.919)."""
    return [float(f"{a}.{b}") for a, b in _PRICE_RE.findall(text or "")]


def parse_price_bucket(label: str) -> tuple[float | None, float | None]:
    """(low, high) inclusive €/l bounds for one outcome label; None = unbounded.

      "1,919 €/l arba mažiau"      → (None, 1.919)
      "Nuo 1,920 iki 1,930 €/l"    → (1.920, 1.930)
      "1,960 €/l arba daugiau"     → (1.960, None)
    """
    low = label.lower()
    nums = _prices(low)
    if any(w in low for w in ("daugiau", "aukštesn", "aukstesn", "viršij", "virsij")):
        return (nums[0] if nums else None), None
    if any(w in low for w in ("mažiau", "maziau", "žemesn", "zemesn")):
        return None, (nums[0] if nums else None)
    if "iki" in low and len(nums) >= 2:
        return nums[0], nums[1]
    if len(nums) >= 2:
        return min(nums), max(nums)
    if len(nums) == 1:
        return nums[0], nums[0]
    return None, None


def buckets_from_market(market: dict) -> list[dict]:
    out = []
    for o in (market.get("market_options") or market.get("options") or []):
        oid = str(app_api._pick(o, "id", "option_id", default=""))
        label = str(app_api._pick(o, "label", "name", "title", default=""))
        low, high = parse_price_bucket(label)
        out.append({"option_id": oid, "label": label, "low": low, "high": high})
    return out


def bucket_for_price(buckets: list[dict], price: float) -> dict | None:
    for b in buckets:
        low, high = b["low"], b["high"]
        if (low is None or price >= low) and (high is None or price <= high):
            return b
    best, best_d = None, None                            # nearest-bound fallback
    for b in buckets:
        for bound in (b["low"], b["high"]):
            if bound is None:
                continue
            d = abs(price - bound)
            if best_d is None or d < best_d:
                best, best_d = b, d
    return best


def _looks_like_price_buckets(options: list[str]) -> bool:
    """True when the options are €/l price ranges (not dates, not Taip/Ne)."""
    priced = 0
    for label in options:
        low = label.lower()
        if "taip" == low.strip() or "ne" == low.strip():
            return False
        if re.search(r"20\d{2}\s*m", low):               # a date option → a ladder
            return False
        if ("/l" in low or "eur" in low or "€" in low) and _prices(low):
            priced += 1
    return priced >= 3


# ── pure: which fuel / which day a market is about ───────────────────────────

def _single_date(text: str) -> str:
    m = _ISO_RE.search(text)
    if m:
        try:
            return date(int(m[1]), int(m[2]), int(m[3])).isoformat()
        except ValueError:
            return ""
    m = _LT_DATE_RE.search(text)
    if m:
        try:
            return date(int(m[1]), _LT_MONTHS[m[2].lower()], int(m[3])).isoformat()
        except ValueError:
            return ""
    return ""


def fuel_target(market: dict) -> tuple[str, str] | None:
    """(fuel_label, ISO date) for a DAILY average-price market, else None.

    fuel_label is 'benzinas' (A95 petrol) or 'dyzelinas', matching the keys the
    LEA parser returns. Excludes ladders, threshold (Taip/Ne) and 'highest 2026'
    markets — only a single-day bucketed average-price market qualifies."""
    title = app_api.question_of(market)
    rules = str(market.get("rules") or "")
    text = f"{title} {rules}".lower()
    if "vidutin" not in text:
        return None
    if "benzin" not in text and "dyzel" not in text and "a95" not in text:
        return None
    if any(w in text for w in ("aukščiaus", "auksciaus", "pasieks", "bent vien")):
        return None                                      # highest / threshold / "at least once"
    options = [str(app_api._pick(o, "label", "name", "title", default=""))
               for o in (market.get("market_options") or market.get("options") or [])]
    if not _looks_like_price_buckets(options):
        return None
    iso = _single_date(title) or _single_date(rules)
    if not iso:
        return None
    fuel = "benzinas" if ("benzin" in text or "a95" in text) else "dyzelinas"
    return fuel, iso


# ── network: the LEA price for an EXACT date ─────────────────────────────────

def _bulletin_iso(html: str, year: int) -> str:
    """ISO date the bulletin is about, from its LT date ('rugsėjo 23 d.')."""
    m = resolvers._BULLETIN_DATE_RE.search(html)
    if not m:
        return ""
    mm = re.match(r"(\w+)\s+(\d+)", m.group(0))
    if not mm:
        return ""
    month = _LT_MONTHS.get(mm.group(1).lower())
    if not month:
        return ""
    try:
        return date(year, month, int(mm.group(2))).isoformat()
    except ValueError:
        return ""


def _get(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


def lea_price_for_date(target_iso: str, fuel: str,
                       lookback: int | None = None) -> tuple[float | None, str]:
    """(price, source_url) for `fuel` on the EXACT `target_iso`, from the LEA
    bulletin whose own stated date equals it, or (None, '') if not published yet.

    Never returns a different day's price — that is the whole safety of this
    resolver (mirrors the cinema-period fix)."""
    lookback = lookback or config.FUEL_BULLETIN_LOOKBACK
    year = date.fromisoformat(target_iso).year
    try:
        sitemap = _get("https://www.ena.lt/sitemap.xml")
    except Exception as exc:                             # noqa: BLE001
        log.warning("fuel: sitemap fetch failed: %s", exc)
        return None, ""
    for url in resolvers.recent_fuel_bulletin_urls(sitemap, lookback):
        try:
            html = _get(url)
        except Exception as exc:                         # noqa: BLE001
            log.debug("fuel: bulletin %s failed: %s", url, exc)
            continue
        if _bulletin_iso(html, year) != target_iso:      # only the exact date's bulletin
            continue
        prices = resolvers._fuel_prices(html)
        val = prices.get(fuel)
        if isinstance(val, (int, float)) and 0.3 < val < 5:
            return float(val), url
    return None, ""


def latest_lea_price(fuel: str) -> tuple[float | None, str]:
    """The most recent published `fuel` average (for the creation forecast)."""
    try:
        sitemap = _get("https://www.ena.lt/sitemap.xml")
    except Exception as exc:                             # noqa: BLE001
        log.warning("fuel: sitemap fetch failed: %s", exc)
        return None, ""
    for url in resolvers.recent_fuel_bulletin_urls(sitemap, config.FUEL_BULLETIN_LOOKBACK):
        try:
            prices = resolvers._fuel_prices(_get(url))
        except Exception:                                # noqa: BLE001
            continue
        val = prices.get(fuel)
        if isinstance(val, (int, float)) and 0.3 < val < 5:
            return float(val), url
    return None, ""


# ── market creation: forecast → buckets → probabilities ──────────────────────

FUELS = {
    "benzinas": {"gen": "benzino A95", "loc": "benzino A95"},
    "dyzelinas": {"gen": "dyzelino", "loc": "dyzelino"},
}


def _fmt(price: float) -> str:
    return f"{price:.3f}".replace(".", ",")


def build_buckets(forecast: float) -> list[dict]:
    """Five contiguous €/l buckets ~1 cent wide, the middle one centred on the
    forecast, matching the app's label style."""
    b = round(forecast, 2)
    edges = [b - 0.016, b - 0.006, b + 0.004, b + 0.014]          # inclusive label bounds
    bnds = [b - 0.0155, b - 0.0055, b + 0.0045, b + 0.0145]       # probability boundaries
    return [
        {"label": f"{_fmt(edges[0] - 0.001)} €/l arba mažiau", "lo": None, "hi": bnds[0]},
        {"label": f"Nuo {_fmt(edges[0])} iki {_fmt(edges[1])} €/l", "lo": bnds[0], "hi": bnds[1]},
        {"label": f"Nuo {_fmt(edges[1] + 0.001)} iki {_fmt(edges[2])} €/l", "lo": bnds[1], "hi": bnds[2]},
        {"label": f"Nuo {_fmt(edges[2] + 0.001)} iki {_fmt(edges[3])} €/l", "lo": bnds[2], "hi": bnds[3]},
        {"label": f"{_fmt(edges[3] + 0.001)} €/l arba daugiau", "lo": bnds[3], "hi": None},
    ]


def _lt_date_text(d: date) -> str:
    return f"{d.year} m. {_LT_MONTH_GEN[d.month]} {d.day} d."


def _rules_text(fuel: str, date_text: str) -> str:
    name = FUELS[fuel]["gen"]
    return (
        f"Rinka bus išspręsta pagal Lietuvos energetikos agentūros paskelbtą "
        f"{date_text} nacionalinę vidutinę {name} kainą.\n\n"
        "Naudojamas visų duomenis pateikusių Lietuvos degalinių tos dienos kainų "
        "vidurkis (apie 10:00 val.). Atskirų degalinių, tinklų, miestų, "
        "savivaldybių, mažiausia, didžiausia ir didmeninė kaina neįskaitomos. "
        + ("Kitų benzino rūšių kainos nenaudojamos.\n\n" if fuel == "benzinas" else "\n")
        + "Kiekviena nurodyta kainos riba yra atskira baigtis; laimi ta, į kurios "
        "intervalą patenka oficiali tos dienos vidutinė kaina.\n\n"
        "Jeigu tos dienos oficialus vidurkis nepaskelbiamas, rinka sprendžiama jį "
        "paskelbus; jeigu duomenys vėliau pataisomi, naudojama naujausia oficiali "
        "reikšmė. Rezultato šaltinis — Lietuvos energetikos agentūra (ena.lt)."
    )


def _context_text(fuel: str, forecast: float) -> str:
    name = FUELS[fuel]["loc"]
    return (
        f"Degalų kainos Lietuvoje kinta beveik kasdien. Paskutinė paskelbta "
        f"vidutinė {name} kaina — apie {_fmt(forecast)} €/l, bet net kelių centų "
        "pokytis per dieną gali pakeisti galutinę baigtį."
    )


def market_spec(fuel: str, target_iso: str, image_url: str,
                forecast: float, tz: ZoneInfo) -> dict:
    d = date.fromisoformat(target_iso)
    buckets = build_buckets(forecast)
    pcts = bucket_probabilities(forecast, buckets, config.FUEL_FORECAST_SIGMA)
    closes_at = datetime.combine(
        d, dtime(hour=config.FUEL_CLOSE_HOUR, minute=config.FUEL_CLOSE_MINUTE), tzinfo=tz)
    return {
        "fuel": fuel,
        "date": target_iso,
        "title": f"Vidutinė {FUELS[fuel]['gen']} kaina Lietuvoje {_lt_date_text(d)}?",
        "subtitle": _lt_date_text(d),
        "category": config.FUEL_CATEGORY,
        "image_url": image_url or config.FUEL_IMAGE,
        "liquidity": config.FUEL_LIQUIDITY,
        "rules": _rules_text(fuel, _lt_date_text(d)),
        "context": _context_text(fuel, forecast),
        "options": [{"label": b["label"], "probability": p} for b, p in zip(buckets, pcts)],
        "closes_at": closes_at.isoformat(),
        "forecast": forecast,
    }


# ── planning / creating (mirrors weather.plan_new_markets) ───────────────────

def _tz() -> ZoneInfo:
    return ZoneInfo(config.WEATHER_TZ)


def plan_new_markets(rows: list[dict], now: datetime, tz: ZoneInfo,
                     *, skip_existing: bool = True,
                     horizon: int | None = None,
                     forecasts: dict | None = None) -> list[dict]:
    """Specs for the daily fuel markets that should exist over the horizon. The
    newest image of an existing fuel market is reused per fuel."""
    horizon = config.FUEL_HORIZON_DAYS if horizon is None else horizon
    today = now.astimezone(tz).date()
    have: set[tuple[str, str]] = set()
    images: dict[str, str] = {}
    for m in rows:
        tgt = fuel_target(m)
        if not tgt:
            continue
        have.add(tgt)
        img = str(m.get("image_url") or "")
        if img and tgt[0] not in images:
            images[tgt[0]] = img
    specs = []
    for fuel in FUELS:
        forecast = (forecasts or {}).get(fuel)
        if forecast is None:
            forecast, _ = latest_lea_price(fuel)
        if forecast is None:
            continue
        for n in range(1, horizon + 1):
            iso = (today + timedelta(days=n)).isoformat()
            if skip_existing and (fuel, iso) in have:
                continue
            specs.append(market_spec(fuel, iso, images.get(fuel, ""), forecast, tz))
    return specs


def _created_message(spec: dict) -> str:
    opts = " · ".join(f"{o['probability']}% {o['label']}" for o in spec["options"])
    return "\n".join([
        "⛽ SUKURTA DEGALŲ RINKA",
        "",
        f"· {spec['title']}",
        f"  Prognozė ~{_fmt(spec['forecast'])} €/l | likvidumas {spec['liquidity']} | "
        f"uždaroma {spec['closes_at'][:16].replace('T', ' ')}",
        f"  {opts}",
    ])


def create_markets(specs: list[dict], *, alert: bool, do_create: bool) -> list[dict]:
    reports = []
    for s in specs:
        if not do_create:
            reports.append({"status": "would-create", "fuel": s["fuel"], "date": s["date"],
                            "forecast": s["forecast"], "options": s["options"],
                            "title": s["title"]})
            continue
        ok, detail = app_api.create_market(s)
        if ok and alert:
            notify.send(_created_message(s))
        elif not ok and alert:
            notify.send(f"⚠️ DEGALŲ RINKA: nepavyko sukurti — {detail[:180]}")
        reports.append({"status": "created" if ok else "error", "fuel": s["fuel"],
                        "date": s["date"], "detail": detail, "title": s["title"],
                        "forecast": s["forecast"], "options": s["options"]})
    return reports


# ── resolution ───────────────────────────────────────────────────────────────

def _resolved_message(market: dict, bucket: dict, price: float, fuel: str, url: str) -> str:
    return "\n".join([
        "⛽ DEGALŲ RINKA IŠSPRĘSTA",
        "",
        f"· {app_api.question_of(market)}",
        f"  💶 Vidutinė {FUELS[fuel]['loc']} kaina: {_fmt(price)} €/l",
        f"  🏆 Laimi: {bucket.get('label')}",
        f"  🔗 {url}",
    ])


def resolve_markets(rows: list[dict], now: datetime, tz: ZoneInfo, state: dict,
                    *, alert: bool, do_resolve: bool) -> tuple[list[dict], bool]:
    """Resolve daily fuel markets once the LEA bulletin for their EXACT date is
    published. Resolves only on a confirmed date+price; never guesses."""
    reports, changed = [], False
    today = now.astimezone(tz).date().isoformat()
    for m in rows:
        if app_api.winning_option_of(m) or app_api.status_of(m) in config.APP_SETTLED_STATUSES:
            continue
        tgt = fuel_target(m)
        if not tgt:
            continue
        fuel, iso = tgt
        if iso > today:                                  # the day has not happened yet
            continue
        mid = app_api.market_id_of(m)
        st = state.setdefault(mid, {})
        if st.get("resolved") or st.get("fuel_alerted"):
            continue
        try:
            price, url = lea_price_for_date(iso, fuel)
        except Exception as exc:                         # noqa: BLE001
            log.warning("fuel resolve %s failed: %s", mid, exc)
            continue
        if price is None:                                # not published yet — wait
            continue
        buckets = buckets_from_market(m)
        bucket = bucket_for_price(buckets, price)
        if not bucket or not bucket.get("option_id"):
            if alert:
                notify.send(f"⚠️ DEGALŲ RINKA: kainai {_fmt(price)} €/l nerasta baigtis "
                            f"— {app_api.question_of(m)}")
            st["fuel_alerted"] = True
            reports.append({"status": "error", "market_id": mid, "reason": "no bucket"})
            changed = True
            continue
        label = bucket.get("label")
        if not do_resolve:
            reports.append({"status": "would-resolve", "market_id": mid, "option": label,
                            "note": f"{_fmt(price)} €/l ({fuel})"})
            continue
        ok, detail = app_api.resolve_market(mid, bucket["option_id"])
        if ok:
            st.update({"resolved": True, "resolved_option_id": bucket["option_id"],
                       "resolved_label": label, "resolved_price": price,
                       "resolved_at": now.isoformat(), "resolved_source": url})
            changed = True
            if alert:
                notify.send(_resolved_message(m, bucket, price, fuel, url))
            reports.append({"status": "resolved", "market_id": mid, "option": label,
                            "via": "fuel"})
        else:
            if alert:
                notify.send(f"⚠️ DEGALŲ RINKA: nepavyko resolvinti ({detail[:150]}) — "
                            f"{app_api.question_of(m)}")
            reports.append({"status": "error", "market_id": mid, "reason": f"resolve: {detail}"})
    return reports, changed
