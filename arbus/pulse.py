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
import time
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


# Reddit 403s generic user agents and increasingly gates www.reddit.com JSON.
# A descriptive UA in Reddit's documented "platform:app:version (by /u/user)"
# form plus the old.reddit.com mirror gets the public listing back.
REDDIT_UA = "windows:arbus-market-agent:1.0 (by /u/arbus_bot)"
REDDIT_HOSTS = ["https://old.reddit.com", "https://www.reddit.com"]


def _parse_reddit_rss(xml_bytes: bytes, sub: str, cap: int) -> list[Signal]:
    """Parse Reddit's Atom feed — the fallback when JSON listings are blocked.

    RSS carries no score or comment count, so these signals say what is being
    discussed without a magnitude. Still worth having: the draft stage treats
    them as topics to investigate, not as proof of attention.
    """
    root = ET.fromstring(xml_bytes)
    signals: list[Signal] = []
    for entry in root.iter():
        if _localname(entry.tag) != "entry":
            continue
        title = ""
        link = ""
        for child in entry:
            name = _localname(child.tag)
            if name == "title" and (child.text or "").strip():
                title = child.text.strip()
            elif name == "link":
                link = child.attrib.get("href", "") or link
        if not title:
            continue
        signals.append(Signal(f"Reddit r/{sub}", title,
                              f"aktyvi diskusija (r/{sub})", "discussion", link))
        if len(signals) >= cap:
            break
    return signals


def _reddit(cap: int) -> list[Signal]:
    out: list[Signal] = []
    per_sub = max(1, cap // max(1, len(config.REDDIT_SUBS)))
    headers = {"User-Agent": REDDIT_UA, "Accept": "application/json"}
    for idx, sub in enumerate(config.REDDIT_SUBS):
        if idx:
            time.sleep(6)  # Reddit 429s closely spaced requests from one IP
        got: list[Signal] = []
        last: Exception | None = None
        # Preferred: JSON listings, which carry upvotes and comment counts.
        for host in REDDIT_HOSTS:
            try:
                url = f"{host}/r/{sub}/hot.json?limit={per_sub * 2}&raw_json=1"
                resp = requests.get(url, headers=headers, timeout=25, allow_redirects=True)
                resp.raise_for_status()
                got = _parse_reddit(resp.json(), sub, per_sub)
                last = None
                break
            except Exception as exc:  # try the next mirror before giving up
                last = exc
        # Fallback: the Atom feed, which is served where JSON is 403-blocked.
        if not got:
            for host in REDDIT_HOSTS:
                try:
                    resp = requests.get(f"{host}/r/{sub}/hot.rss?limit={per_sub * 2}",
                                        headers={"User-Agent": REDDIT_UA},
                                        timeout=25, allow_redirects=True)
                    resp.raise_for_status()
                    got = _parse_reddit_rss(resp.content, sub, per_sub)
                    if got:
                        log.info("reddit r/%s: JSON blocked, using RSS (no vote counts)", sub)
                        last = None
                        break
                except Exception as exc:
                    last = exc
        out.extend(got)
        if not got and last is not None:
            log.warning("reddit r/%s failed (JSON and RSS): %s", sub, last)
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


def _wiki_urls(today: date, days_back: int = 4) -> list[str]:
    """Candidate URLs, newest first.

    Wikimedia compiles the daily top list some hours after UTC midnight, so
    yesterday can still 404 depending on the time of day and timezone. Walk
    back a few days instead of giving up on the first miss.
    """
    urls = []
    for back in range(1, days_back + 1):
        d = today - timedelta(days=back)
        urls.append("https://wikimedia.org/api/rest_v1/metrics/pageviews/top/"
                    f"lt.wikipedia/all-access/{d.year}/{d.month:02d}/{d.day:02d}")
    return urls


def _wikipedia(cap: int) -> list[Signal]:
    last: Exception | None = None
    for url in _wiki_urls(date.today()):
        try:
            return _parse_wikipedia(_get_json(url), cap)
        except Exception as exc:  # 404 until the day's data is compiled
            last = exc
    raise last if last else RuntimeError("wikipedia: no data")


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


# ── TikTok Creative Center — trending hashtags & sounds in LT ────────────────
# Best-effort: TikTok's Creative Center is a public (advertiser-facing) trends
# surface, but it fights automated access and does not guarantee every country.
# If it returns nothing, the source contributes nothing — no batch is harmed.
# Confirm it works for you with `python -m arbus generate --dry-run`.

_TIKTOK_BASE = "https://ads.tiktok.com/creative_radar_api/v1/popular_trend"
_TIKTOK_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json",
    "Referer": "https://ads.tiktok.com/business/creativecenter/inspiration/popular/hashtag/pc/en",
}


