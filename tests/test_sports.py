"""Sports match resolvers: Euroleague feed, TOPLYGA scrape, and safe mapping."""

from datetime import datetime, timezone

from arbus import sports, weather

_FOOTBALL_RULES = ("Rinka pagal FK „Žalgirio“ ir „Kauno Žalgirio“ TOPLYGOS rungtynių, "
                   "numatytų 2020 m. rugsėjo 16 d., rezultatą. Lygiosios – baigtis.")
_BASKET_RULES = ("Kauno „Žalgirio“ ir Pirėjo „Olympiacos“ 2026–2027 m. Eurolygos "
                 "reguliariojo sezono rungtynių rezultatą. Lygiosios negalima.")


def test_season_year_and_rules_date():
    assert sports.euroleague_season_year(_BASKET_RULES) == 2026
    assert sports._rules_date(_FOOTBALL_RULES) == "2020-09-16"


def test_toplyga_find_and_parse():
    listing = ('<a href="/rungtynes/2020-09-16-zalgiris-k-zalgiris/2200">x</a>'
               '<a href="/rungtynes/2020-09-16-suduva-siauliai/2201">y</a>')
    path = sports.toplyga_find_match("2020-09-16", {"zalgiris"}, {"kauno", "zalgiris"}, listing)
    assert path == "rungtynes/2020-09-16-zalgiris-k-zalgiris/2200"
    assert sports._toplyga_teams(
        "<title>Žalgiris - K. Žalgiris | TOPLYGA</title>") == ("Žalgiris", "K. Žalgiris")


def test_toplyga_score_reads_own_match_not_a_sidebar():
    # Regression: the page lists OTHER fixtures' scores first; the real score is the
    # one whose link points to THIS match. (Bug: Sūduva–Žalgiris read 1:0 vs real 0:0.)
    path = "rungtynes/2026-09-13-suduva-zalgiris/2193"
    html = ('<a href="https://toplyga.lt/rungtynes/2026-09-06-zalgiris-siauliai/2189">1 : 0</a>'
            '<a href="https://toplyga.lt/rungtynes/2026-09-13-suduva-zalgiris/2193">0 : 0</a>')
    assert sports._toplyga_score(html, path) == (0, 0)
    assert sports._toplyga_score("<a href='/other/9'>3 : 2</a>", path) is None  # no self-link → None


def test_euroleague_games_parse(monkeypatch):
    monkeypatch.setattr(sports, "_el_fetch", lambda year: [
        {"status": "result", "home": {"name": "Zalgiris Kaunas", "score": 90},
         "away": {"name": "Olympiacos Piraeus", "score": 88}, "date": "2026-10-05T18:00:00Z"},
        {"status": "scheduled", "home": {"name": "A", "score": None},
         "away": {"name": "B", "score": None}, "date": "2026-10-12T18:00:00Z"},
    ])
    games = sports.euroleague_games(2026)
    assert len(games) == 1 and games[0]["home_score"] == 90 and games[0]["away"] == "Olympiacos Piraeus"


def test_assign_two_teams_derby_and_ambiguous():
    opts = [{"id": "o0", "label": "FK Žalgiris"}, {"id": "o1", "label": "Kauno Žalgiris"}]
    a = weather.assign_two_teams("Žalgiris", "K. Žalgiris", opts)
    assert a["home"]["label"] == "FK Žalgiris" and a["away"]["label"] == "Kauno Žalgiris"
    # identical names on both sides → cannot tell → None
    assert weather.assign_two_teams("Žalgiris", "Žalgiris", opts) is None


def _football_market(date="2020-09-16"):
    return {"id": "m1", "status": "closed", "winning_option_id": None,
            "title": "Žalgiris vs Kauno Žalgiris?",
            "rules": _FOOTBALL_RULES.replace("2020-09-16", date),
            "market_options": [{"id": "o0", "label": "FK Žalgiris"},
                               {"id": "o1", "label": "Kauno Žalgiris"},
                               {"id": "o2", "label": "Lygiosios"}]}


def _wire(monkeypatch):
    calls = {"resolve": [], "telegram": []}
    monkeypatch.setattr(weather.config, "ARBUS_WRITE_KEY", "svc")
    monkeypatch.setattr(weather.app_api, "resolve_market",
                        lambda mid, oid: (calls["resolve"].append((mid, oid)), (True, "ok"))[1])
    monkeypatch.setattr(weather.notify, "send", lambda m: calls["telegram"].append(m))
    return calls


