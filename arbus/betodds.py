"""Bookmaker consensus odds and team logos from BetExplorer.

BetExplorer aggregates ~20 bookmakers per match. For a game we take every
bookmaker's prices, remove each one's margin (normalise 1/odds to sum 1) and
average across bookmakers — the market's consensus probability. Fewer than
BETODDS_MIN_BOOKMAKERS prices means "odds not published yet", and the caller
does not create the market.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta

BASE = "https://www.betexplorer.com"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"}
LEAGUES = {
    "lkl": ("basketball/lithuania/lkl", "ha"),
    "toplyga": ("football/lithuania/toplyga", "1x2"),
}
MIN_BOOKMAKERS = 3

# BetExplorer team name (folded) -> our source key (LKL short name / TOPLYGA slug)
ALIASES = {
    "lkl": {
        "rytas": "Rytas", "neptunas": "Neptūnas", "lietkabelis": "Lietkabelis",
        "jonava": "Hipocredit", "hipocredit": "Hipocredit", "taurage": "Tauragė",
        "gargzdai": "Gargždai", "nevezis": "Nevėžis-Paskolų klubas",
        "zalgiris": "Žalgiris", "siauliai": "Šiauliai", "juventus": "Juventus",
    },
    "toplyga": {
        "zalgiris": "zalgiris", "kauno zalgiris": "k-zalgiris", "suduva": "suduva",
        "transinvest": "transinvest", "banga": "banga", "dziugas": "dziugas",
        "panevezys": "panevezys", "hegelmann": "hegelmann", "siauliai": "fa-siauliai",
    },
}
_DROP = {"bc", "fk", "fc", "fa", "kk", "bk", "kaunas", "klaipeda", "vilnius",
         "telsiai", "utena", "kedainiai"}


def fold(name: str) -> str:
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode().lower()
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _DROP]
    return " ".join(words)


def team_key(league: str, name: str) -> str | None:
    f = fold(name)
    aliases = ALIASES[league]
    if f in aliases:
        return aliases[f]
    # "neptunas klaipeda" -> "neptunas"; longest alias contained in the name wins
    hits = [a for a in aliases if re.search(rf"\b{re.escape(a)}\b", f)]
    return aliases[max(hits, key=len)] if hits else None


@dataclass
class Fixture:
    home: str               # our key
    away: str
    day: tuple[int, int]    # (day, month) as BetExplorer shows it
    url: str                # match page path
    match_id: str


def _get(url: str, **headers) -> str:
    import requests

    r = requests.get(url, headers={**UA, **headers}, timeout=30)
    r.raise_for_status()
    return r.text


def parse_fixtures(league: str, page: str) -> list[Fixture]:
    out, last_day = [], None
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        m = re.search(r'href="(/[^"]+/([A-Za-z0-9]{8})/)" class="in-match"[^>]*>(.*?)</a>',
                      row, flags=re.S)
        if not m:
            continue
        names = [re.sub(r"<[^>]+>", "", x).strip()
                 for x in re.findall(r"<span>(.*?)</span>", m.group(3), flags=re.S)]
        if len(names) < 2:
            continue
        dt = re.search(r"table-main__datetime[^>]*>\s*(\d{1,2})\.(\d{1,2})\.", row)
        day = (int(dt.group(1)), int(dt.group(2))) if dt else last_day
        last_day = day
        home, away = team_key(league, names[0]), team_key(league, names[1])
        if home and away and day:
            out.append(Fixture(home, away, day, m.group(1), m.group(2)))
    return out


@dataclass
class MatchOdds:
    probs: list[float]                  # home, (draw), away — margin removed, sum 1
    bookmakers: int
    logos: list[str] = field(default_factory=list)   # absolute PNG URLs, home first


def parse_odds(odds_html: str, n_outcomes: int) -> tuple[list[float], int]:
    per_book = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", odds_html, flags=re.S):
        prices = [float(x) for x in re.findall(r'data-odd="([\d.]+)"', row)]
        if len(prices) != n_outcomes or any(p <= 1.0 for p in prices):
            continue
        inv = [1 / p for p in prices]
        s = sum(inv)
        per_book.append([x / s for x in inv])
    if not per_book:
        return [], 0
    avg = [sum(b[i] for b in per_book) / len(per_book) for i in range(n_outcomes)]
    return avg, len(per_book)


def find(fixtures: list[Fixture], home: str, away: str, when: datetime) -> Fixture | None:
    for f in fixtures:
        if (f.home, f.away) != (home, away):
            continue
        for delta in (-1, 0, 1):                 # BetExplorer shows its own timezone
            d = when + timedelta(days=delta)
            if (d.day, d.month) == f.day:
                return f
    return None


def match_odds(league: str, fx: Fixture, fetch=_get) -> MatchOdds | None:
    _, bettype = LEAGUES[league]
    n = 2 if bettype == "ha" else 3
    ref = BASE + fx.url
    raw = fetch(f"{BASE}/match-odds-old/{fx.match_id}/1/{bettype}/1/en/",
                **{"X-Requested-With": "XMLHttpRequest", "Referer": ref})
    probs, books = parse_odds(json.loads(raw).get("odds", ""), n)
    if books < MIN_BOOKMAKERS:
        return None
    logos = []
    try:
        page = fetch(ref)
        logos = [BASE + src for src in re.findall(r'<img src="(/res/images/team-logo/[^"]+)"', page)[:2]]
    except Exception:
        pass
    return MatchOdds(probs, books, logos)


def fixtures_for(league: str, fetch=_get) -> list[Fixture]:
    path, _ = LEAGUES[league]
    return parse_fixtures(league, fetch(f"{BASE}/{path}/fixtures/"))


def to_percent(probs: list[float], floor: int = 3) -> list[int]:
    """Integer percents summing to 100, none below `floor`."""
    p = [max(x * 100, floor) for x in probs]
    s = sum(p)
    p = [x * 100 / s for x in p]
    out = [round(x) for x in p]
    out[out.index(max(out))] += 100 - sum(out)
    return out