def _parse_tiktok_hashtags(payload: dict, cap: int) -> list[Signal]:
    lst = payload.get("data", {}).get("list", []) or []
    signals: list[Signal] = []
    for row in lst:
        name = (row.get("hashtag_name") or row.get("hashtag") or "").strip()
        if not name:
            continue
        rank = row.get("rank") or row.get("rank_index") or len(signals) + 1
        views = int(row.get("video_views", 0) or row.get("views", 0) or 0)
        metric = (f"{views:,} vaizdo įrašų peržiūrų (TikTok #{rank})".replace(",", " ")
                  if views else f"TikTok trending #{rank}")
        signals.append(Signal("TikTok Trends LT", f"#{name}", metric, "tiktok",
                              "https://www.tiktok.com/tag/" + name))
        if len(signals) >= cap:
            break
    return signals


def _parse_tiktok_sounds(payload: dict, cap: int) -> list[Signal]:
    lst = (payload.get("data", {}).get("sound_list")
           or payload.get("data", {}).get("list", []) or [])
    signals: list[Signal] = []
    for row in lst:
        title = (row.get("title") or row.get("song_title") or "").strip()
        if not title:
            continue
        author = (row.get("author") or row.get("singer") or "").strip()
        rank = row.get("rank") or len(signals) + 1
        label = f"{title} — {author}" if author else title
        signals.append(Signal("TikTok Sounds LT", label,
                              f"TikTok trending daina (#{rank})", "tiktok",
                              row.get("link", "")))
        if len(signals) >= cap:
            break
    return signals


def _tiktok_urls(kind: str, country: str, period: int, limit: int) -> list[str]:
    """Known Creative Center endpoint shapes for a trend list, in order.

    TikTok reshuffles these paths without notice, so try the documented radar
    path and the older popular_trend path before giving up.
    """
    q = f"period={period}&page=1&limit={limit}&country_code={country}&sort_by=popular"
    return [
        f"{_TIKTOK_BASE}/{kind}/list?{q}",
        f"https://ads.tiktok.com/creative_radar_api/v1/{kind}/list?{q}",
        f"https://ads.tiktok.com/creative_radar_api/v1/popular_trend/{kind}?{q}",
    ]


