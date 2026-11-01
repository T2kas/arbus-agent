"""Market audit: free checks and the AI review's parsing/flagging."""

import json
from datetime import datetime, timezone

from arbus import audit

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def _m(title, status="open", closes=None, structure="single_outcome", opts=(), created="2026-10-25T21:00:00+00:00", cat="kita"):
    return {"id": title, "title": title, "status": status, "closes_at": closes, "category": cat,
            "outcome_structure": structure, "created_at": created, "rules": "r",
            "market_options": [{"id": f"{title}-{i}", "label": l, "probability": p, "sort_order": i,
                                "resolved_outcome": r} for i, (l, p, r) in enumerate(opts)]}


def test_free_findings_flags_overdue_and_passed_ladder_dates():
    rows = [
        _m("EV rugsėjį", "closed", "2026-09-30T20:59:00+00:00", opts=[("Tesla", 48, None), ("VW", 52, None)]),
        _m("Iki...", structure="cumulative_date",
           opts=[("2026 m. spalio 20 d.", 20, None), ("2026 m. gruodžio 31 d.", 40, None)]),
        _m("Done", "resolved", "2026-09-01T00:00:00+00:00"),
        _m("Aukščiausia temperatūra Vilniuje lapkričio 1 d.?", "closed", "2026-11-01T09:00:00+00:00", cat="orai"),
    ]
    f = audit.free_findings(rows, NOW)
    titles = [(x["title"], x["level"]) for x in f]
    assert ("EV rugsėjį", "red") in titles
    assert any(t == "Iki..." and "spalio 20" in x["what"] for x, (t, _) in zip(f, titles))
    assert all(t not in ("Done", "Aukščiausia temperatūra Vilniuje lapkričio 1 d.?") for t, _ in titles)


def test_aurora_decided_from_gfz_kp():
    class R:
        def json(self):
            return {"datetime": ["2026-10-31T15:00:00Z", "2026-10-31T18:00:00Z"], "Kp": [4.333, 5.667]}
    rows = [_m("Lietuvoje bus matoma šiaurės pašvaistė iki...?", structure="cumulative_date",
               opts=[("2026 m. lapkričio 15 d.", 40, None)])]
    f = audit.aurora_findings(rows, NOW, fetch=lambda url, timeout: R())
    assert f and f[0]["level"] == "red" and "5.67" in f[0]["what"]


def test_ai_review_flags_decided_and_stale_prices():
    rows = [_m("Ignitis 23", opts=[("Taip", 64, None), ("Ne", 36, None)]),
            _m("Stable", opts=[("Taip", 50, None), ("Ne", 50, None)]),
            _m("Done?", opts=[("Taip", 30, None), ("Ne", 70, None)])]
    answer = json.dumps([
        {"id": "Ignitis 23", "decided": False, "fair": {"Taip": 25, "Ne": 75}, "note": "kaina 21,55 €"},
        {"id": "Stable", "decided": False, "fair": {"Taip": 55, "Ne": 45}},
        {"id": "Done?", "decided": True, "outcome": "Taip", "source": "https://lrt.lt/x"},
    ])
    found, _cost = audit.ai_findings(rows, NOW, research=lambda p: "ok:\n" + answer)
    by = {f["title"]: f for f in found}
    assert by["Ignitis 23"]["level"] == "orange" and "64% → ~25%" in by["Ignitis 23"]["what"]
    assert "Stable" not in by
    assert by["Done?"]["level"] == "red"


def test_bot_managed_markets_are_skipped_by_ai():
    assert audit.is_bot_managed(_m("Vilniaus „Rytas“ vs Kauno „Žalgiris“"))
    assert audit.is_bot_managed(_m("x", cat="orai"))
    assert not audit.is_bot_managed(_m("Ar Ignitis akcija pasieks 23€ šiemet?"))
