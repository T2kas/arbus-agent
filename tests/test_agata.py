"""AGATA weekly #1-song resolver: week detection, table parse, option matching."""

from datetime import datetime, timezone

from arbus import resolvers, weather


def test_agata_target_detects_year_and_week():
    assert resolvers._agata_target(
        "Kuri daina bus savaitės Nr. 1?",
        "Oficialus AGATA 2026 metų 37-osios savaitės singlų TOP 100.") == (2026, 37)
    assert resolvers._agata_target("Kuri daina Nr.1?", "be šaltinio, tik daina") is None


def test_agata_page_week_from_title():
    assert resolvers._agata_page_week(
        "<title>2026 37-os savaitės klausomiausi (TOP 100) - Agata</title>") == (2026, 37)


def test_parse_agata_uses_only_the_chart_table():
    html = """
    <table>
      <tr><th>Vieta</th><th>Praeitą savaitę</th><th>Atlikėjas/grupė</th><th>Pavadinimas</th></tr>
      <tr><td>1</td><td>2</td><td>Jessica Shy</td><td>Liepa</td></tr>
      <tr><td>2</td><td>1</td><td>Free Finga</td><td>Plastika</td></tr>
    </table>
    <table><tr><th>Kitas dalykas</th></tr><tr><td>99</td></tr></table>
    """
    rows = resolvers.parse_agata(html)
    assert rows[0] == (1, "Jessica Shy", "Liepa")
    assert len(rows) == 2 and rows[1][0] == 2


def test_parse_agata_picks_singles_not_albums():
    # The page has BOTH charts with identical headers — must select by caption.
    html = """
    <p>2026 37-os savaitės ALBUMŲ TOP100</p>
    <table><tr><th>Vieta</th><th>Atlikėjas/grupė</th><th>Pavadinimas</th></tr>
    <tr><td>1</td><td>Jessica Shy</td><td>Liepa</td></tr></table>
    <p>2026 37-os savaitės SINGLŲ TOP100</p>
    <table><tr><th>Vieta</th><th>Atlikėjas/grupė</th><th>Pavadinimas</th></tr>
    <tr><td>1</td><td>Jessica Shy</td><td>Kas Kaltas</td></tr></table>
    """
    assert resolvers.parse_agata(html, "singles")[0] == (1, "Jessica Shy", "Kas Kaltas")
    assert resolvers.parse_agata(html, "albums")[0] == (1, "Jessica Shy", "Liepa")


def _opts(*labels):
    return [{"id": f"o{i}", "label": l} for i, l in enumerate(labels)]


def test_match_song_named_and_other():
    opts = _opts("Jessica Shy „Žvėris“", "Different Dimension ir QV „Palangoj“", "Kita daina")
    assert weather.match_song_option("Jessica Shy", "Žvėris", opts)[1] == "named"
    assert weather.match_song_option("Jessica Shy", "Liepa", opts)[1] == "other"   # not listed
    o, kind = weather.match_song_option("Foo", "Bar", _opts("Jessica Shy „Žvėris“"))
    assert o is None and kind == "no_match"


def test_resolve_music_resolves_named(monkeypatch):
    market = {"id": "m1", "status": "closed", "winning_option_id": None,
              "title": "Kuri daina bus savaitės Nr. 1?",
              "rules": "AGATA 2026 metų 37-osios savaitės singlų TOP 100",
              "market_options": _opts("Jessica Shy „Žvėris“", "Kita daina")}
    monkeypatch.setattr(weather.config, "ARBUS_WRITE_KEY", "svc")
    monkeypatch.setattr(weather.resolvers, "agata_top", lambda q, r: {
        "desc": "2026 m. 37 sav.", "url": "u", "artist": "Jessica Shy",
        "title": "Žvėris", "top": [("Jessica Shy", "Žvėris")]})
    calls = []
    monkeypatch.setattr(weather.app_api, "resolve_market",
                        lambda mid, oid: (calls.append((mid, oid)), (True, "ok"))[1])
    monkeypatch.setattr(weather.notify, "send", lambda m: None)
    reports, changed = weather._resolve_music(
        [market], datetime.now(timezone.utc), weather._tz(), {}, alert=True, do_resolve=True)
    assert calls == [("m1", "o0")] and reports[0]["status"] == "resolved" and changed
