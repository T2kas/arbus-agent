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
import math
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

def _get(url: str, timeout: int = 20) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    r.encoding = "utf-8"
    return r.text


# LEA's daily bulletin lives at a URL that CARRIES the date: today it is
# /Naujiena/kdk-YYYYMMDD/ (earlier days used ndk-YYYYMMDD). The date in the URL
# is the reliable signal — far safer than parsing "rugsėjo 23 d." out of prose,
# which the bulletins often skip (they say "penktadienį"). So the resolver just
# fetches the URL for the exact date it needs.
_KDK_SLUG_RE = re.compile(r"/Naujiena/((?:kdk|ndk)-(20\d{2})(\d{2})(\d{2}))", re.I)


def _kdk_url(iso: str, prefix: str = "kdk") -> str:
    d = date.fromisoformat(iso)
    return f"https://www.ena.lt/Naujiena/{prefix}-{d.strftime('%Y%m%d')}/"


def _price(html: str, fuel: str) -> float | None:
    val = resolvers._fuel_prices(html).get(fuel)
    return float(val) if isinstance(val, (int, float)) and 0.3 < val < 5 else None


def lea_price_for_date(target_iso: str, fuel: str,
                       lookback: int | None = None) -> tuple[float | None, str]:
    """(price, source_url) for `fuel` on the EXACT `target_iso`, or (None, '') if
    not published yet. The date is taken from the bulletin URL itself, so a
    different day's price can never be returned — the safety this resolver needs.
    """
    # 1) the dated bulletin URL directly (the date is in the URL).
    for prefix in ("kdk", "ndk"):
        try:
            price = _price(_get(_kdk_url(target_iso, prefix)), fuel)
        except Exception:                                # noqa: BLE001 — 404 = not published
            price = None
        if price is not None:
            return price, _kdk_url(target_iso, prefix)
    # 2) fallback: a descriptive-slug bulletin, matched by its URL date via sitemap.
    try:
        sitemap = _get("https://www.ena.lt/sitemap.xml")
    except Exception as exc:                             # noqa: BLE001
        log.warning("fuel: sitemap fetch failed: %s", exc)
        return None, ""
    for slug, y, mo, d in _KDK_SLUG_RE.findall(sitemap):
        if f"{y}-{mo}-{d}" != target_iso:
            continue
        url = f"https://www.ena.lt/Naujiena/{slug}/"
        try:
            price = _price(_get(url), fuel)
        except Exception:                                # noqa: BLE001
            continue
        if price is not None:
            return price, url
    return None, ""


def latest_lea_price(fuel: str, now: datetime | None = None) -> tuple[float | None, str]:
    """The most recent published `fuel` average (the creation forecast) — walk
    back day by day from today over the dated bulletin URLs until one has data."""
    today = (now or datetime.now(timezone.utc)).astimezone(_tz()).date()
    for n in range(0, 8):
        iso = (today - timedelta(days=n)).isoformat()
        price, url = lea_price_for_date(iso, fuel)
        if price is not None:
            return price, url
    return None, ""


# ── publication days: LEA posts no bulletin on weekends / public holidays ─────

