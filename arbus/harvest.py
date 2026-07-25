"""Stage 1 — deterministic RSS harvest of Lithuanian news headlines."""

from __future__ import annotations

import calendar
import logging
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

    return _balance_by_day(items, cap)


def _balance_by_day(items: list[dict], cap: int) -> list[dict]:
    """Sample headlines evenly across the look-back window's days.

    A plain newest-first sort under a cap lets the last 24 hours crowd out the
    rest of the week, and the drafter then builds markets from whatever ran
    yesterday instead of the week's actually-significant stories. Round-robin
    across days keeps every day of the window represented; within a day newer
    items still come first. Undated items join at the end.
    """
    by_day: dict[str, list[dict]] = {}
    undated: list[dict] = []
    for item in items:
        day = item["published"][:10]
        (by_day.setdefault(day, []) if day else undated).append(item)
    for bucket in by_day.values():
        bucket.sort(key=lambda i: i["published"], reverse=True)

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
    """Render harvested items as a compact block for the LLM prompt."""
    lines = [f"- [{i['source']}] {i['title']} ({i['published'][:10]}) {i['link']}" for i in items]
    return "\n".join(lines)
