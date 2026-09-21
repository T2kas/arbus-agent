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

import html as _html
import io
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urljoin

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


# ── Cinema: Lietuvos kino centras TOP reports (.xlsx) ────────────────────────
# "Most-watched film" markets resolve on the LKC reports (all on the same site):
#   • WEEKLY  — "Savaitės (Weekly) TOP YYYY.MM.DD-YYYY.MM.DD.xlsx" (column ADM),
#   • MONTHLY — "YYYY <Mėnuo>.xlsx" with a per-month sheet ("Žiūrovų skaičius"),
#   • YEARLY  — "YYYY TOP …_lkc_suvestine.xlsx", sheet "YYYY".
# We take the MAX viewers (not revenue rank #1), optionally only Lithuanian films
# ("Kilmės šalis" = LT), skipping the totals row.
_LKC_BASE = "https://www.lkc.lt"
_LKC_REPORTS_URL = _LKC_BASE + "/registras-ir-statistika/faktai-ir-statistika/naujausios-ataskaitos"
_LKC_YEARLY_URL = _LKC_BASE + "/registras-ir-statistika/faktai-ir-statistika/metines-ataskaitos"
_LKC_ARCHIVE_URL = _LKC_BASE + "/registras-ir-statistika/faktai-ir-statistika/archyvas"
_IKI_DAY_RE = re.compile(r"iki\s+(?:\w+\s+)?(\d{1,2})\s*d", re.I)
_CINEMA_TOTAL_RE = re.compile(r"(?i)^\s*(total\b|iš\s*viso|is\s*viso|viso\b|bendra|suma|grand)")
# Diacritic-insensitive month stems → number (matches genitive/accusative/nominative
# in rules AND file/sheet names): "rugsėjo"/"rugsėjį"/"Rugsėjis" all hold "rugsej".
_MONTH_STEMS = {"sausi": 1, "vasari": 2, "kov": 3, "baland": 4, "geguz": 5,
                "birzel": 6, "liep": 7, "rugpjut": 8, "rugpjuc": 8, "rugsej": 9,
                "spal": 10, "lapkrit": 11, "gruod": 12}
_MONTH_STEM_BY_NUM = {1: "sausi", 2: "vasari", 3: "kov", 4: "baland", 5: "geguz",
                      6: "birzel", 7: "liep", 8: "rugpjut", 9: "rugsej", 10: "spal",
                      11: "lapkrit", 12: "gruod"}
_MONTH_NAME_LT = ["", "sausis", "vasaris", "kovas", "balandis", "gegužė", "birželis",
                  "liepa", "rugpjūtis", "rugsėjis", "spalis", "lapkritis", "gruodis"]


