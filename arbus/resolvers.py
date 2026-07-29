"""Authoritative data feeds for resolution — facts, not the model's memory.

The AI check kept hallucinating exactly the values a market resolves on: a
stock price it never looked up, a temperature it could not find, a fuel price
behind a portal it cannot read. The fix is to stop asking the model for those
numbers at all. When a frozen market is about a stock, the weather or fuel, this
module fetches the real figure from an official/keyless feed and hands it to the
check as a FACT, so the model's only job is to read a number we already have.

Each resolver is best-effort and fail-safe: it returns a short Lithuanian fact
string, or "" when it does not apply or the feed is unreachable. A missing fact
just means the model searches as before — it never blocks or errors a check.

Fetchers (network) are split from parsers (pure) so the parsers stay testable
offline, the same discipline as pulse.py and harvest.py.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta, timezone

import requests

from . import config

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; ArbusMarketAgent/1.0; +https://arbus.lt)"


def _get_json(url: str, timeout: int = 20) -> dict:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                        allow_redirects=True)
    resp.raise_for_status()
    return resp.json()


# ── Stocks: Nasdaq Vilnius via Yahoo chart JSON (keyless) ────────────────────
# The pulse already quotes these tickers; here we need the period HIGH, because
# the markets ask "did it close above X at least once by <date>", not today's
# price. Yahoo's daily closes over the range give exactly that.

def _ticker_for(question: str) -> tuple[str, str] | None:
    q = question.lower()
    for ticker, name in config.NASDAQ_VILNIUS_TICKERS:
        base = ticker.split(".")[0].lower()          # IGN1L
        if base in q or name.lower() in q or name.split()[0].lower() in q:
            return ticker, name
    return None


def parse_stock(payload: dict, name: str) -> str:
    """Current price + highest daily close in the range, as a LT fact."""
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return ""
    meta = result.get("meta", {})
    closes = [c for c in (result.get("indicators", {}).get("quote", [{}])[0]
                          .get("close") or []) if c is not None]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    if price is None:
        return ""
    cur = "€" if meta.get("currency", "EUR") == "EUR" else meta.get("currency", "")
    high = max(closes) if closes else price
    return (f"{name} ({meta.get('symbol', '')}): dabartinė kaina "
            f"{price:.2f} {cur}, laikotarpio aukščiausias uždarymas "
            f"{high:.2f} {cur}. Šaltinis: Nasdaq Baltic / Yahoo Finance.").strip()


def stock_fact(question: str) -> str:
    hit = _ticker_for(question)
    if not hit:
        return ""
    ticker, name = hit
    for host in ("query1", "query2"):
        try:
            url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                   f"{ticker}?range=1y&interval=1d")
            fact = parse_stock(_get_json(url), name)
            if fact:
                return fact
        except Exception as exc:
            log.debug("stock fact %s via %s failed: %s", ticker, host, exc)
    return ""


# ── Weather: meteo.lt open API (keyless, official LHMT data) ─────────────────
# api.meteo.lt serves LHMT observations per station and day. A market asking the
# max temperature in Vilnius on a past date is decidable the moment the day is
# over — no searching, just the day's highest airTemperature.

_STATION_BY_CITY = {
    "vilni": "vilniaus-ams", "kaun": "kauno-ams", "klaip": "klaipedos-ams",
    "šiaul": "siauliu-ams", "siaul": "siauliu-ams", "panevėž": "panevezio-ams",
    "paneve": "panevezio-ams",
}
# Lithuanian month names in the genitive, as they appear in questions
# ("liepos 25"). Index 1..12.
_LT_MONTHS = {
    "sausio": 1, "vasario": 2, "kovo": 3, "balandžio": 4, "balandzio": 4,
    "gegužės": 5, "geguzes": 5, "birželio": 6, "birzelio": 6, "liepos": 7,
    "rugpjūčio": 8, "rugpjucio": 8, "rugsėjo": 9, "rugsejo": 9, "spalio": 10,
    "lapkričio": 11, "lapkricio": 11, "gruodžio": 12, "gruodzio": 12,
}
_ISO_RE = re.compile(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})")
_LT_DATE_RE = re.compile(
    r"(" + "|".join(_LT_MONTHS) + r")\s+(\d{1,2})\D{0,8}(20\d{2})", re.I)


def _parse_date(text: str, fallback_iso: str = "") -> str:
    """Find a date in the question, ISO ('2026-07-25') or Lithuanian
    ('liepos 25, 2026'). Falls back to the market's closes_at if given."""
    m = _ISO_RE.search(text)
    if m:
        y, mo, d = m.groups()
    else:
        m = _LT_DATE_RE.search(text)
        if not m:
            return fallback_iso[:10]
        mo, d, y = _LT_MONTHS[m.group(1).lower()], m.group(2), m.group(3)
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except (ValueError, TypeError):
        return fallback_iso[:10]


