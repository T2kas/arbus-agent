"""Stage 1 — deterministic RSS harvest of Lithuanian news headlines."""

from __future__ import annotations

import calendar
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import feedparser
import requests

from . import config

log = logging.getLogger(__name__)

UA = "Mozilla/5.0 (compatible; ArbusMarketAgent/1.0)"


def _fetch(url: str) -> bytes:
    # Fetch ourselves instead of letting feedparser do it: some LT outlets
    # (Delfi) redirect their RSS through hosts feedparser's urllib chain
    # mishandles; requests follows the chain cleanly.
    resp = requests.get(url, headers={"User-Agent": UA}, timeout=25, allow_redirects=True)
    resp.raise_for_status()
    return resp.content


def harvest(days: int = config.HARVEST_DAYS, cap: int = config.HARVEST_MAX_HEADLINES) -> list[dict]:
    """Fetch recent headlines from all configured feeds.

    Returns a list of {source, title, link, published} dicts, newest first.
    Individual feed failures are logged and skipped.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items: list[dict] = []
    seen_titles: set[str] = set()

    for feed in config.FEEDS:
        try:
            parsed = feedparser.parse(_fetch(feed["url"]))
            if parsed.bozo and not parsed.entries:
                log.warning("feed %s unreadable: %s", feed["name"], parsed.bozo_exception)
                continue
            for entry in parsed.entries:
                ts = entry.get("published_parsed") or entry.get("updated_parsed")
                published = (
                    datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc) if ts else None
                )
                if published and published < cutoff:
                    continue
                title = (entry.get("title") or "").strip()
                if not title:
                    continue
                key = title.lower()
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                items.append(
                    {
                        "source": feed["name"],
                        "title": title,
                        "link": entry.get("link", ""),
                        "published": published.isoformat() if published else "",
                    }
                )
        except Exception as exc:  # network errors, malformed XML, etc.
            log.warning("feed %s failed: %s", feed["name"], exc)

    _annotate_coverage(items)
    return _balance_by_day(items, cap)


def probe_feeds(days: int = config.HARVEST_DAYS) -> list[tuple[str, str, int, str]]:
    """Check every configured feed one by one: (name, url, fresh items, error).

    Feeds die quietly — an outlet changes its RSS path and the batch simply
    gets thinner, with the reason buried in a log line. This makes the state of
    every source visible in one command.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[tuple[str, str, int, str]] = []
    for feed in config.FEEDS:
        try:
            parsed = feedparser.parse(_fetch(feed["url"]))
            fresh = 0
            for entry in parsed.entries:
                ts = entry.get("published_parsed") or entry.get("updated_parsed")
                published = (datetime.fromtimestamp(calendar.timegm(ts), tz=timezone.utc)
                             if ts else None)
                if published is None or published >= cutoff:
                    fresh += 1
            error = "" if parsed.entries else str(
                parsed.get("bozo_exception", "no entries — wrong URL?"))
            out.append((feed["name"], feed["url"], fresh, error))
        except Exception as exc:
            out.append((feed["name"], feed["url"], 0, str(exc)))
    return out


def _annotate_coverage(items: list[dict]) -> None:
    """Set item["coverage"] = how many distinct outlets ran a similar story.

    Exact-duplicate titles are already dropped at harvest, but the same event
    phrased differently by LRT, Delfi and 15min survives — and that overlap is
    the only engagement signal RSS carries: a story every outlet picked up beat
    editorial filters three times. Fuzzy-match titles across sources and count
    distinct outlets per cluster.
    """
    from rapidfuzz import fuzz

    norms = [re.sub(r"\W+", " ", it["title"].lower()).strip() for it in items]
    for i, item in enumerate(items):
        outlets = {item["source"]}
        for j, other in enumerate(items):
            if i != j and other["source"] not in outlets and \
                    fuzz.token_set_ratio(norms[i], norms[j]) >= 80:
                outlets.add(other["source"])
        item["coverage"] = len(outlets)


def _balance_by_day(items: list[dict], cap: int) -> list[dict]:
    """Sample headlines evenly across the look-back window's days.

    A plain newest-first sort under a cap lets the last 24 hours crowd out the
    rest of the week, and the drafter then builds markets from whatever ran
    yesterday instead of the week's actually-significant stories. Round-robin
    across days keeps every day of the window represented; within a day,
    stories covered by more outlets come first (the engagement signal), newer
    breaking ties. Undated items join at the end.
    """
    by_day: dict[str, list[dict]] = {}
    undated: list[dict] = []
    for item in items:
        day = item["published"][:10]
        (by_day.setdefault(day, []) if day else undated).append(item)
    for bucket in by_day.values():
        bucket.sort(key=lambda i: i["published"], reverse=True)   # newest first,
        bucket.sort(key=lambda i: i.get("coverage", 1), reverse=True)  # coverage wins

    days = sorted(by_day, reverse=True)
    picked: list[dict] = []
    round_idx = 0
    while len(picked) < cap:
        took_any = False
        for day in days:
            bucket = by_day[day]
            if round_idx < len(bucket):
                picked.append(bucket[round_idx])
                took_any = True
                if len(picked) >= cap:
                    break
        if not took_any:
            break
        round_idx += 1
    for item in undated:
        if len(picked) >= cap:
            break
        picked.append(item)
    return picked


def headlines_block(items: list[dict]) -> str:
    """Render harvested items as a compact block for the LLM prompt.

    Stories several outlets ran get a "×N portalai" marker — ready-made
    attention-gate evidence the drafter is told to prioritize.
    """
    lines = []
    for i in items:
        cov = i.get("coverage", 1)
        marker = f" [×{cov} portalai]" if cov > 1 else ""
        lines.append(f"- [{i['source']}] {i['title']}{marker} "
                     f"({i['published'][:10]}) {i['link']}")
    return "\n".join(lines)