def _cnorm(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return s.lower()


def _cinema_target(question: str, rules: str) -> tuple[str, str] | None:
    """(start_iso, end_iso) if this is an LKC WEEKLY most-watched-film market."""
    text = f"{question}\n{rules}"
    low = text.lower()
    if "kino" not in low or not any(k in low for k in ("žiūrov", "ziurov", "adm", "film")):
        return None
    year_m = re.search(r"20\d{2}", low)
    if not year_m:
        return None
    year = int(year_m.group())
    pairs = [(_LT_MONTHS[mo], int(d)) for mo, d in
             re.findall(r"(" + "|".join(_LT_MONTHS) + r")\s+(\d{1,2})", low)]
    try:
        span = None
        if len(pairs) >= 2:
            ds = sorted(date(year, mo, d) for mo, d in pairs)
            span = (ds[0], ds[-1])
        elif len(pairs) == 1:
            mo, d1 = pairs[0]
            m2 = _IKI_DAY_RE.search(low)
            if m2:
                span = tuple(sorted([date(year, mo, d1), date(year, mo, int(m2.group(1)))]))
    except ValueError:
        return None
    if not span or (span[1] - span[0]).days > 8:     # >8 days = not a week
        return None
    return span[0].isoformat(), span[1].isoformat()


def _dated_month(question: str, rules: str) -> int | None:
    """The month tied to a specific DATE ('rugsėjo 1 d.') — a monthly market's
    evaluation period. This is what makes month detection safe: a September
    market's rules also mention 'rugpjūčio arba spalio dienas' as a caveat, and
    picking the first month by dict order chose AUGUST, so the bot grabbed the
    already-published August report and resolved a September market on it. Only a
    month next to a day-of-month is the real period; incidental mentions are
    ignored. Title first, then rules; earliest such month wins."""
    for text in (question, rules):
        low = _cnorm(text)
        hits = []
        for stem, num in _MONTH_STEMS.items():
            m = re.search(re.escape(stem) + r"\w*\s+\d{1,2}\s*d", low)
            if m:
                hits.append((m.start(), num))
        if hits:
            return min(hits)[1]
    return None


def _any_month(question: str, rules: str) -> int | None:
    """Fallback: the earliest month mentioned at all (title before rules), for a
    monthly market that names its month without a '1 d.' date range."""
    for text in (question, rules):
        low = _cnorm(text)
        hits = sorted((low.find(stem), num)
                      for stem, num in _MONTH_STEMS.items() if stem in low)
        if hits:
            return hits[0][1]
    return None


def _cinema_period(question: str, rules: str):
    """('weekly', start, end) | ('monthly', year, month) | ('yearly', year) | None."""
    low = _cnorm(f"{question}\n{rules}")
    # Must be a most-watched-FILM market: a film/cinema word AND a viewers signal.
    # Requiring the viewers signal stops unrelated markets that merely contain
    # "kino"/"film" (e.g. a border-control market) being treated as cinema.
    if "film" not in low and "kino" not in low:
        return None
    if not any(k in low for k in ("ziurov", "ziurim", "adm", "kino centr")):
        return None
    weekly = _cinema_target(question, rules)
    if weekly:
        return ("weekly", weekly[0], weekly[1])
    ym = re.search(r"20\d{2}", low)
    if not ym:
        return None
    year = int(ym.group())
    # Whole-year evaluation FIRST, before any month date: a yearly market's rules
    # carry a report deadline ('vasario 28 d.') that would otherwise be read as
    # the evaluation month and settle the year market on February data.
    if re.search(r"per\s+vis|vis\w*\s+20\d{2}\s*m|metin", low):
        return ("yearly", year)
    # A month tied to a real date is the monthly evaluation period.
    dated = _dated_month(question, rules)
    if dated:
        return ("monthly", year, dated)
    if re.search(r"\bmet(ais|us|u)\b", low):
        return ("yearly", year)
    month = _any_month(question, rules)
    if month:
        return ("monthly", year, month)
    return None


def _is_lt_film_market(text: str) -> bool:
    return "lietuvisk" in _cnorm(text)


def _lkc_weekly_url(start_iso: str, end_iso: str, page_html: str) -> str:
    """The Weekly (not Weekend) .xlsx whose name carries this date range."""
    hrefs = re.findall(r'href="([^"]+\.xlsx)"', page_html, re.I)
    s, e = start_iso.replace("-", "."), end_iso.replace("-", ".")
    for want_both in (True, False):                  # prefer both dates, else end date
        for h in hrefs:
            if "weekly" in h.lower() and e in h and (s in h or not want_both):
                return urljoin(_LKC_BASE, quote(h))
    return ""


def _lkc_monthly_url(year: int, month: int, *page_htmls: str) -> str:
    stem, y = _MONTH_STEM_BY_NUM[month], str(year)
    for html in page_htmls:
        for h in re.findall(r'href="([^"]+\.xlsx)"', html, re.I):
            hn = _cnorm(h)
            if (y in hn and stem in hn and "weekl" not in hn and "weekend" not in hn
                    and "savait" not in hn and " top" not in hn):
                return urljoin(_LKC_BASE, quote(h))
    return ""


def _lkc_yearly_url(year: int, page_html: str) -> str:
    y = str(year)
    for h in re.findall(r'href="([^"]+\.xlsx)"', page_html, re.I):
        hn = _cnorm(h)
        if y in hn and "top" in hn:
            return urljoin(_LKC_BASE, quote(h))
    return ""


def _read_film_rows(ws) -> list[tuple[str, float, str]]:
    """(film, viewers, country) from a TOP sheet — weekly (ADM) or monthly/yearly
    ('Žiūrovų skaičius'), skipping the totals row. Picks the period-viewers column,
    never the 'Bendras/Total' cumulative one."""
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    header_i = header = None
    for i, r in enumerate(rows[:15]):
        jn = _cnorm(" ".join(str(c or "") for c in r))
        if ("filmas" in jn or "movie" in jn or "pavadinim" in jn) and ("adm" in jn or "ziurov" in jn):
            header_i = i
            header = [_cnorm(str(c or "")) for c in r]
            break
    if header_i is None:
        return []
    film_col = next((k for k, c in enumerate(header) if "pavadinim" in c and "orgin" not in c), None)
    if film_col is None:
        film_col = next((k for k, c in enumerate(header)
                         if "pavadinim" in c or "filmas" in c or "movie" in c), None)
    view_cands = [k for k, c in enumerate(header)
                  if ("adm" in c or "ziurov" in c) and "bendr" not in c and "total" not in c]
    view_col = view_cands[0] if view_cands else None
    country_col = next((k for k, c in enumerate(header) if "kilm" in c or "salis" in c), None)
    if film_col is None or view_col is None:
        return []
    out = []
    for r in rows[header_i + 1:]:
        if film_col >= len(r) or view_col >= len(r) or not r[film_col]:
            continue
        name = str(r[film_col]).strip()
        if _CINEMA_TOTAL_RE.match(name):
            continue
        try:
            viewers = float(str(r[view_col]).replace("\xa0", "").replace(" ", "").replace(",", "."))
        except (TypeError, ValueError):
            continue
        country = ""
        if country_col is not None and country_col < len(r) and r[country_col] is not None:
            country = str(r[country_col]).strip()
        out.append((name, viewers, country))
    return out


def _load_wb(content: bytes):
    try:
        import openpyxl
    except ImportError:
        log.warning("cinema resolver needs openpyxl (in requirements.txt)")
        return None
    return openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)


