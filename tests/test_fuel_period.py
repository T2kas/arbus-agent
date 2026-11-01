"""Period fuel markets (monthly/yearly max, "pasieks X €/l iki...?") and the
reviewed decision-file resolver."""

import json
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from arbus import app as app_api, fuel_period, manual_resolve

TZ = ZoneInfo("Europe/Vilnius")

MAX_RULES = ("Rinka bus išspręsta pagal aukščiausią Lietuvos energetikos agentūros paskelbtą "
             "vienos dienos vidutinę dyzelino kainą nuo 2026 m. spalio 1 d. iki spalio 31 d. imtinai.")
YEAR_RULES = "… dyzelino kainą nuo 2026 m. rugsėjo 23 d. iki gruodžio 31 d. imtinai."
TH_RULES = ("„Taip“ laimi, jeigu bent vienos dienos nuo 2026 m. rugsėjo 23 d. iki konkrečios "
            "datos imtinai oficiali Lietuvos vidutinė dyzelino kaina bus 2,400 €/l arba didesnė.")


def _opt(i, label, outcome=None):
    return {"id": f"o{i}", "label": label, "resolved_outcome": outcome}


def _max_market():
    return {"id": "m1", "title": "Aukščiausia vidutinė dyzelino kaina spalį?", "status": "closed",
            "rules": MAX_RULES,
            "market_options": [_opt(1, "2,299 €/l arba mažiau"), _opt(2, "Nuo 2,300 iki 2,349 €/l"),
                               _opt(3, "2,350 €/l arba daugiau")]}


def _ladder():
    return {"id": "m2", "title": "Vidutinė dyzelino kaina Lietuvoje pasieks 2,40 €/l iki...?",
            "status": "open", "rules": TH_RULES,
            "market_options": [_opt(1, "2026 m. rugsėjo 30 d.", "no"), _opt(2, "2026 m. spalio 31 d."),
                               _opt(3, "2026 m. gruodžio 31 d.")]}


def test_classify_reads_period_and_threshold():
    s = fuel_period.classify(_max_market())
    assert s == {"kind": "max", "fuel": "dyzelinas", "start": date(2026, 10, 1), "end": date(2026, 10, 31)}
    y = fuel_period.classify({"title": "Aukščiausia vidutinė dyzelino kaina Lietuvoje 2026 m.",
                              "rules": YEAR_RULES})
    assert (y["start"], y["end"]) == (date(2026, 9, 23), date(2026, 12, 31))
    t = fuel_period.classify(_ladder())
    assert t["kind"] == "threshold" and t["threshold"] == 2.40 and t["start"] == date(2026, 9, 23)
    bad = fuel_period.classify({"title": "Aukščiausia vidutinė benzino A95 kaina Lietuvoje 2026 m.",
                                "rules": "tik kontekstas"})
    assert "error" in bad
    assert fuel_period.classify({"title": "Aukščiausia temperatūra Vilniuje?"}) is None


def _fake_fetch(prices, default=None):
    calls = []

    def fetch(iso, fuel):
        calls.append(iso)
        return prices.get(iso, default), "https://ena.lt/x"
    return fetch, calls


def test_max_waits_until_period_is_final_then_resolves(monkeypatch):
    prices = {"2026-10-01": 2.268, "2026-10-20": 2.268, "2026-10-30": 2.248}
    fetch, calls = _fake_fetch(prices)
    done = []
    monkeypatch.setattr(app_api, "resolve_market", lambda mid, oid: (done.append(oid), (True, "ok"))[1])
    monkeypatch.setattr(app_api, "eliminate_options", lambda mid, ids: (True, "ok"))
    state = {}
    # Oct 31 morning: Oct 30's value is not final until Oct 31 12:00 -> no winner yet
    early = datetime(2026, 10, 31, 8, tzinfo=timezone.utc)
    rep, _ = fuel_period.resolve_markets([_max_market()], early, TZ, state, alert=False,
                                         do_resolve=True, fetch=fetch)
    assert not done
    # Nov 2 (after Nov 1 12:00, the cutoff for Oct 31): every day known -> resolve
    late = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    fuel_period.resolve_markets([_max_market()], late, TZ, state, alert=False,
                                do_resolve=True, fetch=fetch)
    assert done == ["o1"]
    # the cache means a second pass fetches nothing new for known days
    n = len(calls)
    fuel_period.resolve_markets([_max_market()], late, TZ, state, alert=False,
                                do_resolve=False, fetch=fetch)
    assert len(calls) == n


