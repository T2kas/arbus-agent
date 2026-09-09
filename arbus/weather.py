"""Weather bot: resolve daily-max-temperature markets from the official Meteo LT
API (api.meteo.lt / LHMT), with no server.

An `orai` market asks which temperature bucket the day's HIGHEST air temperature
falls into, at one station (Vilnius / Kaunas), on one Lithuanian calendar day.
Because only the maximum matters and temperature can still rise later in the day:

  • the TOP bucket (unbounded above, e.g. "21,0 °C arba aukštesnė") becomes
    mathematically final the moment any valid measurement reaches its lower
    bound — so the bot closes trading and resolves it EARLY;
  • every other bucket can only be confirmed once the LAST hourly measurement of
    the Lithuanian calendar day has arrived (a later reading could still lift the
    max into a higher bucket), so those wait for end of day.

All measurement times from Meteo LT are UTC; the market's day is Europe/Vilnius.
A Lithuanian calendar day spans two UTC dates, so the final check pulls both and
keeps only the readings that land on the target day once converted to Vilnius
(zoneinfo handles summer/winter time — never a hand-written offset).

Network fetchers are kept apart from the pure parsers/mappers so the decision
logic stays testable offline, the same discipline as resolvers.py.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

from . import app as app_api, config, notify
from .resolvers import _LT_MONTHS, _STATION_BY_CITY, UA

log = logging.getLogger(__name__)

_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
_ISO_DATE = re.compile(r"(20\d{2})-(\d{1,2})-(\d{1,2})")
_LT_DAY = re.compile(r"(" + "|".join(_LT_MONTHS) + r")\s+(\d{1,2})", re.I)


# ── pure: parsing temperature buckets from option labels ─────────────────────

def _numbers(text: str) -> list[float]:
    cleaned = re.sub(r"\s", " ", text)
    return [float(m.replace(",", ".")) for m in _NUM.findall(cleaned)]


def parse_bucket(label: str) -> tuple[float | None, float | None]:
    """(low, high) inclusive °C bounds for one outcome label; None = unbounded.

    Handles the three shapes the app uses:
      "16,9 °C arba žemesnė"   → (None, 16.9)   temp ≤ 16.9
      "17,0 iki 18,9 °C"       → (17.0, 18.9)   17.0 ≤ temp ≤ 18.9
      "21,0 °C arba aukštesnė" → (21.0, None)   temp ≥ 21.0
    """
    l = label.lower()
    nums = _numbers(l)
    if any(w in l for w in ("aukštesn", "aukstesn", "daugiau", "viršij", "virsij")):
        return (nums[0] if nums else None), None        # "… arba aukštesnė" → ≥X
    if any(w in l for w in ("žemesn", "zemesn", "mažiau", "maziau")):
        return None, (nums[0] if nums else None)        # "… arba žemesnė"  → ≤X
    if "iki" in l and len(nums) >= 2:
        return nums[0], nums[1]                          # "X iki Y"
    if len(nums) >= 2:
        return min(nums), max(nums)
    if len(nums) == 1:
        return nums[0], nums[0]
    return None, None


def buckets_from_market(market: dict) -> list[dict]:
    """One dict per outcome: {option_id, label, low, high, sort_order}."""
    out = []
    for o in (market.get("market_options") or market.get("options") or []):
        oid = str(app_api._pick(o, "id", "option_id", default=""))
        label = str(app_api._pick(o, "label", "name", "title", default=""))
        low, high = parse_bucket(label)
        out.append({"option_id": oid, "label": label, "low": low, "high": high,
                    "sort_order": o.get("sort_order")})
    return out


def top_bucket(buckets: list[dict]) -> dict | None:
    """The unbounded-above bucket (the one that can be locked early)."""
    tops = [b for b in buckets if b["high"] is None and b["low"] is not None]
    return max(tops, key=lambda b: b["low"]) if tops else None


def bucket_for_temp(buckets: list[dict], temp: float) -> dict | None:
    """The outcome whose range contains `temp` (bounds inclusive)."""
    for b in buckets:
        low, high = b["low"], b["high"]
        if (low is None or temp >= low) and (high is None or temp <= high):
            return b
    # The buckets tile the line in 0,1 °C steps, so a value only falls in a gap
    # through a data quirk — pick the outcome with the nearest bound rather than
    # refuse to resolve.
    best, best_d = None, None
    for b in buckets:
        for bound in (b["low"], b["high"]):
            if bound is None:
                continue
            d = abs(temp - bound)
            if best_d is None or d < best_d:
                best, best_d = b, d
    return best


# ── pure: measurements, times, the target day ────────────────────────────────

def valid_temp(v) -> bool:
    """A usable airTemperature: a real finite number, not None/NaN/bool/str."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return False
    return math.isfinite(float(v))


