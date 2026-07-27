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