def test_max_eliminates_buckets_below_running_max(monkeypatch):
    fetch, _ = _fake_fetch({"2026-10-06": 2.31})
    elim = []
    monkeypatch.setattr(app_api, "eliminate_options", lambda mid, ids: (elim.extend(ids), (True, "ok"))[1])
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    fuel_period.resolve_markets([_max_market()], now, TZ, {}, alert=False, do_resolve=True, fetch=fetch)
    assert elim == ["o1"]


def test_threshold_ladder_yes_and_no(monkeypatch):
    got = []
    monkeypatch.setattr(app_api, "settle_option", lambda mid, oid, out: (got.append((oid, out)), (True, "ok"))[1])
    # never reached by Oct 31 -> Oct 31 "no"; Dec 31 still open
    fetch, _ = _fake_fetch({"2026-09-23": 2.297, "2026-10-20": 2.268}, default=2.2)
    now = datetime(2026, 11, 2, 12, tzinfo=timezone.utc)
    monkeypatch.setattr(fuel_period, "MAX_FETCHES_PER_RUN", 100)
    fuel_period.resolve_markets([_ladder()], now, TZ, {}, alert=False, do_resolve=True, fetch=fetch)
    assert got == [("o2", "no")]
    # reached on Nov 10 -> Dec 31 "yes" (Oct 31 already settled in the app)
    got.clear()
    m = _ladder()
    m["market_options"][1]["resolved_outcome"] = "no"
    fetch, _ = _fake_fetch({"2026-11-10": 2.405}, default=2.2)
    now = datetime(2026, 11, 11, 12, tzinfo=timezone.utc)
    fuel_period.resolve_markets([m], now, TZ, {}, alert=False, do_resolve=True, fetch=fetch)
    assert got == [("o3", "yes")]


def test_decision_file_is_refused_on_any_mismatch(tmp_path, monkeypatch):
    rows = [{"id": "m1", "title": "Ar X?", "status": "closed",
             "market_options": [_opt(1, "Taip"), _opt(2, "Ne")]},
            _ladder()]
    entries = [{"market_id": "m1", "title": "Ar X?", "resolve": "Ne"},
               {"market_id": "m2", "title": _ladder()["title"],
                "options": {"2026 m. spalio 31 d.": "no", "2026 m. rugsėjo 30 d.": "no"}}]
    actions, errors = manual_resolve.plan(entries, rows)
    assert not errors
    kinds = [(a["kind"], a.get("outcome")) for a in actions]
    assert kinds == [("resolve", None), ("option", "no"), ("skip", None)]   # Sep 30 already "no"

    bad = [{"market_id": "m1", "title": "Ar Y?", "resolve": "Ne"}]
    assert manual_resolve.plan(bad, rows)[1]
    bad = [{"market_id": "m1", "title": "Ar X?", "resolve": "Gal"}]
    assert manual_resolve.plan(bad, rows)[1]

    f = tmp_path / "d.json"
    f.write_text(json.dumps(bad), "utf-8")
    monkeypatch.setattr(app_api, "markets", lambda n: (rows, ""))
    called = []
    monkeypatch.setattr(app_api, "resolve_market", lambda *a: called.append(a))
    reports, err = manual_resolve.run(str(f), dry_run=False, alert=False)
    assert err and not called


def test_november_decision_and_batch_files_are_valid():
    from arbus import batch_create
    now = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)
    specs = batch_create.load("markets/2026-11.json")
    assert len(specs) == 11
    for s in specs:
        assert batch_create.validate(s, now) == [], s["title"]
        assert "spal" not in s["title"]
    for e in manual_resolve.load("markets/resolutions/2026-11-01.json"):
        assert e["market_id"] and e["title"] and e["source"].startswith("https://")


def test_settle_option_routes_no_through_elimination(monkeypatch):
    calls = []
    monkeypatch.setattr(app_api, "eliminate_options", lambda mid, ids: (calls.append("elim"), (True, "ok"))[1])
    monkeypatch.setattr(app_api, "resolve_option", lambda oid, out: (calls.append(out), (False, "not authenticated"))[1])
    assert app_api.settle_option("m", "o", "no") == (True, "ok") and calls == ["elim"]
    calls.clear()
    assert app_api.settle_option("m", "o", "yes")[0] is False and calls == ["yes"]
    calls.clear()
    monkeypatch.setattr(app_api, "eliminate_options",
                        lambda mid, ids: (False, "cannot eliminate all remaining options; resolve a winner instead"))
    app_api.settle_option("m", "o", "no")
    assert calls == ["no"]                     # last open option -> resolve_option