def _tiktok(cap: int) -> list[Signal]:
    country = config.TIKTOK_COUNTRY
    period = config.TIKTOK_PERIOD
    out: list[Signal] = []
    half = max(1, cap // 2)
    for kind, parser in (("hashtag", _parse_tiktok_hashtags), ("sound", _parse_tiktok_sounds)):
        for url in _tiktok_urls(kind, country, period, half):
            try:
                resp = requests.get(url, headers=_TIKTOK_HEADERS, timeout=25)
                resp.raise_for_status()
                got = parser(resp.json(), half)
                if got:
                    out.extend(got)
                    break
            except Exception as exc:  # blocked / country unsupported / path moved
                log.debug("tiktok %s endpoint failed (%s): %s", kind, url, exc)
    if not out:
        log.info("tiktok: no signals (Creative Center blocks automated access or "
                 "does not cover %s) — optional source, batch continues", country)
    return out


# ── Apple charts — what Lithuania listens to and installs ───────────────────
# Apple's marketing RSS is public, key-free and genuinely country-scoped, which
# makes it the most reliable chart signal available for Lithuania: Spotify's
# chart pages now require a login, and Instagram/Threads expose no trend feed at
# all. App rankings double as a culture signal — which apps a country is
# installing this week is a real trend, not a proxy for one.

_APPLE_BASE = "https://rss.applemarketingtools.com/api/v2"


def _parse_apple(payload: dict, source: str, kind: str, noun: str, cap: int) -> list[Signal]:
    results = payload.get("feed", {}).get("results", []) or []
    signals: list[Signal] = []
    for rank, row in enumerate(results, 1):
        name = (row.get("name") or "").strip()
        if not name:
            continue
        artist = (row.get("artistName") or "").strip()
        label = f"{name} — {artist}" if artist else name
        signals.append(Signal(source, label, f"{noun} #{rank} Lietuvoje", kind,
                              row.get("url", "")))
        if len(signals) >= cap:
            break
    return signals


def _apple_music(cap: int) -> list[Signal]:
    url = (f"{_APPLE_BASE}/{config.APPLE_STOREFRONT}/music/most-played/"
           f"{max(cap, 10)}/songs.json")
    return _parse_apple(_get_json(url), "Apple Music LT", "chart", "populiariausia daina", cap)


# ── Nasdaq Vilnius — Lithuanian listed companies, weekly moves ──────────────
# Official Nasdaq Baltic pages export only Excel/HTML, so quotes come from
# Yahoo Finance's public chart JSON (no key, ".VS" suffix = Vilnius). Markets
# seeded from these must still resolve against the official Nasdaq Baltic
# closing price — the pulse only says which stock moved enough to argue about.

def _parse_yahoo_chart(payload: dict, name: str, ticker: str) -> Signal | None:
    """One signal per ticker: last price + week change, or None if unusable."""
    try:
        result = payload["chart"]["result"][0]
    except (KeyError, IndexError, TypeError):
        return None
    meta = result.get("meta", {})
    closes = [c for c in (result.get("indicators", {}).get("quote", [{}])[0]
                          .get("close") or []) if c is not None]
    price = meta.get("regularMarketPrice") or (closes[-1] if closes else None)
    if price is None:
        return None
    currency = "€" if meta.get("currency", "EUR") == "EUR" else meta.get("currency", "")
    price_txt = f"{price:.2f}".replace(".", ",") + f" {currency}".rstrip()
    metric = price_txt
    if closes and closes[0]:
        change = (price / closes[0] - 1) * 100
        metric += f" · savaitė {change:+.1f} %".replace(".", ",")
    return Signal("Nasdaq Vilnius", name, metric, "stock",
                  f"https://finance.yahoo.com/quote/{ticker}")


def _nasdaq_vilnius(cap: int) -> list[Signal]:
    signals: list[Signal] = []
    for idx, (ticker, name) in enumerate(config.NASDAQ_VILNIUS_TICKERS[:cap]):
        if idx:
            time.sleep(0.4)  # stay well under Yahoo's per-IP rate limit
        sig = None
        for host in ("query1", "query2"):
            try:
                url = (f"https://{host}.finance.yahoo.com/v8/finance/chart/"
                       f"{ticker}?range=5d&interval=1d")
                sig = _parse_yahoo_chart(_get_json(url), name, ticker)
                if sig:
                    break
            except Exception as exc:  # one dead ticker must not sink the rest
                log.debug("nasdaq %s via %s failed: %s", ticker, host, exc)
        if sig:
            signals.append(sig)
    return signals


# ── Registry + public API ────────────────────────────────────────────────────

# (label, fetcher, cap). The cap keeps entertainment sources from flooding the
# prompt: charts and trending videos are seasoning, while search/discussion/
# pageview signals carry the news-and-society topics the platform leads with.
# App Store rankings were dropped entirely — which apps are being installed
# almost never seeds a market (team review, 2026-07-25). A cap of None means
# "use the default".
SOURCES: list[tuple[str, "callable", int | None]] = [
    ("Google Trends LT", _google_trends, None),
    ("Reddit", _reddit, None),
    ("Wikipedia LT", _wikipedia, None),
    ("TikTok Creative Center", _tiktok, None),
    ("YouTube Trending LT", _youtube, config.PULSE_ENTERTAINMENT_CAP),
    ("Apple Music LT", _apple_music, config.PULSE_ENTERTAINMENT_CAP),
    ("Nasdaq Vilnius", _nasdaq_vilnius, None),
]


def pulse(cap_per_source: int = config.PULSE_MAX_PER_SOURCE) -> list[Signal]:
    """Harvest every source. Best-effort: failures are logged and skipped.

    Unlike news harvest, an empty pulse is not fatal — the pipeline still runs
    on headlines alone. It just loses the social/culture edge for that batch.
    """
    if not config.PULSE_ENABLED:
        return []
    signals: list[Signal] = []
    for label, fetch, cap in SOURCES:
        try:
            got = fetch(cap or cap_per_source)
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