def test_resolve_football_home_win(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(weather.sports, "toplyga_result", lambda iso, a, b: {
        "home": "Žalgiris", "away": "K. Žalgiris", "home_score": 2, "away_score": 1,
        "url": "u", "date": iso})
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    reports, changed = weather._resolve_sports([_football_market()], now, weather._tz(), {},
                                               alert=True, do_resolve=True)
    assert calls["resolve"] == [("m1", "o0")] and reports[0]["status"] == "resolved"


def test_resolve_football_draw(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(weather.sports, "toplyga_result", lambda iso, a, b: {
        "home": "Žalgiris", "away": "K. Žalgiris", "home_score": 1, "away_score": 1,
        "url": "u", "date": iso})
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
    reports, _ = weather._resolve_sports([_football_market()], now, weather._tz(), {},
                                         alert=True, do_resolve=True)
    assert calls["resolve"] == [("m1", "o2")]                # Lygiosios


def test_football_not_resolved_before_match_day(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(weather.sports, "toplyga_result",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not fetch")))
    now = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)   # before the 2026-09-16 match
    reports, _ = weather._resolve_sports([_football_market("2026-09-16")], now, weather._tz(), {},
                                         alert=True, do_resolve=True)
    assert calls["resolve"] == []


def test_resolve_basketball(monkeypatch):
    calls = _wire(monkeypatch)
    market = {"id": "m2", "status": "closed", "winning_option_id": None,
              "title": "Žalgiris vs Olympiacos?", "rules": _BASKET_RULES,
              "market_options": [{"id": "z", "label": "Kauno „Žalgiris“"},
                                 {"id": "o", "label": "Pirėjo „Olympiacos“"}]}
    monkeypatch.setattr(weather.sports, "euroleague_games", lambda year: [
        {"home": "Zalgiris Kaunas", "away": "Olympiacos Piraeus",
         "home_score": 95, "away_score": 80, "url": "u", "date": "2026-10-05"}])
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    reports, _ = weather._resolve_sports([market], now, weather._tz(), {},
                                         alert=True, do_resolve=True)
    assert calls["resolve"] == [("m2", "z")] and reports[0]["status"] == "resolved"


def test_toplyga_finished_marker():
    assert sports._toplyga_finished("blah Rungtynių pabaiga blah") is True
    assert sports._toplyga_finished("Antrojo kėlinio pabaiga") is True
    assert sports._toplyga_finished("Pirmojo kėlinio pabaiga") is False   # half-time only
    assert sports._toplyga_finished("rungtynės vyksta") is False


def _today_football_rules():
    from datetime import date
    mo = ["", "sausio", "vasario", "kovo", "balandžio", "gegužės", "birželio",
          "liepos", "rugpjūčio", "rugsėjo", "spalio", "lapkričio", "gruodžio"]
    d = date.today()
    return (f"FK „Žalgirio“ ir „Kauno Žalgirio“ TOPLYGOS rungtynių, {d.year} m. "
            f"{mo[d.month]} {d.day} d. rezultatą. Lygiosios – baigtis.")


def _today_market():
    return {"id": "m9", "status": "closed", "winning_option_id": None,
            "title": "derby", "rules": _today_football_rules(),
            "market_options": [{"id": "o0", "label": "FK Žalgiris"},
                               {"id": "o1", "label": "Kauno Žalgiris"},
                               {"id": "o2", "label": "Lygiosios"}]}


def test_football_match_day_waits_for_full_time(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(weather.sports, "toplyga_result", lambda iso, a, b: {
        "home": "Žalgiris", "away": "K. Žalgiris", "home_score": 1, "away_score": 0,
        "url": "u", "date": iso, "finished": False})               # live/half-time
    reports, _ = weather._resolve_sports([_today_market()], datetime.now(timezone.utc),
                                         weather._tz(), {}, alert=True, do_resolve=True)
    assert calls["resolve"] == []                                  # not full-time → wait


def test_football_match_day_resolves_once_full_time(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(weather.sports, "toplyga_result", lambda iso, a, b: {
        "home": "Žalgiris", "away": "K. Žalgiris", "home_score": 3, "away_score": 2,
        "url": "u", "date": iso, "finished": True})
    reports, _ = weather._resolve_sports([_today_market()], datetime.now(timezone.utc),
                                         weather._tz(), {}, alert=True, do_resolve=True)
    assert calls["resolve"] == [("m9", "o0")]                      # FK Žalgiris (home) won
