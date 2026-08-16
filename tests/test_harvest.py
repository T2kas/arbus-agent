"""Offline tests for the per-day balanced headline sampler."""

import sys
import types

# feedparser wheels don't build everywhere; harvest imports it at module level
# but _balance_by_day never touches it, so a stub keeps these tests offline.
sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))

from arbus import config, harvest  # noqa: E402
from arbus.harvest import _annotate_coverage, _balance_by_day, headlines_block  # noqa: E402


def _item(day: str, hour: int) -> dict:
    return {"source": "S", "title": f"{day}T{hour:02d}", "link": "",
            "published": f"{day}T{hour:02d}:00:00+00:00"}


def test_every_day_represented_under_cap():
    # 30 items yesterday, 2 per older day — newest-first would take only
    # yesterday; balanced sampling must include the whole week.
    items = [_item("2026-07-24", h) for h in range(30)]
    for day in ("2026-07-23", "2026-07-22", "2026-07-21"):
        items += [_item(day, h) for h in range(2)]
    picked = _balance_by_day(items, cap=12)
    days = {i["published"][:10] for i in picked}
    assert days == {"2026-07-24", "2026-07-23", "2026-07-22", "2026-07-21"}


def test_within_day_newest_first():
    items = [_item("2026-07-24", h) for h in (3, 9, 6)]
    picked = _balance_by_day(items, cap=3)
    assert [i["title"] for i in picked] == ["2026-07-24T09", "2026-07-24T06",
                                           "2026-07-24T03"]


def test_coverage_counts_distinct_outlets_for_similar_titles():
    items = [
        {"source": "LRT", "title": "Vilniuje atidarytas naujas stadionas",
         "link": "", "published": "2026-07-24T10:00:00+00:00"},
        {"source": "Delfi", "title": "Naujas stadionas atidarytas Vilniuje",
         "link": "", "published": "2026-07-24T11:00:00+00:00"},
        {"source": "15min", "title": "Kaune lijo visą dieną",
         "link": "", "published": "2026-07-24T09:00:00+00:00"},
    ]
    _annotate_coverage(items)
    assert items[0]["coverage"] == 2 and items[1]["coverage"] == 2
    assert items[2]["coverage"] == 1


def test_multi_outlet_stories_rank_first_within_day():
    solo = _item("2026-07-24", 23)                       # newest but one outlet
    big = dict(_item("2026-07-24", 8), coverage=3)       # older, three outlets
    picked = _balance_by_day([solo, big], cap=2)
    assert picked[0] is big


def test_headlines_block_marks_multi_outlet_coverage():
    items = [dict(_item("2026-07-24", 10), coverage=3),
             _item("2026-07-24", 11)]
    block = headlines_block(items)
    assert "[×3 portalai]" in block
    assert block.count("portalai") == 1                  # solo item unmarked


def test_cap_respected_and_undated_last():
    items = [_item("2026-07-24", 1), {"source": "S", "title": "no date",
                                      "link": "", "published": ""}]
    picked = _balance_by_day(items, cap=2)
    assert [i["title"] for i in picked] == ["2026-07-24T01", "no date"]
    assert len(_balance_by_day(items, cap=1)) == 1


def test_probe_feeds_reports_dead_feeds_instead_of_hiding_them(monkeypatch):
    """A feed that quietly dies just makes batches thinner; `arbus feeds` is
    how that becomes visible."""
    monkeypatch.setattr(config, "FEEDS", [
        {"name": "Alive", "url": "https://ok.lt/rss"},
        {"name": "Dead", "url": "https://gone.lt/rss"},
    ])

    def fake_fetch(url):
        if "gone" in url:
            raise RuntimeError("404 Not Found")
        return b"<rss/>"

    monkeypatch.setattr(harvest, "_fetch", fake_fetch)
    monkeypatch.setattr(harvest.feedparser, "parse",
                        lambda _b: type("P", (), {"entries": [{"title": "x"}]})(),
                        raising=False)

    rows = harvest.probe_feeds()
    assert rows[0][0] == "Alive" and rows[0][2] == 1 and rows[0][3] == ""
    assert rows[1][0] == "Dead" and rows[1][2] == 0 and "404" in rows[1][3]
