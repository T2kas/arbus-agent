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
# The LEA "tool" page (ena.lt/degalu-kainos-degalinese, ena.lt/dk-irankis) turned
# out to be a Power BI iframe with zero static price text — regex-scraping it can
# never work, confirmed live (0 matches for "dyzel"/"benzin" in the page HTML).
#
# What DOES work: LEA publishes a plain-text daily bulletin as a news post,
# stating the averages in plain sentences ("vidutinė dyzelino kaina siekė
# 1,982 Eur/l"). But the URL slug is NOT one predictable pattern — some days it
# is "ndk-YYYYMMDD" (e.g. ndk-20260727), other days a descriptive Lithuanian
# slug ("antradienio-ryta-didejo-visu-degalu-vidutines-kainos", i.e. "Tuesday
# morning, all fuel prices rose") — confirmed live: the ndk- post for a given
# date can lag a day behind the actual latest bulletin, which uses the
# descriptive slug instead. Guessing one pattern therefore risks reading a
# stale day and missing that a threshold was already crossed.
#
# The reliable fix: ena.lt/sitemap.xml lists every URL with its <lastmod> date.
# Sorting Naujiena entries by lastmod descending and trying each until one
# matches the bulletin sentence finds the ACTUAL most recent fuel-price post,
# regardless of which slug style it used that day (confirmed live: found
# 2026-07-29's post this way when the ndk- guess would have returned 07-27's).
#
# Three paths, in order:
#   1. FUEL_PRICE_URL — a JSON feed the team points us at, if they ever get one.
#   2. The sitemap walk above.
#   3. A generic regex scrape of FUEL_LEA_URL, kept only in case the team points
#      it at some other (genuinely static) HTML page later.

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


# "vidutinė dyzelino kaina siekė 1,982 Eur/l" / "vidutinė benzino kaina sudarė
# 1,773 Eur/l" — the bulletin's own summary sentence. Anchoring on "vidutinė ...
# kaina (siekė|sudarė)" is what skips the day-over-day comparison figure that
# follows in the same paragraph ("... nei penktadienį ..., kai buvo 1,979 Eur/l").
_FUEL_BULLETIN_RE = re.compile(
    r"vidutin\w*\s+(dyzelino|benzino|SND)\s+kaina\s+(?:siek[ėe]|sudar[ėe])\s+"
    r"([\d]+,[\d]+)\s*Eur/l", re.I)
_SITEMAP_NAUJIENA_RE = re.compile(
    r"<url><loc>(https://www\.ena\.lt/Naujiena/[^<]+)</loc><lastmod>([\d-]+)</lastmod>")


_FUEL_LABELS = {"dyzelino": "dyzelinas", "benzino": "benzinas", "snd": "SND (dujos)"}
_FUEL_URL_RE = re.compile(r"degal|dyzelin|benzin", re.I)
# The LT month + day a bulletin is about ("liepos 28 d."), used to date a price.
_BULLETIN_DATE_RE = re.compile(
    r"(sausio|vasario|kovo|balandžio|gegužės|birželio|liepos|rugpjūčio|"
    r"rugsėjo|spalio|lapkričio|gruodžio)\s+\d+\s+d\.", re.I)


def _fuel_prices(html: str) -> dict[str, float]:
    """{'dyzelinas': 2.069, ...} from one bulletin, or {} if it is not one."""
    found: dict[str, float] = {}
    for kind, num in _FUEL_BULLETIN_RE.findall(html):
        label = _FUEL_LABELS.get(kind.lower(), kind)
        if label not in found:
            found[label] = float(num.replace(",", "."))
    return found


def _bulletin_date(html: str) -> str:
    m = _BULLETIN_DATE_RE.search(html)
    return m.group(0) if m else ""


def parse_fuel_bulletin(html: str) -> str:
    found = _fuel_prices(html)
    if not found:
        return ""
    parts = ", ".join(f"{lbl} {val:.3f} €/l" for lbl, val in found.items())
    return f"Vidutinės degalų kainos: {parts}."


def recent_naujiena_urls(sitemap_xml: str, limit: int = 20) -> list[str]:
    """Naujiena (news post) URLs from the sitemap, most recently modified first."""
    entries = _SITEMAP_NAUJIENA_RE.findall(sitemap_xml)
    entries.sort(key=lambda e: e[1], reverse=True)
    return [url for url, _ in entries[:limit]]


