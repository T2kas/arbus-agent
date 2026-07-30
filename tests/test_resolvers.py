"""Offline tests for the resolution data feeds (parsers only, no network)."""

from arbus import resolvers


# ── stocks ──────────────────────────────────────────────────────────────────

def test_stock_parser_reports_price_and_period_high():
    payload = {"chart": {"result": [{
        "meta": {"symbol": "IGN1L.VS", "currency": "EUR",
                 "regularMarketPrice": 21.4},
        "indicators": {"quote": [{"close": [19.0, 20.5, 23.6, 22.1, 21.4]}]},
    }]}}
    fact = resolvers.parse_stock(payload, "Ignitis grupė")
    assert "21.40 €" in fact and "23.60 €" in fact          # current + high
    assert "Nasdaq" in fact


def test_stock_target_matches_ticker_or_name():
    assert resolvers._ticker_for("Ar Ignitis grupė akcija pakils virš 23 Eur?")
    assert resolvers._ticker_for("Ar IGN1L viršys 23 Eur?")
    assert resolvers._ticker_for("Ar oras bus geras?") is None


def test_stock_parser_survives_a_junk_payload():
    assert resolvers.parse_stock({}, "X") == ""


# ── weather ─────────────────────────────────────────────────────────────────

def test_weather_parser_takes_the_daily_high():
    payload = {"station": {"name": "Vilniaus AMS"},
               "observations": [{"airTemperature": 18.2}, {"airTemperature": 22.7},
                                {"airTemperature": 20.1}, {"airTemperature": None}]}
    fact = resolvers.parse_weather(payload, "2026-07-25")
    assert "22.7 °C" in fact and "23 °C" in fact            # raw + rounded
    assert "LHMT" in fact


def test_weather_target_needs_city_date_and_a_temperature_topic():
    hit = resolvers._weather_target("Aukščiausia temperatūra Vilniuje 2026-07-25?")
    assert hit == ("vilniaus-ams", "2026-07-25")
    assert resolvers._weather_target("Ar Žalgiris laimės 2026-07-30?") is None  # no temp
    # temperature topic + city but no date anywhere → cannot resolve
    assert resolvers._weather_target("Ar Vilniuje bus karšta?") is None


def test_weather_parser_with_no_readings_is_empty():
    assert resolvers.parse_weather({"observations": []}, "2026-07-25") == ""


# ── fuel ────────────────────────────────────────────────────────────────────

def test_fuel_parser_formats_available_prices():
    assert "dyzelinas 2.050 €/l" in resolvers.parse_fuel({"diesel": 2.05})
    assert resolvers.parse_fuel({}) == ""


# ── dispatcher ──────────────────────────────────────────────────────────────

def test_facts_for_never_raises_and_returns_empty_when_nothing_matches():
    assert resolvers.facts_for("Ar Seimas priims biudžetą iki gruodžio?") == ""


# ── Lithuanian date parsing (the weather bug) ───────────────────────────────

def test_weather_target_parses_lithuanian_month_dates():
    # "liepos 25, 2026" is how the market actually phrased it — not ISO.
    hit = resolvers._weather_target(
        "Kokia bus aukščiausia temperatūra Vilniuje liepos 25, 2026?")
    assert hit == ("vilniaus-ams", "2026-07-25")


def test_weather_target_falls_back_to_closes_at():
    hit = resolvers._weather_target(
        "Kokia bus aukščiausia temperatūra Kaune?", closes_at="2026-08-01T21:00:00Z")
    assert hit == ("kauno-ams", "2026-08-01")


def test_parse_date_handles_both_formats():
    assert resolvers._parse_date("2026-07-25") == "2026-07-25"
    assert resolvers._parse_date("rugpjūčio 3, 2026") == "2026-08-03"
    assert resolvers._parse_date("no date", "2026-09-09T00:00:00Z") == "2026-09-09"


# ── fuel: LEA HTML scrape (no clean API) ────────────────────────────────────

def test_fuel_html_parser_pulls_averages_out_of_the_page():
    html = ("<td>Dyzelinas</td><td>1,832 €</td>"
            "<td>95 benzinas</td><td>1,723 EUR</td>"
            "<td>Dujos (LPG)</td><td>0,772 €</td>")
    fact = resolvers.parse_fuel_html(html)
    assert "dyzelinas 1.832 €/l" in fact
    assert "benzinas 1.723 €/l" in fact
    assert "LEA" in fact


def test_fuel_html_parser_ignores_nonsense_numbers():
    assert resolvers.parse_fuel_html("<p>Dyzelinas pabrango 15 %</p>") == ""


# ── fuel: the LEA daily bulletin (real ena.lt post text, live-verified) ─────

# Real sentence shape from https://www.ena.lt/Naujiena/ndk-20260727/ — the
# comparison clause ("... nei penktadienį ..., kai buvo 1,979 Eur/l") must NOT
# be picked up as the average.
_BULLETIN_TEXT = (
    "<p>Lietuvoje pirmadienio rytą, palyginti su penktadieniu, vidutinės "
    "dyzelino ir SND kainos padidėjo.</p>"
    "<p>Dyzelino kainos degalinėse pirmadienio rytą svyravo nuo 1,820 Eur/l "
    "iki 2,127 Eur/l, o vidutinė dyzelino kaina siekė 1,982 Eur/l, arba 0,15 "
    "proc. daugiau nei penktadienį (liepos 24 d.), kai buvo 1,979 Eur/l.</p>"
    "<p>Benzino kainos pirmadienio rytą degalinėse svyravo nuo 1,659 Eur/l iki "
    "1,920 Eur/l, o vidutinė benzino kaina sudarė 1,773 Eur/l, arba 0,34 proc. "
    "mažiau nei penktadienį.</p>"
    "<p>SND kainos degalinėse svyravo nuo 0,660 Eur/l iki 0,909 Eur/l, o "
    "vidutinė SND kaina siekė 0,762 Eur/l.</p>"
)