def _weather_target(question: str, closes_at: str = "") -> tuple[str, str] | None:
    """(station code, ISO date) if this is a Lithuanian temperature market."""
    q = question.lower()
    if "temperat" not in q and "°c" not in q and "karšt" not in q and "šalt" not in q:
        return None
    station = next((code for key, code in _STATION_BY_CITY.items() if key in q), None)
    if station is None:
        return None
    iso = _parse_date(question, closes_at)
    return (station, iso) if iso else None


def parse_weather(payload: dict, iso_date: str) -> str:
    """Highest airTemperature recorded that day, as a LT fact."""
    obs = payload.get("observations") or []
    temps = [o.get("airTemperature") for o in obs
             if isinstance(o.get("airTemperature"), (int, float))]
    if not temps:
        return ""
    hi = max(temps)
    return (f"Aukščiausia užfiksuota oro temperatūra {iso_date} "
            f"({payload.get('station', {}).get('name', '')}): {hi:.1f} °C "
            f"(suapvalinta {round(hi)} °C). Šaltinis: LHMT / api.meteo.lt.").strip()


def weather_fact(question: str, closes_at: str = "") -> str:
    target = _weather_target(question, closes_at)
    if not target:
        return ""
    station, iso = target
    if not iso or iso > date.today().isoformat():   # no date, or day not over yet
        return ""
    try:
        url = f"https://api.meteo.lt/v1/stations/{station}/observations/{iso}"
        return parse_weather(_get_json(url), iso)
    except Exception as exc:
        log.debug("weather fact %s %s failed: %s", station, iso, exc)
        return ""


# ── Fuel: official source is LEA / ena.lt (daily averages), no clean JSON API ─
# The LEA portal (ena.lt/degalu-kainos-degalinese) is the authoritative source
# the markets cite and updates every working day, but it bot-blocks simple
# fetchers and exposes no documented JSON. Two paths, in order:
#   1. FUEL_PRICE_URL — a JSON feed the team points us at (an internal scrape,
#      or an aggregator like degalu-kaina.lt if it exposes one). parse_fuel
#      reads {"diesel":.., "petrol":..} or the Lithuanian keys.
#   2. Best-effort scrape of the LEA page with a browser User-Agent, pulling the
#      average diesel/petrol price out of the HTML with a regex.
# Either way the fact cites LEA, and a failure just falls back to searching.

def parse_fuel(payload: dict) -> str:
    parts = []
    for key, label in (("diesel", "dyzelinas"), ("petrol", "benzinas"),
                       ("dyzelinas", "dyzelinas"), ("benzinas", "benzinas"),
                       ("gasoline", "benzinas")):
        val = payload.get(key)
        if isinstance(val, (int, float)) and label not in " ".join(parts):
            parts.append(f"{label} {val:.3f} €/l")
    if not parts:
        return ""
    return ("Vidutinės degalų kainos (LEA): " + ", ".join(parts)
            + ". Šaltinis: Lietuvos energetikos agentūra (ena.lt).")


# "Dyzelinas 1,832 €/l", "95 benzinas – 1,723 EUR" etc. on the LEA page.
_FUEL_HTML_RE = re.compile(
    r"(dyzelin|benzin|95|98|dujo|lpg)[^0-9]{0,40}?(\d[.,]\d{2,3})\s*(?:€|eur)",
    re.I)