def parse_cinema_xlsx(content: bytes, top: int = 6) -> list[tuple[str, float]]:
    """(film, viewers) sorted desc from a workbook's first sheet — weekly usage."""
    wb = _load_wb(content)
    if wb is None:
        return []
    rows = sorted(_read_film_rows(wb.worksheets[0]), key=lambda x: x[1], reverse=True)
    return [(n, v) for n, v, _c in rows[:top]]


def _select_sheet(wb, period):
    kind = period[0]
    if kind == "weekly":
        return wb.worksheets[0]
    if kind == "monthly":
        stem = _MONTH_STEM_BY_NUM[period[2]]
        for name in wb.sheetnames:
            if stem in _cnorm(name):
                return wb[name]
        return None
    for name in wb.sheetnames:                        # yearly: the "YYYY" sheet
        if _cnorm(name).strip() == str(period[1]):
            return wb[name]
    for name in wb.sheetnames:                        # fallback: first film table
        if _read_film_rows(wb[name]):
            return wb[name]
    return None


def _month_last_day(year: int, month: int) -> date:
    return (date(year, 12, 31) if month == 12
            else date(year, month + 1, 1) - timedelta(days=1))


def _get_text(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    r.encoding = "utf-8"
    return r.text


_LKC_WEEK_SPAN_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})\s*-\s*(\d{4})\.(\d{2})\.(\d{2})")


def latest_lkc_week(page_html: str = "") -> tuple[str, str] | None:
    """(start_iso, end_iso) of the most recently PUBLISHED LKC weekly report,
    read from the weekly .xlsx filenames on the reports page. The series creator
    uses this to line a new weekly market's date span up with a real report file
    (so the resolver's filename match will find it), then rolls it forward a week.

    Best-effort: returns None if the page is unreachable or has no weekly file.
    """
    try:
        html = page_html or _get_text(_LKC_REPORTS_URL)
    except Exception as exc:                              # noqa: BLE001
        log.debug("latest_lkc_week fetch failed: %s", exc)
        return None
    spans = []
    for h in re.findall(r'href="([^"]+\.xlsx)"', html, re.I):
        if "weekly" not in h.lower():
            continue
        m = _LKC_WEEK_SPAN_RE.search(_html.unescape(h))
        if not m:
            continue
        try:
            s = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            e = date(int(m.group(4)), int(m.group(5)), int(m.group(6)))
        except ValueError:
            continue
        if e >= s and (e - s).days <= 8:
            spans.append((s, e))
    if not spans:
        return None
    s, e = max(spans, key=lambda se: se[1])              # newest by end date
    return s.isoformat(), e.isoformat()