def parse_obs_time_utc(s) -> datetime | None:
    """Meteo LT gives 'YYYY-MM-DD HH:MM:SS' in UTC (also tolerate ISO 'T'/'Z')."""
    if not isinstance(s, str) or not s.strip():
        return None
    t = s.strip().replace("T", " ")
    t = t.split("+")[0].replace("Z", "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return datetime.strptime(t, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def _tz() -> ZoneInfo:
    return ZoneInfo(config.WEATHER_TZ)


def lt_day_measurements(observations, target_iso: str,
                        tz: ZoneInfo | None = None) -> list[tuple[datetime, float]]:
    """(Vilnius datetime, temp) for every valid reading on the target LT day.

    Drops null/NaN/wrong-type temperatures and unparseable timestamps, converts
    UTC → Europe/Vilnius, and keeps only readings whose Vilnius calendar date is
    the target day (which is why the caller must supply both UTC dates)."""
    tz = tz or _tz()
    try:
        target = date.fromisoformat(target_iso)
    except (TypeError, ValueError):
        return []
    out = []
    for o in observations or []:
        temp = o.get("airTemperature")
        if not valid_temp(temp):
            continue
        dt = parse_obs_time_utc(o.get("observationTimeUtc"))
        if dt is None:
            continue
        v = dt.astimezone(tz)
        if v.date() == target:
            out.append((v, float(temp)))
    return out


def day_max(observations, target_iso: str,
            tz: ZoneInfo | None = None) -> tuple[float | None, list]:
    ms = lt_day_measurements(observations, target_iso, tz)
    if not ms:
        return None, []
    return max(t for _, t in ms), ms


def day_complete(observations, target_iso: str, tz: ZoneInfo | None = None) -> bool:
    """True once the last hourly measurement of the LT day (23:00 local) exists —
    the point after which the max can no longer change."""
    return any(v.hour == 23 for v, _ in lt_day_measurements(observations, target_iso, tz))


def decline_locked(seq: list[tuple[datetime, float]], need: int = 2) -> bool:
    """True when the day's max is followed by `need` CONSECUTIVE hourly readings
    each strictly below it — the temperature has turned down for `need` straight
    hours, so (barring a rare late re-peak) the daily max is settled.

    `seq` is the day's (local datetime, temp) readings. Consecutive hours are
    required: a gap right after the peak could hide a higher reading, so we wait.
    """
    if not seq:
        return False
    seq = sorted(seq)
    m = max(t for _, t in seq)
    last_peak = max(i for i, (_, t) in enumerate(seq) if t == m)   # end of any plateau
    prev_dt = seq[last_peak][0]
    drops = 0
    for dt, t in seq[last_peak + 1:]:
        if dt - prev_dt != timedelta(hours=1) or t >= m:
            break                                    # gap, or not below the max
        drops += 1
        prev_dt = dt
        if drops >= need:
            return True
    return False


def _seq_from_state(st: dict, tz: ZoneInfo) -> list[tuple[datetime, float]]:
    """The day's (local datetime, temp) readings from the accumulated state."""
    seq = []
    for utc_key, m in (st.get("measurements") or {}).items():
        dt = parse_obs_time_utc(utc_key)
        if dt is not None:
            seq.append((dt.astimezone(tz), m["temp"]))
    seq.sort()
    return seq


# ── pure: which station / which day a market is about ─────────────────────────

def parse_target_date(*texts: str) -> str:
    """ISO date for the market's day, from ISO ('2026-09-10') or Lithuanian
    ('2026 m. rugsėjo 10 d.') text — any field order."""
    joined = " ".join(t for t in texts if isinstance(t, str))
    m = _ISO_DATE.search(joined)
    if m:
        y, mo, d = m.groups()
        try:
            return date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            pass
    low = joined.lower()
    ym = re.search(r"20\d{2}", low)
    dm = _LT_DAY.search(low)
    if ym and dm:
        try:
            return date(int(ym.group()), _LT_MONTHS[dm.group(1).lower()],
                        int(dm.group(2))).isoformat()
        except ValueError:
            pass
    return ""


def weather_target(market: dict) -> tuple[str, str] | None:
    """(station code, ISO date) for an orai market, or None if undetermined."""
    title = app_api.question_of(market)
    subtitle = str(market.get("subtitle") or "")
    rules = str(market.get("rules") or "")
    text = " ".join([title, subtitle, rules]).lower()
    station = next((code for key, code in _STATION_BY_CITY.items() if key in text), None)
    if not station:
        return None
    iso = parse_target_date(title, subtitle, rules)
    return (station, iso) if iso else None


# ── network ──────────────────────────────────────────────────────────────────

def _base() -> str:
    return "https://api.meteo.lt/v1/stations"


def latest_url(station: str) -> str:
    return f"{_base()}/{station}/observations/latest"


def dated_url(station: str, iso: str) -> str:
    return f"{_base()}/{station}/observations/{iso}"


def fetch(url: str, timeout: int = 20) -> tuple[dict, str]:
    """(parsed JSON, raw text). Raw text is what we checksum."""
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    resp.raise_for_status()
    return resp.json(), resp.text


def fetch_lt_day(station: str, target_iso: str) -> tuple[list[dict], list[str], str]:
    """Every hourly observation that can belong to the target LT day, pulled from
    the two UTC dates it spans. Returns (observations, source urls, checksum)."""
    tgt = date.fromisoformat(target_iso)
    obs: list[dict] = []
    sources: list[str] = []
    parts: list[str] = []
    for iso in [(tgt - timedelta(days=1)).isoformat(), target_iso]:
        url = dated_url(station, iso)
        try:
            data, raw = fetch(url)
        except Exception as exc:                       # noqa: BLE001 — best effort
            log.warning("weather: fetch %s failed: %s", url, exc)
            continue
        obs.extend(data.get("observations") or [])
        sources.append(url)
        parts.append(raw)
    checksum = hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()
    return obs, sources, checksum


def _checksum(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── state (committed JSON, so a stateless CI run remembers) ───────────────────

def load_state() -> dict:
    try:
        return json.loads(Path(config.WEATHER_STATE_PATH).read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state: dict) -> None:
    path = Path(config.WEATHER_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True),
                    encoding="utf-8")


# ── Telegram copy ────────────────────────────────────────────────────────────

def _fmt(temp: float) -> str:
    return f"{temp:.1f}".replace(".", ",")


def resolved_message(market: dict, bucket: dict, temp: float, via: str,
                     sources: list[str]) -> str:
    why = {
        "top_locked": "🔒 TOP baigtis užrakinta (temperatūra gali tik kilti)",
        "decline": "📉 Maksimumas krenta jau kelias valandas — piko nebebus",
        "end_of_day": "✅ Diena baigėsi — galutinis maksimumas",
    }.get(via, "✅ Galutinis maksimumas")
    lines = [
        "🌡️ ORŲ RINKA IŠSPRĘSTA",
        "",
        f"· {app_api.question_of(market)}",
        f"  Aukščiausia temp: {_fmt(temp)} °C",
        f"  Laimi: {bucket.get('label')}",
        f"  {why}",
    ]
    if sources:
        lines.append(f"  🔗 {sources[-1]}")
    return "\n".join(lines)


def _resolve_failed_message(market: dict, bucket: dict, temp: float, detail: str) -> str:
    return "\n".join([
        "⚠️ ORŲ RINKA — NEPAVYKO NUSTATYTI REZULTATO",
        "",
        f"· {app_api.question_of(market)}",
        f"  Turėjo laimėti: {bucket.get('label')} ({_fmt(temp)} °C)",
        f"  Klaida: {detail[:200]}",
        "  Nustatyk rankiniu būdu dashboarde.",
    ])


def _no_data_message(market: dict, station: str, iso: str) -> str:
    return "\n".join([
        "⚠️ ORŲ RINKA — NĖRA GALIOJANČIŲ MATAVIMŲ",
        "",
        f"· {app_api.question_of(market)}",
        f"  Stotis {station}, diena {iso}: nė vieno galiojančio airTemperature.",
        "  Rinka NEIŠSPRĘSTA ir NEATŠAUKTA (pozicijos nepaliestos) — reikia admino.",
    ])


# ── market creation: forecast → buckets → probabilities ──────────────────────

# Genitive Lithuanian month names (index 1..12) for titles/dates.
_LT_MONTH_GEN = ["", "sausio", "vasario", "kovo", "balandžio", "gegužės",
                 "birželio", "liepos", "rugpjūčio", "rugsėjo", "spalio",
                 "lapkričio", "gruodžio"]

# The cities the bot manages: measurement station (observations), forecast place
# code, and Lithuanian name forms (locative for titles, genitive for the rules).
CITIES = {
    "vilnius": {"station": "vilniaus-ams", "place": "vilnius",
                "loc": "Vilniuje", "gen": "Vilniaus"},
    "kaunas": {"station": "kauno-ams", "place": "kaunas",
               "loc": "Kaune", "gen": "Kauno"},
}


def forecast_url(place: str) -> str:
    return f"https://api.meteo.lt/v1/places/{place}/forecasts/long-term"


def forecast_daily_max(place: str, target_iso: str,
                       tz: ZoneInfo | None = None) -> float | None:
    """Forecast daily max for a Vilnius calendar day, from the free Meteo LT
    long-term forecast (keyless). None if unavailable."""
    tz = tz or _tz()
    try:
        data, _ = fetch(forecast_url(place))
    except Exception as exc:                           # noqa: BLE001
        log.warning("weather: forecast %s failed: %s", place, exc)
        return None
    temps = []
    for t in data.get("forecastTimestamps") or []:
        v = t.get("airTemperature")
        dt = parse_obs_time_utc(t.get("forecastTimeUtc"))
        if valid_temp(v) and dt is not None and dt.astimezone(tz).date().isoformat() == target_iso:
            temps.append(float(v))
    return max(temps) if temps else None


def build_buckets(forecast_max: float) -> list[dict]:
    """Four contiguous outcomes centred on the forecast: the two 2 °C middle
    buckets straddle it, with an open bucket below and above. Labels match the
    app's existing style (comma decimals)."""
    b = round(forecast_max) - 2
    return [
        {"label": f"{_fmt(b - 0.1)} °C arba žemesnė", "lo": None, "hi": b - 0.05},
        {"label": f"{_fmt(b)} iki {_fmt(b + 1.9)} °C", "lo": b - 0.05, "hi": b + 1.95},
        {"label": f"{_fmt(b + 2)} iki {_fmt(b + 3.9)} °C", "lo": b + 1.95, "hi": b + 3.95},
        {"label": f"{_fmt(b + 4)} °C arba aukštesnė", "lo": b + 3.95, "hi": None},
    ]


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def bucket_probabilities(forecast_max: float, buckets: list[dict],
                         sigma: float) -> list[int]:
    """Integer percentages (each ≥1, summing to 100) from a normal distribution
    N(forecast_max, sigma) integrated over each bucket — the app requires every
    option in a 3+ way market to be strictly between 0 and 100."""
    weights = []
    for b in buckets:
        lo = _phi((b["lo"] - forecast_max) / sigma) if b["lo"] is not None else 0.0
        hi = _phi((b["hi"] - forecast_max) / sigma) if b["hi"] is not None else 1.0
        weights.append(max(hi - lo, 0.0))
    total = sum(weights) or 1.0
    exact = [w / total * 100 for w in weights]
    pct = [max(int(math.floor(x)), 1) for x in exact]           # floor, min 1
    order = sorted(range(len(exact)), key=lambda i: exact[i] - math.floor(exact[i]),
                   reverse=True)
    i = 0
    while sum(pct) < 100:                                        # hand out the remainder
        pct[order[i % len(order)]] += 1
        i += 1
    while sum(pct) > 100:                                        # trim from the largest
        big = max(range(len(pct)), key=lambda k: pct[k])
        if pct[big] <= 1:
            break
        pct[big] -= 1
    return pct


def _lt_date_text(d: date) -> str:
    return f"{d.year} m. {_LT_MONTH_GEN[d.month]} {d.day} d."


def _rules_text(gen: str, date_text: str) -> str:
    return (
        f"Rinka bus išspręsta pagal aukščiausią oro temperatūrą, kurią {gen} "
        f"automatinė meteorologijos stotis užfiksuos {date_text} nuo 00:00 iki "
        "23:59 Lietuvos laiku.\n\n"
        f"Naudojama didžiausia Lietuvos hidrometeorologijos tarnybos „Meteo LT“ "
        f"API laukelyje airTemperature paskelbta {gen} AMS reikšmė. Temperatūra "
        "papildomai neapvalinama.\n\n"
        f"Jeigu {gen} AMS nepateiks dalies matavimų, naudojama aukščiausia iš tą "
        f"dieną paskelbtų galiojančių reikšmių. Jeigu {gen} AMS nepateiks nė "
        "vieno galiojančio matavimo, naudojama artimiausios veikiančios LHMT "
        "automatinės meteorologijos stoties aukščiausia tos dienos temperatūra.\n\n"
        "Rezultato šaltinis yra Lietuvos hidrometeorologijos tarnybos „Meteo LT“ API."
    )


def _context_text(loc: str, month_name: str, forecast_max: float) -> str:
    return (
        f"{month_name.capitalize()} orai {loc} gali pasikeisti vos per kelias "
        f"valandas. Šiuo metu prognozės aukščiausią temperatūrą laiko apie "
        f"{_fmt(round(forecast_max))} °C, bet modeliai dėl tikslios reikšmės dar "
        "nesutaria, todėl net kelių laipsnių skirtumas gali pakeisti galutinę baigtį."
    )


def market_spec(city_key: str, target_iso: str, image_url: str = "",
                sigma: float | None = None, tz: ZoneInfo | None = None) -> dict | None:
    """The full admin_create_market payload for one city+day, or None if there is
    no forecast for that day."""
    tz = tz or _tz()
    sigma = config.WEATHER_FORECAST_SIGMA if sigma is None else sigma
    c = CITIES[city_key]
    tf = forecast_daily_max(c["place"], target_iso, tz)
    if tf is None:
        return None
    d = date.fromisoformat(target_iso)
    buckets = build_buckets(tf)
    pcts = bucket_probabilities(tf, buckets, sigma)
    closes_at = datetime.combine(d, dtime(hour=config.WEATHER_CLOSE_HOUR), tzinfo=tz)
    return {
        "city": city_key,
        "date": target_iso,
        "station": c["station"],
        "title": f"Aukščiausia temperatūra {c['loc']} {_LT_MONTH_GEN[d.month]} {d.day} d.?",
        "subtitle": _lt_date_text(d),
        "category": config.WEATHER_CATEGORY,
        "image_url": image_url or "",
        "liquidity": config.WEATHER_LIQUIDITY,
        "rules": _rules_text(c["gen"], _lt_date_text(d)),
        "context": _context_text(c["loc"], _LT_MONTH_GEN[d.month], tf),
        "options": [{"label": b["label"], "probability": p}
                    for b, p in zip(buckets, pcts)],
        "closes_at": closes_at.isoformat(),
        "forecast_max": tf,
    }


def plan_new_markets(existing_rows: list[dict], now: datetime, tz: ZoneInfo,
                     *, skip_existing: bool = True,
                     horizon: int | None = None) -> list[dict]:
    """Specs for the markets that should exist over the next `horizon` days for
    each city. With skip_existing, ones already in the app are left out."""
    horizon = config.WEATHER_HORIZON_DAYS if horizon is None else horizon
    today = now.astimezone(tz).date()
    have: set[tuple[str, str]] = set()
    images: dict[str, str] = {}
    for m in existing_rows:                             # markets() returns newest-first
        if app_api.category_of(m) != config.WEATHER_CATEGORY:
            continue
        wt = weather_target(m)
        if not wt:
            continue
        station, iso = wt
        have.add((station, iso))
        img = str(m.get("image_url") or "")
        if img and station not in images:               # newest image per station
            images[station] = img
    specs = []
    for n in range(1, horizon + 1):
        iso = (today + timedelta(days=n)).isoformat()
        for city_key, c in CITIES.items():
            if skip_existing and (c["station"], iso) in have:
                continue
            spec = market_spec(city_key, iso, images.get(c["station"], ""), tz=tz)
            if spec:
                specs.append(spec)
    return specs


def create_markets(specs: list[dict], *, alert: bool, do_create: bool) -> list[dict]:
    reports = []
    for s in specs:
        if not do_create:
            reports.append({"status": "would-create", "city": s["city"],
                            "date": s["date"], "forecast_max": s["forecast_max"],
                            "options": s["options"]})
            continue
        ok, detail = app_api.create_market(s)
        if ok and alert:
            notify.send(_created_message(s, detail))
        reports.append({"status": "created" if ok else "error", "city": s["city"],
                        "date": s["date"], "detail": detail,
                        "options": s["options"], "forecast_max": s["forecast_max"]})
    return reports


def _created_message(spec: dict, market_id: str) -> str:
    opts = " · ".join(f"{o['label']} {o['probability']}%" for o in spec["options"])
    return "\n".join([
        "🆕 SUKURTA ORŲ RINKA",
        "",
        f"· {spec['title']}",
        f"  Prognozė ~{_fmt(round(spec['forecast_max']))} °C | uždaroma "
        f"{config.WEATHER_CLOSE_HOUR}:00",
        f"  {opts}",
    ])


# ── orchestration ────────────────────────────────────────────────────────────

def _resolve(market: dict, mid: str, bucket: dict | None, temp: float,
             sources: list[str], st: dict, via: str, now: datetime,
             alert: bool, do_resolve: bool) -> dict:
    """Hand the decided bucket to the app's resolution RPC. The bot ONLY resolves
    — it never closes/freezes trading (a separate system owns that)."""
    if not bucket or not bucket.get("option_id"):
        if alert:
            notify.send(f"⚠️ ORŲ RINKA: nerasta baigtis temperatūrai {_fmt(temp)} °C — "
                        f"{app_api.question_of(market)}")
        return {"market_id": mid, "status": "error", "reason": "no bucket for temp"}

    if not do_resolve:
        return {"market_id": mid, "status": "would-resolve", "via": via,
                "option": bucket.get("label"), "temp": temp,
                "note": "resolve išjungtas (nustatyk WEATHER_RESOLVE=true)"}

    ok, detail = app_api.resolve_market(mid, bucket["option_id"])
    if ok:
        st.update({"resolved": True, "resolved_option_id": bucket["option_id"],
                   "resolved_label": bucket.get("label"), "resolved_via": via,
                   "resolved_temp": temp, "resolved_at": now.isoformat(),
                   "resolved_sources": sources})
        if alert:
            notify.send(resolved_message(market, bucket, temp, via, sources))
        return {"market_id": mid, "status": "resolved", "via": via,
                "option": bucket.get("label"), "temp": temp, "detail": detail}
    # Failure: do NOT mark resolved — retry next run (and the app-side
    # winning_option guard stops a double resolve if the RPC actually landed).
    if alert:
        notify.send(_resolve_failed_message(market, bucket, temp, detail))
    return {"market_id": mid, "status": "error", "reason": f"resolve: {detail}"}


def _process_market(market: dict, mid: str, state: dict, now: datetime,
                    tz: ZoneInfo, alert: bool, do_resolve: bool) -> tuple[dict, bool]:
    target = weather_target(market)
    if not target:
        return {"market_id": mid, "status": "skip", "reason": "no station/date"}, False
    station, iso = target
    buckets = buckets_from_market(market)
    top = top_bucket(buckets)

    st = state.setdefault(mid, {})
    # A brand-new market, or a row reused for a different day/station, starts clean.
    if st.get("date") != iso or st.get("station") != station:
        st.clear()
        st.update({"date": iso, "station": station, "current_maximum": None,
                   "measurements": {}, "checksum": None, "resolved": False})
    if st.get("resolved"):
        return {"market_id": mid, "status": "done", "via": st.get("resolved_via"),
                "option": st.get("resolved_label")}, False

    changed = False
    vil_now = now.astimezone(tz)
    tgt = date.fromisoformat(iso)

    # ── monitor the running day via /observations/latest ──
    try:
        data, raw = fetch(latest_url(station))
    except Exception as exc:                           # noqa: BLE001 — best effort
        return {"market_id": mid, "status": "error", "reason": f"latest: {exc}"}, False
    checksum = _checksum(raw)
    if checksum != st.get("checksum"):
        st["checksum"] = checksum
        changed = True

    for v, temp in lt_day_measurements(data.get("observations"), iso, tz):
        key = v.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        if key not in st["measurements"]:               # never store a reading twice
            st["measurements"][key] = {"temp": temp, "received_at": now.isoformat(),
                                       "source": latest_url(station), "checksum": checksum}
            changed = True

    seq = _seq_from_state(st, tz)                        # day's (local time, temp), sorted
    cur_max = max((t for _, t in seq), default=None)
    if cur_max is not None and cur_max != st.get("current_maximum"):
        st["current_maximum"] = cur_max                 # rises only, by construction
        st["max_received_at"] = now.isoformat()
        st["source"] = latest_url(station)
        changed = True
    latest_hour = seq[-1][0].hour if seq else None

    # ── end-of-day: authoritative, always correct (guaranteed fallback) ──
    end_of_day = vil_now.date() > tgt or (vil_now.date() == tgt and vil_now.hour >= 23)
    if end_of_day:
        day_obs, sources, _ = fetch_lt_day(station, iso)
        if day_complete(day_obs, iso, tz):
            final_max, _ = day_max(day_obs, iso, tz)
            if final_max is None:                        # no valid data all day
                if alert and not st.get("alerted_no_data"):
                    notify.send(_no_data_message(market, station, iso))
                    st["alerted_no_data"] = True
                    changed = True
                return {"market_id": mid, "status": "wait",
                        "note": "nėra galiojančių matavimų — laukiam/adminas"}, changed
            return _resolve(market, mid, bucket_for_temp(buckets, final_max),
                            final_max, sources, st, "end_of_day",
                            now, alert, do_resolve), True
        return {"market_id": mid, "status": "wait", "max": cur_max,
                "note": "diena dar nepilna — laukiam paskutinio valandinio matavimo"}, changed

    # ── evening early resolution (only from WEATHER_RESOLVE_MIN_HOUR onward) ──
    # Nothing resolves before this hour, so the afternoon peak window is already
    # past and a separate system can keep trading open until then. Two safe locks:
    #   • top bucket — once the max is ≥ its lower bound it can only stay there;
    #   • decline    — the max has been beaten downward for N straight hours.
    if (cur_max is not None and latest_hour is not None
            and latest_hour >= config.WEATHER_RESOLVE_MIN_HOUR):
        top_locked = top and top["low"] is not None and cur_max >= top["low"]
        if top_locked or decline_locked(seq, config.WEATHER_DECLINE_HOURS):
            day_obs, sources, _ = fetch_lt_day(station, iso)     # confirm officially
            confirm_max, _ = day_max(day_obs, iso, tz)
            if confirm_max is not None:
                if top_locked and not (top["low"] is not None and confirm_max >= top["low"]):
                    top_locked = False                   # official data disagrees — fall through
                bucket = top if top_locked else bucket_for_temp(buckets, confirm_max)
                via = "top_locked" if top_locked else "decline"
                return _resolve(market, mid, bucket, confirm_max, sources, st,
                                via, now, alert, do_resolve), True

    return {"market_id": mid, "status": "watch", "max": cur_max}, changed


def run(now: datetime | None = None, *, alert: bool = True, do_resolve: bool = True,
        do_create: bool = True, force_create: bool = False,
        limit: int = 200) -> tuple[list[dict], str]:
    """One pass: resolve open orai markets, then keep the creation horizon full.
    Returns (per-market reports, error)."""
    now = now or datetime.now(timezone.utc)
    try:
        tz = _tz()
    except Exception as exc:                            # noqa: BLE001
        return [], (f"laiko juostos '{config.WEATHER_TZ}' nėra: {exc}. "
                    "Įdiek IANA zonas: pip install tzdata (jau requirements.txt).")
    rows, err = app_api.markets(limit)
    if err:
        return [], err
    state = load_state()
    reports: list[dict] = []
    changed = False
    for m in rows:
        if app_api.category_of(m) != config.WEATHER_CATEGORY:
            continue
        if app_api.status_of(m) in config.APP_SETTLED_STATUSES:
            continue
        if app_api.winning_option_of(m):                # already decided in the app
            continue
        mid = app_api.market_id_of(m)
        report, mchanged = _process_market(m, mid, state, now, tz, alert, do_resolve)
        reports.append(report)
        changed = changed or mchanged
    if changed:
        save_state(state)

    # Keep the rolling horizon full — create tomorrow/day-after where missing.
    # Gated to the evening (fresh forecast) unless forced.
    if do_create and (force_create or now.astimezone(tz).hour >= config.WEATHER_CREATE_HOUR):
        specs = plan_new_markets(rows, now, tz)
        reports += create_markets(specs, alert=alert, do_create=True)
    return reports, ""