def _easter(year: int) -> date:
    """Western Easter Sunday (anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _lt_holidays(year: int) -> set[date]:
    fixed = [(1, 1), (2, 16), (3, 11), (5, 1), (6, 24), (7, 6), (8, 15),
             (11, 1), (11, 2), (12, 24), (12, 25), (12, 26)]
    days = {date(year, m, d) for m, d in fixed}
    days.add(_easter(year) + timedelta(days=1))          # Easter Monday
    return days


def is_publication_day(d: date) -> bool:
    """LEA publishes the daily average only on working days — verified: Fri
    2026-09-25 has a kdk- bulletin, Sat 26 / Sun 27 are 404 (and the archive
    jumps Friday → Monday). A market for any other day can never resolve."""
    return d.weekday() < 5 and d not in _lt_holidays(d.year)


def next_publication_days(after: date, n: int) -> list[date]:
    out, d = [], after
    while len(out) < n:
        d += timedelta(days=1)
        if is_publication_day(d):
            out.append(d)
    return out


# ── market creation: forecast → buckets → probabilities ──────────────────────

FUELS = {
    "benzinas": {"gen": "benzino A95", "loc": "benzino A95"},
    "dyzelinas": {"gen": "dyzelino", "loc": "dyzelino"},
}

# The picture every daily fuel market carried from October to 2026-11-06; used
# only when no earlier fuel market with a picture can be read.
_IMG = "https://crwtwtwljqypvgvvfmyo.supabase.co/storage/v1/object/public/market-images/3686a8dc-a32c-459b-af52-55089933ee92/"
DEFAULT_IMAGES = {
    "benzinas": _IMG + "6378f59a-c3fe-4aaf-a6fd-cf5e7355a5b1.jpg",
    "dyzelinas": _IMG + "454209f0-161b-4c1b-ab6c-eab923b60d11.jpg",
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


def _context_text(fuel: str, forecast: float, d: date | None = None) -> str:
    name = FUELS[fuel]["loc"]
    cycle = (" Antradieniais degalinių kainos Lietuvoje dažniausiai šokteli aukštyn, "
             "o likusią savaitės dalį pamažu slenka žemyn." if d and d.weekday() == 1 else "")
    return (
        f"Degalų kainos Lietuvoje kinta beveik kasdien. Paskutinė paskelbta "
        f"vidutinė {name} kaina — apie {_fmt(forecast)} €/l, bet net kelių centų "
        f"pokytis per dieną gali pakeisti galutinę baigtį.{cycle}"
    )


# ── recent weekday cycle, re-fitted every day from the bulletins themselves ───
#
# The weekly cycle is not stable: the Tuesday jump was +8.7 ct in May and
# +4.4 ct in October, Friday went from −2.4 ct to +0.6 ct, and the spread of
# each weekday shrank from 2–4 ct to ~0.5–1 ct. A table fitted once goes stale
# and anyone who simply watches the last few weeks beats it. So the bot keeps
# the LEA averages in its state and re-fits (median change, sd) per weekday on
# the last FUEL_RECENT_WEEKS weeks. Walk-forward test on 2026-04 → 10-30 (log
# density at the real price, lower = better): static table 2.257 → 12-week
# window 2.150 (2.218 → 2.093 on the later half).

def bulletin_prices(iso: str) -> dict[str, float]:
    """Both fuels' LEA averages for the EXACT date (from its dated URL), or {}."""
    for prefix in ("kdk", "ndk"):
        try:
            html = _get(_kdk_url(iso, prefix))
        except Exception:                                # noqa: BLE001 — 404 = no bulletin
            continue
        out = {f: p for f in FUELS if (p := _price(html, f)) is not None}
        if out:
            return out
    return {}


def _bulletin_prices_with_sitemap():
    """bulletin_prices, falling back to descriptive-slug bulletins found by the
    date in their URL in the sitemap (fetched at most once per call)."""
    slugs: dict[str, list[str]] | None = None

    def fetch(iso: str) -> dict[str, float]:
        nonlocal slugs
        got = bulletin_prices(iso)
        if got:
            return got
        if slugs is None:
            slugs = {}
            try:
                for slug, y, mo, d in _KDK_SLUG_RE.findall(_get("https://www.ena.lt/sitemap.xml")):
                    slugs.setdefault(f"{y}-{mo}-{d}", []).append(slug)
            except Exception as exc:                     # noqa: BLE001
                log.warning("fuel: sitemap fetch failed: %s", exc)
        for slug in slugs.get(iso, []):
            try:
                html = _get(f"https://www.ena.lt/Naujiena/{slug}/")
            except Exception:                            # noqa: BLE001
                continue
            out = {f: p for f in FUELS if (p := _price(html, f)) is not None}
            if out:
                return out
        return {}
    return fetch


