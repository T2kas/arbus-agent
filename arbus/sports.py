"""Sports match resolvers — a finished game's score from an official source.

Two leagues so far, both fetched with a single free GET, parser split from
fetcher so the logic is testable offline:

  • Euroleague basketball — the official incrowd JSON feed (status "result" =
    final, with home/away names + scores).
  • TOPLYGA football — toplyga.lt match pages (/rungtynes/<date>-<home>-<away>/
    <id>) carry a clean "X : Y" score; the fixture list links them.

Each `*_result` returns a finished game as {home, away, home_score, away_score,
url} or None (not applicable / not finished / not found). Mapping a team to a
market option, and the never-guess safety, live in weather._resolve_sports.
"""

from __future__ import annotations

import html as _html
import logging
import re
import unicodedata
from datetime import date

import requests

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; ArbusMarketAgent/1.0; +https://arbus.lt)"
_LT_MONTHS = {"sausio": 1, "vasario": 2, "kovo": 3, "balandžio": 4, "gegužės": 5,
              "birželio": 6, "liepos": 7, "rugpjūčio": 8, "rugsėjo": 9,
              "spalio": 10, "lapkričio": 11, "gruodžio": 12}


def _cnorm(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c))
    return s.lower()


def _rules_date(text: str) -> str:
    """First explicit date in the text, ISO ('2026-09-16') or LT ('rugsėjo 16 d.')."""
    m = re.search(r"(20\d{2})[-.](\d{1,2})[-.](\d{1,2})", text)
    if m:
        y, mo, d = m.groups()
    else:
        low = text.lower()
        y = (re.search(r"20\d{2}", low) or [None])[0] if re.search(r"20\d{2}", low) else None
        mm = re.search(r"(" + "|".join(_LT_MONTHS) + r")\s+(\d{1,2})", low)
        if not (y and mm):
            return ""
        mo, d = _LT_MONTHS[mm.group(1)], mm.group(2)
    try:
        return date(int(y), int(mo), int(d)).isoformat()
    except (TypeError, ValueError):
        return ""


# ── Euroleague (JSON feed) ───────────────────────────────────────────────────

_EL_FEED = ("https://feeds.incrowdsports.com/provider/euroleague-feeds/v2/"
            "competitions/E/seasons/E{year}/games")


def euroleague_season_year(text: str) -> int | None:
    """Starting year of a "2026–2027 m." season, i.e. season code E<year>."""
    m = re.search(r"20(\d{2})\s*[–\-/]\s*20\d{2}", text) or re.search(r"20\d{2}", text)
    if not m:
        return None
    return int(re.search(r"20\d{2}", m.group()).group())


def _el_fetch(year: int) -> list[dict]:
    r = requests.get(_EL_FEED.format(year=year), headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    data = r.json()
    return data.get("data") or data.get("games") or (data if isinstance(data, list) else [])


def euroleague_games(year: int) -> list[dict]:
    """Finished games: {home, away, home_score, away_score, url, date}."""
    out = []
    for g in _el_fetch(year):
        if str(g.get("status", "")).lower() != "result":
            continue
        home, away = g.get("home") or {}, g.get("away") or {}
        try:
            hs, as_ = int(home.get("score")), int(away.get("score"))
        except (TypeError, ValueError):
            continue
        out.append({
            "home": home.get("name") or home.get("editorialName") or "",
            "away": away.get("name") or away.get("editorialName") or "",
            "home_score": hs, "away_score": as_,
            "url": "https://www.euroleaguebasketball.net/euroleague/game-center/",
            "date": (g.get("date") or "")[:10],
        })
    return out


# ── TOPLYGA football (HTML) ──────────────────────────────────────────────────

_TOPLYGA_HOME = "https://www.toplyga.lt/"
_TOPLYGA_MATCH_RE = re.compile(r'/?rungtynes/(\d{4}-\d{2}-\d{2}-[a-z0-9\-]+/\d+)', re.I)


def _toplyga_score(match_html: str) -> tuple[int, int] | None:
    """Final 'X : Y' on a match page, taken from the scoreboard near the top."""
    for m in re.finditer(r">\s*(\d{1,2})\s*[:\-]\s*(\d{1,2})\s*<", match_html):
        return int(m.group(1)), int(m.group(2))
    return None


def _toplyga_teams(match_html: str) -> tuple[str, str] | None:
    """(home, away) from the match page title 'Home - Away | TOPLYGA …'."""
    m = re.search(r"<title>(.*?)</title>", match_html, re.I | re.S)
    if not m:
        return None
    head = _html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).split("|")[0]
    parts = re.split(r"\s[-–]\s", head)
    if len(parts) < 2:
        return None
    return parts[0].strip(), parts[1].strip()


def toplyga_find_match(target_iso: str, tokens_a: set, tokens_b: set,
                       listing_html: str) -> str | None:
    """Match-page path whose slug has the date and a token from each team."""
    tgt = target_iso.replace("-", "-")
    for path in _TOPLYGA_MATCH_RE.findall(listing_html):
        slug = _cnorm(path)
        if not slug.startswith(tgt):
            continue
        if any(t in slug for t in tokens_a) and any(t in slug for t in tokens_b):
            return "rungtynes/" + path
    return None


def _get(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    r.encoding = "utf-8"
    return r.text


def toplyga_result(target_iso: str, tokens_a: set, tokens_b: set) -> dict | None:
    """Finished TOPLYGA match on `target_iso` between the two teams, or None.
    home/away follow the slug order; scores from the match page."""
    try:
        path = toplyga_find_match(target_iso, tokens_a, tokens_b, _get(_TOPLYGA_HOME))
        if not path:
            return None
        url = _TOPLYGA_HOME + path
        html = _get(url)
        score = _toplyga_score(html)
    except Exception as exc:                           # noqa: BLE001
        log.debug("toplyga %s failed: %s", target_iso, exc)
        return None
    teams = _toplyga_teams(html)
    if not score or not teams:
        return None
    return {"home": teams[0], "away": teams[1],
            "home_score": score[0], "away_score": score[1],
            "url": url, "date": target_iso}