def recent_fuel_bulletin_urls(sitemap_xml: str, limit: int) -> list[str]:
    """Just the fuel-price bulletin URLs, newest first — so we can read a run of
    days without fetching every unrelated news post."""
    urls = recent_naujiena_urls(sitemap_xml, 5000)
    fuel = [u for u in urls if _FUEL_URL_RE.search(u) and "kain" in u.lower()]
    return fuel[:limit]


def fuel_bulletin_fact(candidates: int | None = None) -> str:
    """LEA fuel prices as a fact, WITH the period high — not just today.

    Fuel-threshold markets ask "did the average reach X at least once by
    <deadline>", so the latest day alone gives a wrong answer: diesel read 2,030
    €/l on liepos 30 but had hit 2,069 on liepos 28, so a ≥2,05 market is already
    "Taip", not "dar neaišku". The daily post exposes no history, but the sitemap
    lists ~75 past bulletins — so we walk the freshest `candidates` of them and
    report the running high (with the day it occurred), the way the stock feed
    reports the year's high. That lets a fact-only check (0 searches) resolve the
    threshold correctly instead of missing an earlier crossing.

    Coverage is the recent window the sitemap exposes, not all history — enough
    for the weeks-long windows these markets use; stated in the fact so it is not
    mistaken for the all-time high.
    """
    if candidates is None:
        candidates = config.FUEL_BULLETIN_LOOKBACK
    resp = requests.get("https://www.ena.lt/sitemap.xml",
                        headers={"User-Agent": UA}, timeout=20)
    resp.raise_for_status()

    latest: tuple[str, dict[str, float], str] | None = None
    highs: dict[str, tuple[float, str, str]] = {}       # label -> (price, date, url)
    days = 0
    for url in recent_fuel_bulletin_urls(resp.text, candidates):
        try:
            page = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        except Exception as exc:
            log.debug("fuel bulletin candidate %s failed: %s", url, exc)
            continue
        if page.status_code != 200:
            continue
        prices = _fuel_prices(page.text)
        if not prices:
            continue
        when = _bulletin_date(page.text)
        if latest is None:                              # first hit = newest day
            latest = (when, prices, url)
        for label, val in prices.items():
            if label not in highs or val > highs[label][0]:
                highs[label] = (val, when, url)         # remember WHERE the high is
        days += 1

    if latest is None:
        return ""
    when, prices, url = latest
    now_str = ", ".join(f"{lbl} {v:.3f} €/l" for lbl, v in prices.items())
    # Each high carries its own bulletin URL, so a threshold verdict cites the day
    # that actually shows the high — not the latest day, which may be back below it.
    high_str = "; ".join(
        f"{lbl} {v:.3f} €/l ({d or 'data nenurodyta'}, {u})"
        for lbl, (v, d, u) in highs.items())
    return (f"Vidutinės degalų kainos — naujausia{f' ({when})' if when else ''}: "
            f"{now_str} (šaltinis {url}). Laikotarpio (pastarosios ~{days} "
            f"paskelbtos dienos) aukščiausios kainos: {high_str}. "
            f"Šaltinis: Lietuvos energetikos agentūra (ena.lt).")


def fuel_fact(question: str) -> str:
    q = question.lower()
    if "degal" not in q and "dyzel" not in q and "benzin" not in q and "kuro" not in q:
        return ""
    if config.FUEL_PRICE_URL:
        try:
            fact = parse_fuel(_get_json(config.FUEL_PRICE_URL))
            if fact:
                return fact
        except Exception as exc:
            log.debug("fuel JSON feed failed: %s", exc)
    try:
        fact = fuel_bulletin_fact()
        if fact:
            return fact
    except Exception as exc:
        log.debug("fuel bulletin walk failed: %s", exc)
    try:
        resp = requests.get(config.FUEL_LEA_URL,
                            headers={"User-Agent": UA}, timeout=20)
        resp.raise_for_status()
        return parse_fuel_html(resp.text)
    except Exception as exc:
        log.debug("fuel LEA scrape failed: %s", exc)
        return ""


# Kept for a generic HTML aggregator FUEL_LEA_URL might point at later — the
# ena.lt "tool" page itself is a Power BI iframe and will never match this.
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