def update_history(state: dict, today: date, weeks: int | None = None,
                   fetch=None) -> dict:
    """Fill state["_fuel_hist"] = {iso: {fuel: price}} for the publication days
    of the last `weeks` weeks (fetching only the missing ones; a day still
    missing after 3 days is remembered as {} so it is not re-fetched forever)."""
    weeks = config.FUEL_RECENT_WEEKS if weeks is None else weeks
    fetch = fetch or _bulletin_prices_with_sitemap()
    hist = state.setdefault("_fuel_hist", {})
    start = today - timedelta(days=7 * weeks + 7)
    for iso in [k for k in hist if k < start.isoformat()]:
        del hist[iso]
    d = start
    while d <= today:
        iso = d.isoformat()
        if is_publication_day(d) and iso not in hist:
            try:
                got = fetch(iso)
            except Exception as exc:                     # noqa: BLE001
                log.warning("fuel history %s: %s", iso, exc)
                got = {}
            if got or (today - d).days >= 3:
                hist[iso] = got
        d += timedelta(days=1)
    return hist


def recent_weekday_model(hist: dict, target: date, weeks: int | None = None) -> dict:
    """{weekday: (median change €/l, sd)} from consecutive bulletins (only a
    weekend in between, never a holiday) of the last `weeks` weeks before
    `target`, both fuels pooled. A weekday with too few changes keeps the
    static FUEL_WEEKDAY_MODEL entry."""
    weeks = config.FUEL_RECENT_WEEKS if weeks is None else weeks
    lo = (target - timedelta(days=7 * weeks)).isoformat()
    days = sorted(k for k, v in hist.items() if v and lo <= k < target.isoformat())
    changes: dict[int, list[float]] = {}
    for a, b in zip(days, days[1:]):
        da, db = date.fromisoformat(a), date.fromisoformat(b)
        if any((da + timedelta(days=i)).weekday() < 5 for i in range(1, (db - da).days)):
            continue                                     # a working day (holiday/missing) in between
        for f in FUELS:
            if f in hist[a] and f in hist[b]:
                changes.setdefault(db.weekday(), []).append(hist[b][f] - hist[a][f])
    model = dict(config.FUEL_WEEKDAY_MODEL)
    for wd, v in changes.items():
        if len(v) < config.FUEL_RECENT_MIN_CHANGES:
            continue
        v = sorted(v)
        mid = len(v) // 2
        med = v[mid] if len(v) % 2 else (v[mid - 1] + v[mid]) / 2
        sd = math.sqrt(sum((x - med) ** 2 for x in v) / (len(v) - 1))
        model[wd] = (round(med, 4), round(max(sd, config.FUEL_SIGMA_FLOOR), 4))
    return model


def drift_and_sigma(target: date, base: date | None = None,
                    model: dict | None = None) -> tuple[float, float]:
    """Expected change €/l from the base bulletin's price to the target day's,
    and its σ. Each weekday's (drift, σ) in FUEL_WEEKDAY_MODEL is the change
    from the PREVIOUS bulletin (Monday's covers the weekend). When working days
    in between have no bulletin (a public holiday — e.g. Monday 2 Nov 2026,
    Vėlinės, made Tuesday's market start from Friday's price), their drifts are
    added too and the σ's combined."""
    model = model or config.FUEL_WEEKDAY_MODEL
    days = [target]
    if base is not None and base < target:
        days = [base + timedelta(days=i) for i in range(1, (target - base).days + 1)]
        days = [x for x in days if x.weekday() < 5] or [target]
    drift = sum(model.get(x.weekday(), (0.0, config.FUEL_FORECAST_SIGMA))[0] for x in days)
    var = sum(model.get(x.weekday(), (0.0, config.FUEL_FORECAST_SIGMA))[1] ** 2 for x in days)
    return drift, math.sqrt(var)


