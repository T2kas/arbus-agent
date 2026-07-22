"""Stage 1b — the PULSE: deterministic harvest of real social / attention signal.

News RSS (stage 1) tells us what newsrooms decided to publish. It does NOT tell
us what Lithuanians are actually searching, arguing about, or looking up — the
teen-talk / hype / internet-culture layer that makes for the markets people
instantly recognize and want to pick a side on. Asking a web-search LLM to
"go into TikTok" does not work: its index is news-article-biased, so it invents
or launders culture out of press coverage.

The fix is to feed the drafter REAL, current, attention-weighted signal from
sources with public structured endpoints, harvested the same resilient way we
harvest RSS:

  - Google Trends LT   what Lithuania is SEARCHING right now (+ volume)
  - Reddit r/lietuva   what Lithuania is DISCUSSING (+ upvotes / comments)
  - Wikipedia LT top   what Lithuania is LOOKING UP (+ pageviews)
  - YouTube Trending    (optional, needs a free API key) top LT videos

Every signal carries a hard, checkable attention number — exactly what the
attention gate in prompts/system.md demands as evidence. Parsers are separated
from fetchers so the whole module is testable offline with fixtures, and any
single source failing (network, rate-limit, format change) is logged and
skipped, never fatal — the pipeline still runs on news alone.

Sources are a registry: add a keyed source (Spotify Top 50, TikTok, …) by
writing one function and appending it. Keyed sources stay inert until their
key is present, so the zero-auth path carries no risk from them.
"""

from __future__ import annotations

import json
import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, timedelta

import requests

from . import config

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; ArbusMarketAgent/1.0; +https://arbus.lt)"


@dataclass
class Signal:
    """One unit of live attention signal, ready for the draft prompt."""

    source: str   # human label, e.g. "Google Trends LT"
    title: str    # the search term / post title / article / video
    metric: str   # checkable attention number, Lithuanian, e.g. "20 000+ paieškų"
    kind: str     # search | discussion | pageview | video | chart
    url: str = ""  # a grounding link when the source gives one


# ── fetch helpers (the only part that touches the network) ───────────────────

def _get_bytes(url: str, timeout: int = 25) -> bytes:
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=timeout,
                        allow_redirects=True)
    resp.raise_for_status()
    return resp.content


def _get_json(url: str, timeout: int = 25) -> dict:
    resp = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"},
                        timeout=timeout, allow_redirects=True)
    resp.raise_for_status()
    return resp.json()


def _localname(tag: str) -> str:
    """Strip the ``{namespace}`` prefix ElementTree puts on namespaced tags."""
    return tag.rsplit("}", 1)[-1]


# ── Google Trends LT — what Lithuania is searching ───────────────────────────

def _parse_google_trends(xml_bytes: bytes, cap: int) -> list[Signal]:
    """Parse the daily trending-searches RSS.

    Each <item> is a trending search term with an ``approx_traffic`` volume and
    one or more news items (great grounding URLs). We match by local tag name so
    a namespace-URI change on Google's side does not break parsing.
    """
    root = ET.fromstring(xml_bytes)
    signals: list[Signal] = []
    for item in root.iter():
        if _localname(item.tag) != "item":
            continue
        term = ""
        traffic = ""
        news_url = ""
        for child in item:
            name = _localname(child.tag)
            text = (child.text or "").strip()
            if name == "title" and text:
                term = text
            elif name == "approx_traffic" and text:
                traffic = text
            elif name == "news_item":
                for gc in child:
                    if _localname(gc.tag) == "news_item_url" and (gc.text or "").strip():
                        news_url = news_url or gc.text.strip()
        if not term:
            continue
        metric = f"{traffic} paieškų" if traffic else "trending paieška"
        signals.append(Signal("Google Trends LT", term, metric, "search", news_url))
        if len(signals) >= cap:
            break
    return signals


def _google_trends(cap: int) -> list[Signal]:
    url = f"https://trends.google.com/trending/rss?geo={config.GOOGLE_TRENDS_GEO}"
    return _parse_google_trends(_get_bytes(url), cap)


# ── Reddit — what Lithuania is discussing ────────────────────────────────────

def _parse_reddit(payload: dict, sub: str, cap: int) -> list[Signal]:
    """Pull post title + upvotes + comment count from a listing JSON.

    Skips stickied/pinned mod posts (announcements, not organic trends).
    """
    signals: list[Signal] = []
    for child in payload.get("data", {}).get("children", []):
        d = child.get("data", {})
        if d.get("stickied") or d.get("pinned"):
            continue
        title = (d.get("title") or "").strip()
        if not title:
            continue
        score = int(d.get("score", 0) or 0)
        comments = int(d.get("num_comments", 0) or 0)
        permalink = d.get("permalink") or ""
        url = f"https://www.reddit.com{permalink}" if permalink else ""
        metric = f"{score} balsų, {comments} komentarų (r/{sub})"
        signals.append(Signal(f"Reddit r/{sub}", title, metric, "discussion", url))
        if len(signals) >= cap:
            break
    return signals