def cinema_top(question: str, rules: str = "") -> dict | None:
    """Structured LKC result for a most-watched-film market (weekly/monthly/
    yearly), or None if it does not apply / the period is not over / the report
    is not published. Returns {desc, url, top:[(film,viewers)], lt_only, kind}."""
    period = _cinema_period(question, rules)
    if not period:
        return None
    lt_only = _is_lt_film_market(f"{question}\n{rules}")
    today = date.today().isoformat()
    kind = period[0]
    try:
        if kind == "weekly":
            start, end = period[1], period[2]
            if end > today:
                return None
            url = _lkc_weekly_url(start, end, _get_text(_LKC_REPORTS_URL))
            desc = f"{start}–{end}"
        elif kind == "monthly":
            year, month = period[1], period[2]
            if _month_last_day(year, month).isoformat() > today:
                return None
            url = _lkc_monthly_url(year, month, _get_text(_LKC_REPORTS_URL),
                                   _get_text(_LKC_ARCHIVE_URL))
            desc = f"{year} m. {_MONTH_NAME_LT[month]}"
        else:  # yearly
            year = period[1]
            if date(year, 12, 31).isoformat() > today:
                return None
            url = _lkc_yearly_url(year, _get_text(_LKC_YEARLY_URL))
            desc = f"{year} m."
        if not url:
            return None
        wb = _load_wb(requests.get(url, headers={"User-Agent": UA}, timeout=60).content)
        if wb is None:
            return None
        ws = _select_sheet(wb, period)
        if ws is None:
            return None
        rows = _read_film_rows(ws)
    except Exception as exc:
        log.debug("cinema %s failed: %s", period, exc)
        return None
    if lt_only:
        rows = [r for r in rows if r[2].strip().upper() == "LT" or "LIETUV" in r[2].upper()]
    top = sorted(((n, v) for n, v, _c in rows), key=lambda x: x[1], reverse=True)[:8]
    if not top:
        return None
    return {"desc": desc, "url": url, "top": top, "lt_only": lt_only, "kind": kind}


def cinema_fact(question: str, rules: str = "", closes_at: str = "") -> str:
    res = cinema_top(question, rules)
    if not res:
        return ""
    winner = res["top"][0]
    scope = "lietuviškų filmų " if res["lt_only"] else ""
    listing = ", ".join(f"„{name}“ {int(v)}" for name, v in res["top"])
    return (f"Lietuvos kino centro {scope}TOP {res['desc']} pagal žiūrovų skaičių: "
            f"daugiausiai surinko „{winner[0]}“ ({int(winner[1])} žiūr.). "
            f"TOP: {listing}. Šaltinis: Lietuvos kino centras ({res['url']}).")


# ── Music: AGATA weekly singles TOP 100 (HTML table) ─────────────────────────
# "Which song is #1 this week" resolves on the official AGATA chart. The weekly
# article lives at /lt/naujienos/s<week>-5/ and its title carries the week
# ("2026 37-os savaitės klausomiausi (TOP 100)"); the table columns are
# Vieta | Praeitą savaitę | Savaičių tope | Atlikėjas/grupė | Pavadinimas.
_AGATA_BASE = "https://www.agata.lt"
_AGATA_LIST_URL = _AGATA_BASE + "/lt/naujienos/?cat=top-100"
_AGATA_WEEK_RE = re.compile(r"(\d{1,2})\s*-?\s*os(?:ios)?\s+savait")


def _agata_target(question: str, rules: str) -> tuple[int, int] | None:
    """(year, week) if this is an AGATA weekly #1-song market."""
    low = _cnorm(f"{question}\n{rules}")
    if "agata" not in low:                            # authoritative signal
        return None
    ym = re.search(r"20\d{2}", low)
    wm = _AGATA_WEEK_RE.search(low)
    if not ym or not wm:
        return None
    week = int(wm.group(1))
    if not 1 <= week <= 53:
        return None
    return int(ym.group()), week


def _agata_page_week(html: str) -> tuple[int, int] | None:
    m = re.search(r"<title>(.*?)</title>", html, re.I | re.S)
    title = _cnorm(_html.unescape(m.group(1))) if m else ""
    ym = re.search(r"20\d{2}", title)
    wm = _AGATA_WEEK_RE.search(title)
    if ym and wm:
        return int(ym.group()), int(wm.group(1))
    return None


def _agata_url(year: int, week: int) -> tuple[str, str] | None:
    """(url, html) of the chart for (year, week), verified by its title."""
    cand = f"{_AGATA_BASE}/lt/naujienos/s{week}-5/"
    html = _get_text(cand)
    if _agata_page_week(html) == (year, week):
        return cand, html
    listing = _get_text(_AGATA_LIST_URL)                # fall back to the listing
    for href in re.findall(rf'href="([^"]*naujienos/s{week}-[^"]*)"', listing, re.I):
        url = urljoin(_AGATA_BASE, href)
        h = _get_text(url)
        if _agata_page_week(h) == (year, week):
            return url, h
    return None


