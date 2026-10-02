"""Reviewed market batches: validation, duplicate-skip, and the October 2026 file."""

import json
from datetime import datetime, timezone

from arbus import batch_create, fuel, resolvers, weather

NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)


def _spec(**kw):
    s = {"title": "Ar X?", "subtitle": "s", "category": "kita",
         "image_url": "https://img/x.jpg", "liquidity": 150000, "rules": "r",
         "context": "c", "closes_at": "2026-10-31T23:59:00+02:00",
         "options": [{"label": "Taip", "probability": 60},
                     {"label": "Ne", "probability": 40}]}
    s.update(kw)
    return s


def test_valid_spec_passes():
    assert batch_create.validate(_spec(), NOW) == []


def test_validation_catches_mistakes():
    assert batch_create.validate(_spec(options=[{"label": "A", "probability": 60},
                                                {"label": "B", "probability": 30}]), NOW)
    assert batch_create.validate(_spec(options=[{"label": "A", "probability": 100},
                                                {"label": "B", "probability": 0}]), NOW)
    assert batch_create.validate(_spec(closes_at="2026-09-30T23:59:00+03:00"), NOW)
    assert batch_create.validate(_spec(closes_at="2026-10-31T23:59:00"), NOW)  # no tz
    assert batch_create.validate(_spec(image_url="http://x"), NOW)
    assert batch_create.validate(_spec(options=[{"label": "A", "probability": 50},
                                                {"label": "A", "probability": 50}]), NOW)


def test_existing_titles_ignore_resolved():
    rows = [{"title": "Ar X?", "status": "open"}, {"title": "Ar Y?", "status": "resolved"}]
    assert batch_create.existing_titles(rows) == {"Ar X?"}


def test_dry_run_skips_existing_and_writes_nothing(monkeypatch, tmp_path):
    f = tmp_path / "b.json"
    f.write_text(json.dumps([_spec(title="Ar X?"), _spec(title="Ar Z?")]), "utf-8")
    monkeypatch.setattr(batch_create.app_api, "markets",
                        lambda n: ([{"title": "Ar X?", "status": "open"}], ""))
    monkeypatch.setattr(batch_create.app_api, "create_market",
                        lambda s: (_ for _ in ()).throw(AssertionError("no writes")))
    monkeypatch.setattr(batch_create, "validate", lambda s, now=None: [])
    reports, err = batch_create.run(str(f), dry_run=True, alert=False)
    assert err == ""
    assert [r["status"] for r in reports] == ["skipped", "would-create"]


def test_invalid_batch_creates_nothing(monkeypatch, tmp_path):
    f = tmp_path / "b.json"
    f.write_text(json.dumps([_spec(), _spec(title="Ar bad?", options=[])]), "utf-8")
    monkeypatch.setattr(batch_create.app_api, "create_market",
                        lambda s: (_ for _ in ()).throw(AssertionError("no writes")))
    reports, err = batch_create.run(str(f), dry_run=False, alert=False)
    assert reports == [] and "invalid" in err


# ── the reviewed October 2026 batch ──────────────────────────────────────────

def _october():
    return json.load(open("markets/2026-10.json", encoding="utf-8"))


def test_october_batch_is_valid():
    specs = _october()
    assert len(specs) == 7
    for s in specs:
        assert batch_create.validate(s, NOW) == [], s["title"]


def test_october_film_market_is_read_as_october_monthly_and_matches_lkc_titles():
    film = next(s for s in _october() if "filmas" in s["title"])
    assert resolvers._cinema_period(film["title"], film["rules"]) == ("monthly", 2026, 10)
    opts = [{"id": str(i), "label": o["label"]} for i, o in enumerate(film["options"])]
    # LKC's own title strings (from the weekly report) must map to the right option
    for lkc, want in [("(Ne)Tobuli melagiai (Perfect Strangers)", "„(Ne)tobuli melagiai“"),
                      ("Žvėries širdis (Heart of The Beast, The)", "„Žvėries širdis“"),
                      ("Absoliutus blogis (Resident Evil)", "„Absoliutus blogis“"),
                      ("Aviukas Šonas ir baimės pilnos kelnės (Shaun the Sheep: The Beast "
                       "of Mossy Bottom)", "„Aviukas Šonas ir baimės pilnos kelnės“"),
                      ("Digeris (Digger)", "„Digeris“"),
                      ("Tulpiniai. Lemtinga klaida", "„Tulpiniai. Lemtinga klaida“"),
                      ("Street Fighter", "Kitas filmas")]:
        opt, _kind = weather.match_cinema_option(lkc, opts)
        assert opt["label"] == want, lkc


def test_october_non_film_markets_are_not_touched_by_auto_resolvers():
    for s in _october():
        if "filmas" in s["title"]:
            continue
        m = {"title": s["title"], "rules": s["rules"],
             "market_options": [{"id": str(i), "label": o["label"]}
                                for i, o in enumerate(s["options"])]}
        assert resolvers._cinema_period(s["title"], s["rules"]) is None
        assert resolvers._agata_target(s["title"], s["rules"]) is None
        assert fuel.fuel_target(m) is None
        assert weather._sports_league(s["rules"]) is None
