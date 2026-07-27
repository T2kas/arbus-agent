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
    assert resolvers._weather_target(
        "Kokia bus aukščiausia temperatūra Vilniuje liepos 25, 2026?") is None
    # explicit ISO-style date is what the parser keys on
    hit = resolvers._weather_target("Aukščiausia temperatūra Vilniuje 2026-07-25?")
    assert hit == ("vilniaus-ams", "2026-07-25")
    assert resolvers._weather_target("Ar Žalgiris laimės 2026-07-30?") is None  # no temp


def test_weather_parser_with_no_readings_is_empty():
    assert resolvers.parse_weather({"observations": []}, "2026-07-25") == ""


# ── fuel ────────────────────────────────────────────────────────────────────

def test_fuel_parser_formats_available_prices():
    assert "dyzelinas 2.050 €/l" in resolvers.parse_fuel({"diesel": 2.05})
    assert resolvers.parse_fuel({}) == ""


# ── dispatcher ──────────────────────────────────────────────────────────────

def test_facts_for_never_raises_and_returns_empty_when_nothing_matches():
    assert resolvers.facts_for("Ar Seimas priims biudžetą iki gruodžio?") == ""