def parse_fuel_html(html: str) -> str:
    found: dict[str, float] = {}
    for kind, num in _FUEL_HTML_RE.findall(html):
        k = kind.lower()
        label = ("dyzelinas" if "dyzel" in k
                 else "dujos/LPG" if ("duj" in k or "lpg" in k)
                 else "benzinas")
        val = float(num.replace(",", "."))
        if 0.3 < val < 5 and label not in found:      # sane €/l range
            found[label] = val
    if not found:
        return ""
    parts = ", ".join(f"{lbl} {val:.3f} €/l" for lbl, val in found.items())
    return (f"Vidutinės degalų kainos (LEA): {parts}. "
            "Šaltinis: Lietuvos energetikos agentūra (ena.lt).")


def fuel_fact(question: str) -> str:
    q = question.lower()
    if "degal" not in q and "dyzel" not in q and "benzin" not in q and "kuro" not in q:
        return ""
    if config.FUEL_PRICE_URL:
        try:
            return parse_fuel(_get_json(config.FUEL_PRICE_URL))
        except Exception as exc:
            log.debug("fuel JSON feed failed: %s", exc)
    try:
        resp = requests.get(config.FUEL_LEA_URL,
                            headers={"User-Agent": UA}, timeout=20)
        resp.raise_for_status()
        return parse_fuel_html(resp.text)
    except Exception as exc:
        log.debug("fuel LEA scrape failed: %s", exc)
        return ""


# ── Public API ───────────────────────────────────────────────────────────────

def diagnose(question: str, closes_at: str = "") -> list[tuple[str, str, str]]:
    """Per-feed status for one market: (feed, fact, error).

    `facts_for` hides failures as empty strings; this exposes them so `arbus
    facts` can tell "this feed does not apply" from "meteo.lt/Yahoo refused the
    request" — the difference between a design gap and a network problem.
    """
    out: list[tuple[str, str, str]] = []

    hit = _ticker_for(question)
    if hit:
        ticker, name = hit
        fact, err = "", ""
        for host in ("query1", "query2"):
            try:
                fact = parse_stock(_get_json(
                    f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                    f"{ticker}?range=1y&interval=1d"), name)
                if fact:
                    break
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
        out.append(("akcijos", fact, "" if fact else (err or "atsakyme nebuvo kainos")))

    target = _weather_target(question, closes_at)
    if target:
        station, iso = target
        fact, err = "", ""
        if iso > date.today().isoformat():
            err = f"data {iso} dar ateityje"
        else:
            try:
                fact = parse_weather(_get_json(
                    f"https://api.meteo.lt/v1/stations/{station}/observations/{iso}"), iso)
            except Exception as exc:
                err = f"{type(exc).__name__}: {exc}"
        out.append(("oras", fact, "" if fact else (err or "nėra matavimų tą dieną")))

    if any(k in question.lower() for k in ("degal", "dyzel", "benzin", "kuro")):
        try:
            fact = fuel_fact(question)
        except Exception as exc:
            fact = f""
            out.append(("degalai", "", f"{type(exc).__name__}: {exc}"))
        else:
            out.append(("degalai", fact, "" if fact else "LEA neatidavė duomenų"))

    return out


def facts_for(question: str, closes_at: str = "") -> str:
    """Authoritative facts relevant to this market, newline-joined ('' if none).

    Never raises: a data feed being down must not stop a resolution check.
    `closes_at` (the market's deadline) helps date-based feeds when the question
    itself is vague.
    """
    resolvers = (
        lambda q: stock_fact(q),
        lambda q: weather_fact(q, closes_at),
        lambda q: fuel_fact(q),
    )
    facts = []
    for resolver in resolvers:
        try:
            fact = resolver(question)
        except Exception as exc:               # defensive: this runs live
            log.debug("resolver failed: %s", exc)
            fact = ""
        if fact:
            facts.append(fact)
    return "\n".join(facts)
