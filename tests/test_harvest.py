"""Offline tests for the per-day balanced headline sampler."""

import sys
import types

# feedparser wheels don't build everywhere; harvest imports it at module level
# but _balance_by_day never touches it, so a stub keeps these tests offline.
sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))

from arbus.harvest import _balance_by_day  # noqa: E402


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


def test_cap_respected_and_undated_last():
    items = [_item("2026-07-24", 1), {"source": "S", "title": "no date",
                                      "link": "", "published": ""}]
    picked = _balance_by_day(items, cap=2)
    assert [i["title"] for i in picked] == ["2026-07-24T01", "no date"]
    assert len(_balance_by_day(items, cap=1)) == 1