def _reddit(cap: int) -> list[Signal]:
    out: list[Signal] = []
    per_sub = max(1, cap // max(1, len(config.REDDIT_SUBS)))
    for sub in config.REDDIT_SUBS:
        try:
            url = f"https://www.reddit.com/r/{sub}/hot.json?limit={per_sub * 2}"
            out.extend(_parse_reddit(_get_json(url), sub, per_sub))
        except Exception as exc:  # one dead sub must not sink the rest
            log.warning("reddit r/%s failed: %s", sub, exc)
    return out


# ── Wikipedia LT — what Lithuania is looking up ──────────────────────────────

# Namespaces / pages that are noise, not culture.
_WIKI_SKIP_PREFIXES = ("Specialus:", "Vikipedija:", "Aptarimas:", "Šablonas:",
                       "Kategorija:", "Pagalba:", "Vartotojas:", "Wikipedia:")
_WIKI_SKIP_EXACT = {"Pagrindinis_puslapis", "-", "Main_Page"}


def _parse_wikipedia(payload: dict, cap: int) -> list[Signal]:
    items = payload.get("items", [])
    if not items:
        return []
    day = items[0]
    day_label = f"{day.get('year','')}-{day.get('month','')}-{day.get('day','')}"
    signals: list[Signal] = []
    for art in day.get("articles", []):
        name = art.get("article", "")
        if name in _WIKI_SKIP_EXACT or name.startswith(_WIKI_SKIP_PREFIXES):
            continue
        title = name.replace("_", " ").strip()
        if not title:
            continue
        views = int(art.get("views", 0) or 0)
        metric = f"{views:,} peržiūrų ({day_label})".replace(",", " ")
        url = f"https://lt.wikipedia.org/wiki/{name}"
        signals.append(Signal("Vikipedija LT", title, metric, "pageview", url))
        if len(signals) >= cap:
            break
    return signals


def _wikipedia(cap: int) -> list[Signal]:
    # Yesterday: today's top list is usually not compiled yet.
    d = date.today() - timedelta(days=1)
    url = ("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
           f"lt.wikipedia/all-access/{d.year}/{d.month:02d}/{d.day:02d}")
    return _parse_wikipedia(_get_json(url), cap)


# ── YouTube Trending LT — optional, activates only with a free API key ───────

def _parse_youtube(payload: dict, cap: int) -> list[Signal]:
    signals: list[Signal] = []
    for it in payload.get("items", []):
        snip = it.get("snippet", {})
        stats = it.get("statistics", {})
        title = (snip.get("title") or "").strip()
        if not title:
            continue
        channel = (snip.get("channelTitle") or "").strip()
        views = int(stats.get("viewCount", 0) or 0)
        vid = it.get("id", "")
        metric = f"{views:,} peržiūrų".replace(",", " ") + (f", {channel}" if channel else "")
        url = f"https://www.youtube.com/watch?v={vid}" if vid else ""
        signals.append(Signal("YouTube Trending LT", title, metric, "video", url))
        if len(signals) >= cap:
            break
    return signals


def _youtube(cap: int) -> list[Signal]:
    key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if not key:
        return []  # inert without a key — zero-auth path is unaffected
    url = ("https://www.googleapis.com/youtube/v3/videos"
           "?part=snippet,statistics&chart=mostPopular"
           f"&regionCode={config.GOOGLE_TRENDS_GEO}&maxResults={min(cap, 50)}&key={key}")
    return _parse_youtube(_get_json(url), cap)


# ── Registry + public API ────────────────────────────────────────────────────

# (label, fetcher). Add a source by appending one line. Keyed sources return []
# until their key is set, so they are safe to leave enabled.
SOURCES: list[tuple[str, "callable"]] = [
    ("Google Trends LT", _google_trends),
    ("Reddit", _reddit),
    ("Wikipedia LT", _wikipedia),
    ("YouTube Trending LT", _youtube),
]


def pulse(cap_per_source: int = config.PULSE_MAX_PER_SOURCE) -> list[Signal]:
    """Harvest every source. Best-effort: failures are logged and skipped.

    Unlike news harvest, an empty pulse is not fatal — the pipeline still runs
    on headlines alone. It just loses the social/culture edge for that batch.
    """
    if not config.PULSE_ENABLED:
        return []
    signals: list[Signal] = []
    for label, fetch in SOURCES:
        try:
            got = fetch(cap_per_source)
            signals.extend(got)
            log.info("pulse %s: %d signals", label, len(got))
        except Exception as exc:  # network, rate limit, format drift
            log.warning("pulse source %s failed: %s", label, exc)
    return signals


def pulse_block(signals: list[Signal]) -> str:
    """Render signals for the draft prompt, grouped by source with numbers."""
    if not signals:
        return ("(no live pulse signals available this run — rely on the headlines "
                "and your own web search for the culture/hype layer)")
    by_source: dict[str, list[Signal]] = {}
    for s in signals:
        by_source.setdefault(s.source, []).append(s)
    lines: list[str] = []
    for source, group in by_source.items():
        lines.append(f"[{source}]")
        for s in group:
            link = f" {s.url}" if s.url else ""
            lines.append(f"- {s.title} — {s.metric}{link}")
        lines.append("")
    return "\n".join(lines).strip()