def market_spec(fuel: str, target_iso: str, image_url: str,
                forecast: float, tz: ZoneInfo, base: date | None = None,
                model: dict | None = None) -> dict:
    d = date.fromisoformat(target_iso)
    drift, sigma = drift_and_sigma(d, base, model)
    expected = forecast + drift              # weekly pump-price cycle (Tuesday jump)
    buckets = build_buckets(expected)
    pcts = bucket_probabilities(expected, buckets, sigma)
    # Closes late on the LAST PUBLICATION DAY BEFORE the market's date (Monday's
    # market on Friday): LEA's own live map (degalukainos.ena.lt) shows each
    # station's price as it is submitted before 10:00, plus voluntary real-time
    # updates (also at weekends), so a market open on the morning — or over the
    # weekend — before its date can be read off the map.
    close_day = d
    if config.FUEL_CLOSE_DAYS_BEFORE > 0:
        close_day = d - timedelta(days=1)
        while not is_publication_day(close_day):
            close_day -= timedelta(days=1)
    closes_at = datetime.combine(
        close_day, dtime(hour=config.FUEL_CLOSE_HOUR, minute=config.FUEL_CLOSE_MINUTE), tzinfo=tz)
    return {
        "fuel": fuel,
        "date": target_iso,
        "title": f"Vidutinė {FUELS[fuel]['gen']} kaina Lietuvoje {_lt_date_text(d)}?",
        "subtitle": _lt_date_text(d),
        "category": config.FUEL_CATEGORY,
        "image_url": image_url or config.FUEL_IMAGE,
        "liquidity": config.FUEL_LIQUIDITY,
        "rules": _rules_text(fuel, _lt_date_text(d)),
        "context": _context_text(fuel, forecast, d),
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
                     forecasts: dict | None = None,
                     state: dict | None = None,
                     image_rows: list[dict] | None = None) -> list[dict]:
    """Specs for the daily fuel markets that should exist over the horizon. The
    newest image of an existing fuel market is reused per fuel. `rows` holds only
    open markets, and the previous fuel market is usually resolved by the time
    the next one is made, so `image_rows` (newest-first, resolved ones included)
    is searched too; failing both, DEFAULT_IMAGES."""
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
    for m in image_rows or []:
        tgt = fuel_target(m)
        img = str(m.get("image_url") or "") if tgt else ""
        if img and tgt[0] not in images:
            images[tgt[0]] = img
    for fuel_key, img in DEFAULT_IMAGES.items():
        images.setdefault(fuel_key, img)
    hist = {}
    if state is not None and config.FUEL_RECENT_WEEKS > 0:
        try:
            hist = update_history(state, today)
        except Exception as exc:                         # noqa: BLE001 — fall back to the table
            log.warning("fuel history update failed: %s", exc)
    specs = []
    for fuel in FUELS:
        forecast, base = (forecasts or {}).get(fuel), None
        if forecast is None:
            forecast, url = latest_lea_price(fuel)
            m = re.search(r"(?:kdk|ndk)-(20\d{2})(\d{2})(\d{2})", url or "")
            base = date(int(m[1]), int(m[2]), int(m[3])) if m else None
        if forecast is None:
            continue
        # Only days LEA actually publishes: Friday's run creates MONDAY's market.
        for d in next_publication_days(today, horizon):
            iso = d.isoformat()
            if skip_existing and (fuel, iso) in have:
                continue
            if (config.FUEL_SKIP_AFTER_GAP and base is not None and any(
                    (base + timedelta(days=i)).weekday() < 5 for i in range(1, (d - base).days))):
                # A working day without a bulletin in between (a holiday, or
                # today's bulletin not out yet): the price has moved unseen for a
                # day while the live map may already show it — no market.
                log.info("fuel %s %s: base bulletin %s is not the previous working day — skipped",
                         fuel, iso, base)
                continue
            model = recent_weekday_model(hist, d) if hist else None
            spec = market_spec(fuel, iso, images.get(fuel, ""), forecast, tz, base, model)
            if datetime.fromisoformat(spec["closes_at"]) <= now + timedelta(hours=1):
                log.info("fuel %s %s: too late to open (closes %s)", fuel, iso, spec["closes_at"])
                continue
            specs.append(spec)
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
        if not is_publication_day(date.fromisoformat(iso)):
            # LEA publishes nothing for weekends/holidays, so the rules' source
            # never appears. Do NOT guess with another day's price — flag once.
            if alert:
                notify.send(
                    "⚠️ DEGALŲ RINKA — REIKIA ADMINO\n\n"
                    f"· {app_api.question_of(m)}\n"
                    f"  {iso} yra savaitgalis / šventė — LEA tą dieną vidurkio "
                    "neskelbia, todėl pagal taisykles rinkos išspręsti negalima. "
                    "Atšauk (grąžink statymus) arba išspręsk rankiniu būdu.")
            st["fuel_alerted"] = True
            reports.append({"status": "error", "market_id": mid,
                            "reason": "no LEA bulletin on weekends/holidays"})
            changed = True
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