def _cells(row: str) -> list[str]:
    return [re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
            for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.I | re.S)]


def _parse_agata_table(table_html: str) -> list[tuple[int, str, str]]:
    """(rank, artist, title) from one chart <table>, rank-sorted."""
    ci = None
    out = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.I | re.S):
        cells = _cells(row)
        if ci is None:
            j = _cnorm(" ".join(cells))
            if "vieta" in j and "pavadinim" in j and ("atlik" in j or "grup" in j):
                header = [_cnorm(c) for c in cells]
                ci = {
                    "rank": next((k for k, c in enumerate(header) if "vieta" in c), 0),
                    "artist": next((k for k, c in enumerate(header) if "atlik" in c or "grup" in c), None),
                    "title": next((k for k, c in enumerate(header) if "pavadinim" in c), None),
                }
            continue
        if ci["artist"] is None or ci["title"] is None:
            break
        if max(ci["rank"], ci["artist"], ci["title"]) >= len(cells):
            continue
        digits = re.sub(r"\D", "", cells[ci["rank"]])
        if not digits:
            continue
        if cells[ci["artist"]] or cells[ci["title"]]:
            out.append((int(digits), cells[ci["artist"]], cells[ci["title"]]))
    out.sort(key=lambda x: x[0])
    return out


def _agata_label_before(html: str, table_start: int) -> str:
    """'singles' or 'albums' from the caption just before a table ("… SINGLŲ
    TOP100" / "… ALBUMŲ TOP100"), or '' if unlabelled."""
    seg = _cnorm(re.sub(r"<[^>]+>", " ", html[max(0, table_start - 700):table_start]))
    si, ai = seg.rfind("singl"), seg.rfind("album")
    if si == -1 and ai == -1:
        return ""
    return "singles" if si > ai else "albums"


def parse_agata(html: str, want: str = "singles") -> list[tuple[int, str, str]]:
    """(rank, artist, title) from the AGATA chart. The page holds BOTH a singlų
    and an albumų TOP100 with identical headers — pick the one whose caption
    matches `want` ('singles'/'albums'); fall back to the first chart table."""
    fallback = None
    for mt in re.finditer(r"<table[^>]*>(.*?)</table>", html, re.I | re.S):
        rows = _parse_agata_table(mt.group(1))
        if not rows:
            continue
        if fallback is None:
            fallback = rows
        if _agata_label_before(html, mt.start()) == want:
            return rows
    return fallback or []


def agata_top(question: str, rules: str = "") -> dict | None:
    """Structured AGATA weekly result, or None if it does not apply / the chart
    is not published. Returns {desc, url, artist, title, top:[(artist,title),…]}."""
    tgt = _agata_target(question, rules)
    if not tgt:
        return None
    year, week = tgt
    low = _cnorm(f"{question}\n{rules}")
    want = "albums" if ("album" in low and "singl" not in low) else "singles"
    try:
        found = _agata_url(year, week)
        if not found:
            return None
        url, html = found
        chart = parse_agata(html, want)
    except Exception as exc:
        log.debug("agata %s w%s failed: %s", year, week, exc)
        return None
    if not chart:
        return None
    return {"desc": f"{year} m. {week} sav.", "url": url,
            "artist": chart[0][1], "title": chart[0][2],
            "top": [(a, t) for _r, a, t in chart[:10]]}


def agata_fact(question: str, rules: str = "", closes_at: str = "") -> str:
    res = agata_top(question, rules)
    if not res:
        return ""
    listing = ", ".join(f"{a} „{t}“" for a, t in res["top"][:5])
    return (f"AGATA singlų TOP 100, {res['desc']}: pirma vieta — {res['artist']} "
            f"„{res['title']}“. TOP: {listing}. Šaltinis: AGATA ({res['url']}).")


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


def facts_for(question: str, closes_at: str = "", rules: str = "") -> str:
    """Authoritative facts relevant to this market, newline-joined ('' if none).

    Never raises: a data feed being down must not stop a resolution check.
    `closes_at` (the market's deadline) helps date-based feeds when the question
    itself is vague; `rules` carries details some feeds need (the cinema week,
    the exact metric).
    """
    resolvers = (
        lambda q: stock_fact(q),
        lambda q: weather_fact(q, closes_at),
        lambda q: fuel_fact(q),
        lambda q: cinema_fact(q, rules, closes_at),
        lambda q: agata_fact(q, rules, closes_at),
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