def test_bulletin_parser_reads_the_average_not_the_comparison_figure():
    fact = resolvers.parse_fuel_bulletin(_BULLETIN_TEXT)
    assert "dyzelinas 1.982 €/l" in fact
    assert "benzinas 1.773 €/l" in fact
    assert "SND (dujos) 0.762 €/l" in fact
    assert "1.979" not in fact             # the day-over-day comparison figure


def test_bulletin_parser_empty_when_the_sentence_shape_is_absent():
    assert resolvers.parse_fuel_bulletin("<p>Elektros kainos padidėjo.</p>") == ""


def test_recent_naujiena_urls_sorts_by_lastmod_descending():
    sitemap = (
        "<url><loc>https://www.ena.lt/Naujiena/old-electricity/</loc>"
        "<lastmod>2026-07-20</lastmod></url>"
        "<url><loc>https://www.ena.lt/Naujiena/fresh-fuel-post/</loc>"
        "<lastmod>2026-07-29</lastmod></url>"
        "<url><loc>https://www.ena.lt/apie-mus/</loc>"          # not a Naujiena URL
        "<lastmod>2026-07-29</lastmod></url>"
    )
    urls = resolvers.recent_naujiena_urls(sitemap)
    assert urls[0].endswith("fresh-fuel-post/")
    assert all("/Naujiena/" in u for u in urls)


def _bulletin(diesel: str, day: str) -> str:
    return (f"<p>Vidutinė dyzelino kaina siekė {diesel} Eur/l ({day}).</p>"
            "<p>Vidutinė benzino kaina sudarė 1,773 Eur/l.</p>")


def test_fuel_fact_reports_the_period_high_not_only_today(monkeypatch):
    """The live bug the user caught: diesel read 2,030 today but had hit 2,069
    days earlier, so a '≥2,05 at least once' market is already Taip — not 'dar
    neaišku'. The fact must walk recent bulletins and surface the running high
    with the day (and URL) it occurred, like the stock feed's period high."""
    sitemap_xml = (
        "<url><loc>https://www.ena.lt/Naujiena/degalu-kainos-liepos-30/</loc>"
        "<lastmod>2026-07-30</lastmod></url>"
        "<url><loc>https://www.ena.lt/Naujiena/dyzelino-kainos-liepos-28/</loc>"
        "<lastmod>2026-07-28</lastmod></url>"
        "<url><loc>https://www.ena.lt/Naujiena/benzino-kainos-liepos-24/</loc>"
        "<lastmod>2026-07-24</lastmod></url>"
        "<url><loc>https://www.ena.lt/Naujiena/apie-elektra/</loc>"   # not fuel
        "<lastmod>2026-07-31</lastmod></url>"
    )

    class _R:
        def __init__(self, text, status=200):
            self.text, self.status_code = text, status
        def raise_for_status(self):
            pass

    pages = {
        "degalu-kainos-liepos-30": _bulletin("2,030", "liepos 30 d."),
        "dyzelino-kainos-liepos-28": _bulletin("2,069", "liepos 28 d."),
        "benzino-kainos-liepos-24": _bulletin("1,982", "liepos 24 d."),
    }

    def fake_get(url, **kw):
        if url.endswith("sitemap.xml"):
            return _R(sitemap_xml)
        for slug, html in pages.items():
            if slug in url:
                return _R(html)
        raise AssertionError(f"unexpected fetch (non-fuel URL?): {url}")

    monkeypatch.setattr(resolvers.requests, "get", fake_get)
    fact = resolvers.fuel_bulletin_fact()
    assert "naujausia (liepos 30 d.): dyzelinas 2.030" in fact   # latest day
    assert "2.069 €/l (liepos 28 d." in fact                     # period high
    assert "dyzelino-kainos-liepos-28" in fact                   # high's own URL


# ── diagnose(): the arbus-facts visibility that ends the guessing ───────────

def test_diagnose_reports_a_network_failure_not_silent_empty(monkeypatch):
    """The Ignitis case: the feed applies, but Yahoo refused — that must show as
    an error, not as 'no feed'."""
    def boom(url, **k):
        raise Exception("403 Forbidden")
    monkeypatch.setattr(resolvers, "_get_json", boom)
    rows = resolvers.diagnose("Ar Ignitis grupė akcija pakils virš 23 Eur?")
    assert len(rows) == 1
    feed, fact, error = rows[0]
    assert feed == "akcijos" and fact == "" and "403" in error


def test_diagnose_returns_the_fact_when_the_feed_works(monkeypatch):
    payload = {"chart": {"result": [{
        "meta": {"symbol": "IGN1L.VS", "currency": "EUR", "regularMarketPrice": 22.7},
        "indicators": {"quote": [{"close": [21.0, 22.7]}]}}]}}
    monkeypatch.setattr(resolvers, "_get_json", lambda url, **k: payload)
    rows = resolvers.diagnose("Ar Ignitis grupė akcija pakils virš 23 Eur?")
    assert rows[0][0] == "akcijos" and "22.70" in rows[0][1] and rows[0][2] == ""


def test_diagnose_weather_reports_meteo_failure(monkeypatch):
    monkeypatch.setattr(resolvers, "_get_json",
                        lambda url, **k: (_ for _ in ()).throw(Exception("timeout")))
    rows = resolvers.diagnose("Aukščiausia temperatūra Vilniuje 2020-07-25?")
    assert rows[0][0] == "oras" and "timeout" in rows[0][2]


def test_diagnose_empty_when_no_feed_applies():
    assert resolvers.diagnose("Ar Seimas priims biudžetą?") == []
