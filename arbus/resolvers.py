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
_DATE_RE = re.compile(r"(20\d{2})[-.\s]?(\d{1,2})[-.\s]?(\d{1,2})")


def _weather_target(question: str) -> tuple[str, str] | None:
    """(station code, ISO date) if this is a Lithuanian temperature market."""
    q = question.lower()
    if "temperat" not in q and "°c" not in q and "karšt" not in q and "šalt" not in q:
        return None
    station = next((code for key, code in _STATION_BY_CITY.items() if key in q), None)
    if station is None:
        return None
    m = _DATE_RE.search(question)
    if not m:
        return None
    try:
        iso = date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None
    return station, iso


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


def weather_fact(question: str) -> str:
    target = _weather_target(question)
    if not target:
        return ""
    station, iso = target
    if iso > date.today().isoformat():        # the day is not over yet
        return ""
    try:
        url = f"https://api.meteo.lt/v1/stations/{station}/observations/{iso}"
        return parse_weather(_get_json(url), iso)
    except Exception as exc:
        log.debug("weather fact %s %s failed: %s", station, iso, exc)
        return ""


# ── Fuel: LHMT/LEA publish no keyless JSON feed we can rely on yet ───────────
# Fuel-price markets (LEA average diesel/petrol) are common and the model cannot
# read the LEA portal, so it hallucinates "no data". There is no stable open
# endpoint today; FUEL_PRICE_URL can point at one when the team finds/builds it
# (e.g. an internal scrape), and the parser is ready for a {"diesel":.., "petrol":..}
# shape. Until then this returns "" and the check falls back to searching.

def parse_fuel(payload: dict) -> str:
    parts = []
    for key, label in (("diesel", "dyzelinas"), ("petrol", "benzinas"),
                       ("dyzelinas", "dyzelinas"), ("benzinas", "benzinas")):
        val = payload.get(key)
        if isinstance(val, (int, float)):
            parts.append(f"{label} {val:.3f} €/l")
    if not parts:
        return ""
    return ("Vidutinės degalų kainos (LEA): " + ", ".join(parts)
            + ". Šaltinis: Lietuvos energetikos agentūra.")


def fuel_fact(question: str) -> str:
    q = question.lower()
    if "degal" not in q and "dyzel" not in q and "benzin" not in q:
        return ""
    if not config.FUEL_PRICE_URL:
        return ""
    try:
        return parse_fuel(_get_json(config.FUEL_PRICE_URL))
    except Exception as exc:
        log.debug("fuel fact failed: %s", exc)
        return ""


# ── Public API ───────────────────────────────────────────────────────────────

_RESOLVERS = (stock_fact, weather_fact, fuel_fact)


def facts_for(question: str) -> str:
    """Authoritative facts relevant to this market, newline-joined ('' if none).

    Never raises: a data feed being down must not stop a resolution check.
    """
    facts = []
    for resolver in _RESOLVERS:
        try:
            fact = resolver(question)
        except Exception as exc:               # defensive: this runs live
            log.debug("resolver %s failed: %s", resolver.__name__, exc)
            fact = ""
        if fact:
            facts.append(fact)
    return "\n".join(facts)
