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
    "euroleague": ("basketball/europe/euroleague", "ha"),
    "rinktine": ("football/world/friendly-international", "1x2"),
}
# Every competition Lithuania's men's football team plays in; a game is found in whichever lists it.
NATIONAL_PATHS = ("football/world/friendly-international", "football/europe/uefa-nations-league",
                  "football/europe/euro-qualification", "football/world/world-cup-qualification")
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
    # English names on BetExplorer -> the Lithuanian title. An opponent missing here is reported, not guessed.
    "rinktine": {
        "lithuania": "Lietuva", "suriname": "Surinamas", "liechtenstein": "Lichtenšteinas", "latvia": "Latvija",
        "estonia": "Estija", "poland": "Lenkija", "finland": "Suomija", "sweden": "Švedija", "norway": "Norvegija",
        "denmark": "Danija", "iceland": "Islandija", "ireland": "Airija", "northern ireland": "Šiaurės Airija",
        "scotland": "Škotija", "wales": "Velsas", "england": "Anglija", "france": "Prancūzija", "germany": "Vokietija",
        "italy": "Italija", "spain": "Ispanija", "portugal": "Portugalija", "netherlands": "Nyderlandai",
        "belgium": "Belgija", "luxembourg": "Liuksemburgas", "switzerland": "Šveicarija", "austria": "Austrija",
        "czech republic": "Čekija", "czechia": "Čekija", "slovakia": "Slovakija", "hungary": "Vengrija",
        "romania": "Rumunija", "bulgaria": "Bulgarija", "greece": "Graikija", "turkey": "Turkija", "cyprus": "Kipras",
        "malta": "Malta", "croatia": "Kroatija", "serbia": "Serbija", "slovenia": "Slovėnija",
        "bosnia herzegovina": "Bosnija ir Hercegovina", "montenegro": "Juodkalnija", "north macedonia": "Šiaurės Makedonija",
        "albania": "Albanija", "kosovo": "Kosovas", "moldova": "Moldova", "ukraine": "Ukraina", "georgia": "Sakartvelas",
        "armenia": "Armėnija", "azerbaijan": "Azerbaidžanas", "kazakhstan": "Kazachstanas", "israel": "Izraelis",
        "faroe islands": "Farerų salos", "andorra": "Andora", "san marino": "San Marinas", "gibraltar": "Gibraltaras",
    },
    "euroleague": {
        "zalgiris": "Zalgiris Kaunas", "barcelona": "FC Barcelona",
        "fenerbahce": "Fenerbahce Tarfin Istanbul", "anadolu efes": "Anadolu Efes Istanbul",
        "villeurbanne": "LDLC ASVEL Villeurbanne", "asvel": "LDLC ASVEL Villeurbanne",
        "virtus bologna": "Virtus Bologna", "partizan": "Partizan Mozzart Bet Belgrade",
        "besiktas": "Besiktas Istanbul", "olimpia milano": "Armani Olimpia Milan",
        "bayern": "FC Bayern Munich", "panathinaikos": "Panathinaikos AKTOR Athens",
        "real madrid": "Real Madrid", "olympiacos": "Olympiacos Piraeus",
        "crvena zvezda": "Crvena Zvezda Meridianbet Belgrade", "dubai": "Dubai Basketball",
        "hapoel tel aviv": "Hapoel IBI Tel Aviv", "maccabi tel aviv": "Maccabi Rapyd Tel Aviv",
        "baskonia": "Kosner Baskonia Vitoria-Gasteiz", "paris": "Paris Basketball",
        "valencia": "Valencia Basket",
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


def parse_fixtures(league: str, page: str, today=None) -> list[Fixture]:
    """`today` (a date) resolves BetExplorer's relative labels: games in the
    next two days show "Today 18:10" / "Tomorrow 15:50" instead of "05.11."."""
    if today is None:
        from datetime import date as _date
        today = _date.today()
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
        rel = re.search(r"table-main__datetime[^>]*>\s*(Today|Tomorrow)", row)
        if dt:
            day = (int(dt.group(1)), int(dt.group(2)))
        elif rel:
            d = today + timedelta(days=1 if rel.group(1) == "Tomorrow" else 0)
            day = (d.day, d.month)
        else:
            day = last_day
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


def devig(prices: list[float], method: str = "power") -> list[float]:
    """Fair probabilities from one bookmaker's decimal prices.

    "proportional" divides 1/odds by their sum — it spreads the margin evenly,
    which leaves longshots too expensive (bookmakers load more margin on them:
    the favourite–longshot bias). "power" finds k with Σ(1/oᵢ)ᵏ = 1 and uses
    (1/oᵢ)ᵏ: the margin comes mostly off the longshot, so a 1.30 favourite keeps
    ~75 % instead of ~72 %. Both return probabilities summing to 1."""
    inv = [1 / p for p in prices]
    if method == "favourite":
        # Owner, 2026-11-07: the favourite is priced exactly as the bookmaker prices it
        # (1/odds, margin left in), and the others share what is left in proportion to
        # their own prices. The underdogs come out a little longer than the bookmaker's.
        fav = min(range(len(prices)), key=lambda i: prices[i])
        top = min(inv[fav], 0.97)
        rest = sum(x for i, x in enumerate(inv) if i != fav)
        return [top if i == fav else (1 - top) * x / rest for i, x in enumerate(inv)]
    if method != "power":
        s = sum(inv)
        return [x / s for x in inv]
    lo, hi = 0.2, 5.0                       # Σ inv^k falls as k grows (inv < 1)
    for _ in range(80):
        k = (lo + hi) / 2
        if sum(x ** k for x in inv) > 1:
            lo = k
        else:
            hi = k
    out = [x ** ((lo + hi) / 2) for x in inv]
    s = sum(out)
    return [x / s for x in out]


def parse_odds(odds_html: str, n_outcomes: int, method: str | None = None) -> tuple[list[float], int]:
    if method is None:
        from . import config
        method = config.GAMES_DEVIG
    per_book = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", odds_html, flags=re.S):
        prices = [float(x) for x in re.findall(r'data-odd="([\d.]+)"', row)]
        if len(prices) != n_outcomes or any(p <= 1.0 for p in prices):
            continue
        per_book.append(devig(prices, method))
    if not per_book:
        return [], 0
    # median per outcome (robust to one stale/outlier bookmaker), renormalised
    med = []
    for i in range(n_outcomes):
        xs = sorted(b[i] for b in per_book)
        mid = len(xs) // 2
        med.append(xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2)
    if method == "favourite":
        # The favourite at the strongest price any bookmaker gives it (owner, 2026-11-07: "the
        # favourite should be the exact bookmaker odds"; the Lithuanian books he compares with,
        # not on BetExplorer, price favourites shorter than the international median), and the
        # others share the rest in proportion to their median.
        fav = max(range(n_outcomes), key=lambda i: med[i])
        top = max(b[fav] for b in per_book)
        rest = sum(x for i, x in enumerate(med) if i != fav) or 1
        return [top if i == fav else (1 - top) * x / rest for i, x in enumerate(med)], len(per_book)
    s = sum(med)
    return [x / s for x in med], len(per_book)


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
    if league == "rinktine":
        out = []
        for path in NATIONAL_PATHS:
            try:
                out += [f for f in parse_fixtures(league, fetch(f"{BASE}/{path}/fixtures/")) if "Lietuva" in (f.home, f.away)]
            except Exception:           # a competition with no page this season
                continue
        return out
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
