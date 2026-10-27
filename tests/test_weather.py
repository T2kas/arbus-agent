"""Weather bot: bucket parsing, LT-day/UTC handling, and the resolve decisions."""

from datetime import datetime, timezone

import pytest

from arbus import app as app_api, config, weather


# ── option buckets ───────────────────────────────────────────────────────────

def test_parse_bucket_the_three_label_shapes():
    assert weather.parse_bucket("16,9 °C arba žemesnė") == (None, 16.9)
    assert weather.parse_bucket("17,0 iki 18,9 °C") == (17.0, 18.9)
    assert weather.parse_bucket("19,0 iki 20,9 °C") == (19.0, 20.9)
    assert weather.parse_bucket("21,0 °C arba aukštesnė") == (21.0, None)


def _market(options, **extra):
    m = {"id": "m1", "category": "orai", "status": "open",
         "title": "Aukščiausia temperatūra Kaune rugsėjo 10 d.?",
         "subtitle": "2026 m. rugsėjo 10 d.", "rules": "",
         "winning_option_id": None, "market_options": options}
    m.update(extra)
    return m


_OPTS = [
    {"id": "b1", "label": "16,9 °C arba žemesnė", "sort_order": 0},
    {"id": "b2", "label": "17,0 iki 18,9 °C", "sort_order": 1},
    {"id": "b3", "label": "19,0 iki 20,9 °C", "sort_order": 2},
    {"id": "b4", "label": "21,0 °C arba aukštesnė", "sort_order": 3},
]


def test_top_bucket_and_temp_mapping():
    buckets = weather.buckets_from_market(_market(_OPTS))
    assert weather.top_bucket(buckets)["option_id"] == "b4"
    pick = lambda t: weather.bucket_for_temp(buckets, t)["option_id"]
    assert pick(12.0) == "b1"
    assert pick(16.9) == "b1"
    assert pick(17.0) == "b2"
    assert pick(18.9) == "b2"
    assert pick(19.0) == "b3"
    assert pick(20.9) == "b3"
    assert pick(21.0) == "b4"
    assert pick(24.3) == "b4"


# ── measurement validation & time handling ───────────────────────────────────

def test_valid_temp_rejects_null_nan_bool_and_strings():
    assert weather.valid_temp(18.4) and weather.valid_temp(0) and weather.valid_temp(-3)
    assert not weather.valid_temp(None)
    assert not weather.valid_temp(float("nan"))
    assert not weather.valid_temp(float("inf"))
    assert not weather.valid_temp(True)          # bool is not a temperature
    assert not weather.valid_temp("18.4")


def test_parse_obs_time_formats():
    got = weather.parse_obs_time_utc("2026-09-10 15:00:00")
    assert got == datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
    assert weather.parse_obs_time_utc("2026-09-10T15:00:00Z") == got
    assert weather.parse_obs_time_utc("") is None
    assert weather.parse_obs_time_utc(None) is None


def test_lt_day_spans_two_utc_dates_and_drops_invalid():
    # LT day 2026-09-10 (summer, UTC+3) = 2026-09-09 21:00 .. 2026-09-10 20:59 UTC.
    obs = [
        {"observationTimeUtc": "2026-09-09 20:00:00", "airTemperature": 99.0},  # 09-09 23:00 LT → out
        {"observationTimeUtc": "2026-09-09 21:00:00", "airTemperature": 10.0},  # 09-10 00:00 LT → in
        {"observationTimeUtc": "2026-09-10 12:00:00", "airTemperature": 18.9},  # 09-10 15:00 LT → in
        {"observationTimeUtc": "2026-09-10 20:00:00", "airTemperature": 14.0},  # 09-10 23:00 LT → in (last hour)
        {"observationTimeUtc": "2026-09-10 21:00:00", "airTemperature": 88.0},  # 09-11 00:00 LT → out
        {"observationTimeUtc": "2026-09-10 13:00:00", "airTemperature": None},  # dropped
        {"observationTimeUtc": "bad", "airTemperature": 50.0},                  # dropped
    ]
    ms = weather.lt_day_measurements(obs, "2026-09-10")
    temps = sorted(t for _, t in ms)
    assert temps == [10.0, 14.0, 18.9]
    assert weather.day_max(obs, "2026-09-10")[0] == 18.9
    assert weather.day_complete(obs, "2026-09-10") is True          # has the 23:00 LT reading


def test_day_incomplete_without_last_hour():
    obs = [{"observationTimeUtc": "2026-09-10 12:00:00", "airTemperature": 18.9}]
    assert weather.day_complete(obs, "2026-09-10") is False


# ── station / date detection ─────────────────────────────────────────────────

def test_parse_target_date_iso_and_lithuanian():
    assert weather.parse_target_date("2026-09-10") == "2026-09-10"
    assert weather.parse_target_date("2026 m. rugsėjo 10 d.") == "2026-09-10"
    assert weather.parse_target_date("rugsėjo 10 d.?") == ""      # no year → undecidable


def test_weather_target_from_market():
    assert weather.weather_target(_market(_OPTS)) == ("kauno-ams", "2026-09-10")
    vil = _market(_OPTS, title="Aukščiausia temperatūra Vilniuje rugsėjo 10 d.?")
    assert weather.weather_target(vil) == ("vilniaus-ams", "2026-09-10")


# ── orchestration: the resolve decisions ─────────────────────────────────────

@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Stub the network + app writes; capture resolve/freeze/telegram calls."""
    calls = {"resolve": [], "freeze": [], "telegram": []}
    monkeypatch.setattr(config, "WEATHER_STATE_PATH", str(tmp_path / "w.json"))
    monkeypatch.setattr(config, "ARBUS_WRITE_KEY", "service-role")
    # Pin the resolution timing so tests are independent of the shipped defaults.
    monkeypatch.setattr(config, "WEATHER_RESOLVE_MIN_HOUR", 17)
    monkeypatch.setattr(config, "WEATHER_DECLINE_HOURS", 2)
    monkeypatch.setattr(weather.app_api, "resolve_market",
                        lambda mid, oid: (calls["resolve"].append((mid, oid)), (True, "ok"))[1])
    monkeypatch.setattr(weather.app_api, "freeze_market",
                        lambda mid: (calls["freeze"].append(mid), (True, "ok"))[1])
    monkeypatch.setattr(weather.notify, "send", lambda msg: calls["telegram"].append(msg))
    # Resolution tests should not touch the creation path.
    monkeypatch.setattr(weather, "plan_new_markets", lambda *a, **k: [])
    # Daily fuel markets are exercised by their own tests, not the weather ones.
    monkeypatch.setattr(config, "FUEL_ENABLED", False)
    # Early per-bucket elimination has its own dedicated test; keep it out of the
    # resolution-flow tests (their fixture does not mock eliminate_options).
    monkeypatch.setattr(config, "WEATHER_ELIMINATE", False)
    return calls


def _obs(*pairs):
    return {"observations": [{"observationTimeUtc": t, "airTemperature": v}
                             for t, v in pairs]}


def _u(h):     # UTC datetime helper for the pure decline tests
    return datetime(2026, 9, 10, h, 0, tzinfo=timezone.utc)


def test_decline_locked_pure():
    assert weather.decline_locked([(_u(12), 20.9), (_u(13), 20.0), (_u(14), 19.5)], 2) is True
    assert weather.decline_locked([(_u(12), 18.0), (_u(13), 19.0), (_u(14), 20.0)], 2) is False  # rising
    assert weather.decline_locked([(_u(12), 20.9), (_u(13), 20.0)], 2) is False                  # only 1 drop
    assert weather.decline_locked([(_u(12), 20.9), (_u(14), 19.5), (_u(15), 19.0)], 2) is False  # gap after peak
    assert weather.decline_locked([], 2) is False
    # Oscillation below the peak must NOT lock (17→18h goes 16.7→17.0, i.e. back up):
    # the real Vilnius 09-12 curve that resolved too early under the old rule.
    osc = [(_u(11), 17.6), (_u(12), 16.9), (_u(13), 17.6), (_u(14), 16.7), (_u(15), 17.0)]
    assert weather.decline_locked(osc, 2) is False
    # Genuinely falling for 2h → locks.
    assert weather.decline_locked([(_u(13), 17.6), (_u(14), 16.7), (_u(15), 16.2)], 2) is True
    # Bounce AFTER the peak, then a fresh 2h downtrend → must still lock. The old
    # rule anchored the streak at the peak, so this single bounce killed early
    # resolution for the whole day; now the later downtrend is what matters.
    # 18.3 peak → 17.6 (drop) → 17.8 (bounce) → 17.4 (drop) → 17.0 (drop) = 2 in a row.
    bounce = [(_u(11), 18.3), (_u(12), 17.6), (_u(13), 17.8),
              (_u(14), 17.4), (_u(15), 17.0)]
    assert weather.decline_locked(bounce, 2) is True
    # But a lone drop-then-bounce with no later 2-in-a-row still waits.
    assert weather.decline_locked([(_u(12), 18.3), (_u(13), 17.2), (_u(14), 17.6)], 2) is False


# The bot NEVER freezes/closes now — a separate system owns that. Every resolving
# test also asserts wired["freeze"] == [].

def test_top_bucket_resolves_in_the_evening_no_freeze(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    # By 17:00 Vilnius (=14:00 UTC) the max is 21.5 → top bucket locked.
    monkeypatch.setattr(weather, "fetch", lambda url, timeout=20: (
        _obs(("2026-09-10 12:00:00", 20.0), ("2026-09-10 14:00:00", 21.5)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day",
                        lambda s, iso: ([{"observationTimeUtc": "2026-09-10 14:00:00",
                                          "airTemperature": 21.5}], ["url"], "sum"))
    now = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)    # 18:00 Vilnius
    reports, err = weather.run(now=now)
    assert err == "" and wired["freeze"] == []
    assert wired["resolve"] == [("m1", "b4")]
    assert reports[0]["status"] == "resolved" and reports[0]["via"] == "top_locked"


def test_decline_resolves_middle_bucket(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    # 15:00→20.9 peak, then 16:00→20.0, 17:00→19.5: two straight drops → locked.
    monkeypatch.setattr(weather, "fetch", lambda url, timeout=20: (
        _obs(("2026-09-10 12:00:00", 20.9), ("2026-09-10 13:00:00", 20.0),
             ("2026-09-10 14:00:00", 19.5)), "{raw}"))
    day = [{"observationTimeUtc": "2026-09-10 12:00:00", "airTemperature": 20.9},
           {"observationTimeUtc": "2026-09-10 13:00:00", "airTemperature": 20.0},
           {"observationTimeUtc": "2026-09-10 14:00:00", "airTemperature": 19.5}]
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: (day, ["u"], "sum"))
    now = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)    # 18:00 Vilnius
    reports, _ = weather.run(now=now)
    assert wired["freeze"] == []
    assert wired["resolve"] == [("m1", "b3")]                  # 19,0–20,9
    assert reports[0]["via"] == "decline"


def test_does_not_resolve_before_min_hour(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    # Even at 21.5 (top bucket) — before 17:00 Vilnius nothing resolves.
    monkeypatch.setattr(weather, "fetch", lambda url, timeout=20: (
        _obs(("2026-09-10 11:00:00", 21.5)), "{raw}"))     # 14:00 Vilnius
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: ([], [], "x"))
    now = datetime(2026, 9, 10, 11, 30, tzinfo=timezone.utc)   # 14:30 Vilnius (<17)
    reports, _ = weather.run(now=now)
    assert wired["resolve"] == [] and reports[0]["status"] == "watch"


def test_does_not_resolve_while_still_rising(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch", lambda url, timeout=20: (
        _obs(("2026-09-10 12:00:00", 18.0), ("2026-09-10 13:00:00", 19.0),
             ("2026-09-10 14:00:00", 20.0)), "{raw}"))       # still climbing, no drops
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: ([], [], "x"))
    now = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)    # 18:00 Vilnius, past min hour
    reports, _ = weather.run(now=now)
    assert wired["resolve"] == [] and reports[0]["status"] == "watch"


def test_end_of_day_resolves_the_matching_bucket(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 12:00:00", 18.9)), "{raw}"))
    day = [{"observationTimeUtc": "2026-09-10 12:00:00", "airTemperature": 18.9},
           {"observationTimeUtc": "2026-09-10 20:00:00", "airTemperature": 14.0}]  # 23:00 LT present
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: (day, ["u1", "u2"], "sum"))
    now = datetime(2026, 9, 10, 21, 30, tzinfo=timezone.utc)   # already 09-11 in Vilnius
    reports, _ = weather.run(now=now)
    assert wired["freeze"] == [] and wired["resolve"] == [("m1", "b2")]
    assert reports[0]["via"] == "end_of_day"


def test_waits_when_last_hour_is_missing(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 12:00:00", 18.9)), "{raw}"))
    day = [{"observationTimeUtc": "2026-09-10 12:00:00", "airTemperature": 18.9}]  # no 23:00 LT
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: (day, ["u"], "sum"))
    now = datetime(2026, 9, 10, 21, 30, tzinfo=timezone.utc)
    reports, _ = weather.run(now=now)
    assert wired["resolve"] == [] and reports[0]["status"] == "wait"


def test_already_decided_market_is_skipped(wired, monkeypatch):
    m = _market(_OPTS, winning_option_id="b2")
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([m], ""))
    reports, _ = weather.run(now=datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc))
    assert reports == [] and wired["resolve"] == []


def test_no_double_resolve_across_runs(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch", lambda url, timeout=20: (
        _obs(("2026-09-10 12:00:00", 20.0), ("2026-09-10 14:00:00", 21.5)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day",
                        lambda s, iso: ([{"observationTimeUtc": "2026-09-10 14:00:00",
                                          "airTemperature": 21.5}], ["url"], "sum"))
    now = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
    weather.run(now=now)
    weather.run(now=now)                                 # second pass sees state.resolved
    assert wired["resolve"] == [("m1", "b4")]            # resolved exactly once


def test_no_resolve_flag_only_monitors(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch", lambda url, timeout=20: (
        _obs(("2026-09-10 12:00:00", 20.0), ("2026-09-10 14:00:00", 21.5)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day",
                        lambda s, iso: ([{"observationTimeUtc": "2026-09-10 14:00:00",
                                          "airTemperature": 21.5}], ["url"], "sum"))
    now = datetime(2026, 9, 10, 15, 0, tzinfo=timezone.utc)
    reports, _ = weather.run(now=now, do_resolve=False)
    assert wired["resolve"] == [] and wired["freeze"] == []
    assert reports[0]["status"] == "would-resolve"


# ── market creation ──────────────────────────────────────────────────────────

def test_build_buckets_centres_on_forecast():
    b = weather.build_buckets(18.7)                 # round→19, B=17
    assert [x["label"] for x in b] == [
        "16,9 °C arba žemesnė", "17,0 iki 18,9 °C",
        "19,0 iki 20,9 °C", "21,0 °C arba aukštesnė"]


def test_bucket_probabilities_sum_100_and_positive():
    b = weather.build_buckets(18.7)
    p = weather.bucket_probabilities(18.7, b, 2.0)
    assert sum(p) == 100
    assert all(1 <= x < 100 for x in p)             # app requires 0 < prob < 100
    assert p[1] + p[2] > p[0] + p[3]                # mass sits in the middle


def test_plan_skips_existing_and_reuses_city_image(monkeypatch):
    from datetime import timezone as _tz2
    monkeypatch.setattr(weather, "forecast_daily_max", lambda place, iso, tz=None: 18.0)
    monkeypatch.setattr(weather, "models_forecast_max", lambda city, iso: {})
    tz = weather._tz()
    now = datetime(2026, 9, 9, 20, 0, tzinfo=_tz2.utc)
    existing = [{"id": "v10", "category": "orai", "image_url": "vilnius.png",
                 "title": "Aukščiausia temperatūra Vilniuje rugsėjo 10 d.?",
                 "subtitle": "2026 m. rugsėjo 10 d."}]      # Vilnius 09-10 already exists
    specs = weather.plan_new_markets(existing, now, tz)     # horizon 2 → 09-10, 09-11
    made = {(s["city"], s["date"]) for s in specs}
    assert ("vilnius", "2026-09-10") not in made           # skipped (exists)
    assert ("kaunas", "2026-09-10") in made
    assert made >= {("vilnius", "2026-09-11"), ("kaunas", "2026-09-11")}
    vil = next(s for s in specs if s["city"] == "vilnius")
    assert vil["image_url"] == "vilnius.png"                # reused the city's image


def test_create_markets_calls_rpc_per_spec(monkeypatch):
    calls = []
    monkeypatch.setattr(weather.app_api, "create_market",
                        lambda spec: (calls.append(spec["title"]), (True, "new-id"))[1])
    monkeypatch.setattr(weather.notify, "send", lambda m: None)
    specs = [{"city": "kaunas", "date": "2026-09-11", "title": "T", "forecast_max": 18.0,
              "options": [{"label": "a", "probability": 50}, {"label": "b", "probability": 50}]}]
    reports = weather.create_markets(specs, alert=True, do_create=True)
    assert calls == ["T"] and reports[0]["status"] == "created"
    # dry run makes no RPC call
    calls.clear()
    reports = weather.create_markets(specs, alert=False, do_create=False)
    assert calls == [] and reports[0]["status"] == "would-create"


def test_eliminate_impossible_buckets(monkeypatch):
    market = {"id": "m1", "market_options": [], "title": "oras?"}
    buckets = [{"option_id": "b1", "label": "16,3 žemiau", "low": None, "high": 16.3},
               {"option_id": "b2", "label": "16,4-18", "low": 16.4, "high": 18.0},
               {"option_id": "b3", "label": "18,1-19", "low": 18.1, "high": 19.0},
               {"option_id": "b4", "label": "19,1+", "low": 19.1, "high": None}]
    calls = []
    monkeypatch.setattr(weather.app_api, "eliminate_options",
                        lambda mid, ids: (calls.append((mid, tuple(ids))), (True, "ok"))[1])
    monkeypatch.setattr(weather.notify, "send", lambda m: None)
    monkeypatch.setattr(weather.config, "WEATHER_ELIMINATE", True)
    st = {}
    assert weather._eliminate_impossible(market, "m1", buckets, 16.7, st, True, True)
    assert calls[-1] == ("m1", ("b1",)) and st["eliminated"] == ["b1"]
    assert weather._eliminate_impossible(market, "m1", buckets, 18.5, st, True, True)
    assert calls[-1] == ("m1", ("b2",)) and set(st["eliminated"]) == {"b1", "b2"}
    weather._eliminate_impossible(market, "m1", buckets, 25.0, st, True, True)
    assert "b4" not in st["eliminated"]                # top (unbounded) never eliminated
    assert not weather._eliminate_impossible(market, "m1", buckets, 25.0, st, True, True)  # idempotent


def test_city_calibration_shifts_and_tightens(monkeypatch):
    from arbus import weather

    monkeypatch.setattr(weather, "forecast_daily_max", lambda *a, **k: 12.0)
    monkeypatch.setattr(weather, "models_forecast_max", lambda *a, **k: {})   # Meteo LT only
    kl = weather.market_spec("klaipeda", "2026-10-25")
    ka = weather.market_spec("kaunas", "2026-10-25")
    p_kl = [o["probability"] for o in kl["options"]]
    p_ka = [o["probability"] for o in ka["options"]]
    # Klaipėda's σ 0.8: the open tails are cheaper than in Kaunas (σ 1.3)
    assert p_kl[0] + p_kl[3] < p_ka[0] + p_ka[3]
    # Kaunas runs ~0.5 °C below the forecast: the lower middle bucket is favoured
    assert p_ka[1] > p_ka[2]
    assert kl["closes_at"].startswith("2026-10-25T11:00")


# ── multi-model blend (Meteo LT + Open-Meteo models) ─────────────────────────

def test_parse_models_max_drops_partial_days(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_MODELS", ("ecmwf_ifs025", "gfs_seamless", "icon_seamless"))
    data = {"hourly": {"time": [f"2026-10-24T{h:02d}:00" for h in range(24)],
                       "temperature_2m_ecmwf_ifs025": [8.0] * 23 + [13.1],
                       "temperature_2m_gfs_seamless": [9.0] * 10 + [None] * 14,   # partial day
                       "temperature_2m_icon_seamless": [11.8] * 24}}
    assert weather.parse_models_max(data) == {"ecmwf_ifs025": 13.1, "icon_seamless": 11.8}
    assert weather.parse_models_max({}) == {}


def test_blend_falls_back_to_meteo_lt_without_models():
    bias, sigma = config.WEATHER_CITY_CALIBRATION["kaunas"]
    b = weather.blend_forecast("kaunas", 15.0, {})
    assert b["mu"] == pytest.approx(15.0 + bias) and b["sigma"] == sigma
    b1 = weather.blend_forecast("kaunas", 15.0, {"ecmwf_aifs025_single": 16.0})   # one model is not enough
    assert b1["mu"] == pytest.approx(15.0 + bias)
    assert weather.blend_forecast("kaunas", None, {}) is None


def test_blend_is_weighted_models_plus_bias_and_ignores_meteo_lt():
    models = {"ecmwf_aifs025_single": 12.0, "icon_seamless": 11.0, "ukmo_seamless": 13.0}
    bias = config.WEATHER_BLEND_CALIBRATION["vilnius"][0]
    with_lt = weather.blend_forecast("vilnius", 8.0, models)
    no_lt = weather.blend_forecast("vilnius", None, models)
    assert with_lt["mu"] == pytest.approx(12.0 + bias)        # 0.5·12 + 0.25·11 + 0.25·13
    assert with_lt["mu"] == no_lt["mu"] and with_lt["sigma"] == no_lt["sigma"]
    assert with_lt["divergence"] == pytest.approx(12.0 + bias - 8.0)
    assert no_lt["divergence"] is None


def test_blend_reweights_when_a_model_is_missing():
    b = weather.blend_forecast("kaunas", None, {"icon_seamless": 10.0, "ukmo_seamless": 12.0})
    assert b["mu"] == pytest.approx(11.0 + config.WEATHER_BLEND_CALIBRATION["kaunas"][0])


def test_blend_sigma_widens_with_spread_and_lead(monkeypatch):
    monkeypatch.setattr(config, "WEATHER_MODEL_BLEND", True)
    agree = {"ecmwf_aifs025_single": 12.0, "icon_seamless": 11.9, "ukmo_seamless": 12.1}
    split = {"ecmwf_aifs025_single": 12.0, "icon_seamless": 9.9, "ukmo_seamless": 14.1}
    assert weather.blend_forecast("klaipeda", None, split)["sigma"] > \
        weather.blend_forecast("klaipeda", None, agree)["sigma"]
    near = weather.blend_forecast("klaipeda", None, agree, lead_days=1.0)["sigma"]
    far = weather.blend_forecast("klaipeda", None, agree, lead_days=3.0)["sigma"]
    assert near < far
    monkeypatch.setattr(config, "WEATHER_MODEL_BLEND", False)    # kill switch
    assert weather.blend_forecast("klaipeda", 12.0, split)["sigma"] == \
        config.WEATHER_CITY_CALIBRATION["klaipeda"][1]


def test_lead_days_to_the_afternoon_peak():
    from datetime import timezone as _utc
    tz = weather._tz()
    now = datetime(2026, 10, 27, 16, 0, tzinfo=_utc.utc)       # 18:00 Vilnius
    assert weather.lead_days("2026-10-29", now, tz) == pytest.approx(1 + 19 / 24)
    assert weather.lead_days("2026-10-20", now, tz) == 0.0


def test_vilnius_oct27_centred_on_the_models(monkeypatch):
    # The exploited market: Meteo LT pulled the old centre to 9.5 °C while the
    # models said ~10.8 and the real max was 10.8 °C.
    from datetime import timezone as _utc
    monkeypatch.setattr(weather, "forecast_daily_max", lambda *a, **k: 9.0)
    monkeypatch.setattr(weather, "models_forecast_max", lambda *a, **k: {
        "ecmwf_aifs025_single": 10.3, "icon_seamless": 10.0, "ukmo_seamless": 10.4})
    now = datetime(2026, 10, 25, 16, 0, tzinfo=_utc.utc)
    spec = weather.market_spec("vilnius", "2026-10-27", now=now)
    p = [o["probability"] for o in spec["options"]]
    assert sum(p) == 100 and all(1 <= x < 100 for x in p)
    win = weather.bucket_for_temp(weather.buckets_from_market(
        {"market_options": [{"id": str(i), "label": o["label"]}
                            for i, o in enumerate(spec["options"])]}), 10.8)
    assert p[int(win["option_id"])] >= 40                     # the real max is the favourite
    assert spec["forecast_max"] == 9.0 and spec["forecast_mu"] == pytest.approx(10.87, abs=0.01)
    assert set(spec["forecast_models"]) == set(config.WEATHER_MODEL_WEIGHTS)
    assert spec["forecast_divergence"] == pytest.approx(1.87, abs=0.01)


def test_market_spec_skips_when_no_forecast_at_all(monkeypatch):
    monkeypatch.setattr(weather, "forecast_daily_max", lambda *a, **k: None)
    monkeypatch.setattr(weather, "models_forecast_max", lambda *a, **k: {})
    assert weather.market_spec("vilnius", "2026-10-24") is None


def test_created_message_warns_on_divergence():
    spec = {"title": "T", "forecast_max": 11.0, "forecast_mu": 12.0, "forecast_sigma": 1.4,
            "forecast_models": {"ecmwf_ifs025": 13.1, "gfs_seamless": 12.9},
            "forecast_divergence": 1.8,
            "options": [{"label": "a", "probability": 50}, {"label": "b", "probability": 50}]}
    msg = weather._created_message(spec, "id")
    assert "Meteo LT 11,0" in msg and "ecmwf 13,1" in msg and "⚠️" in msg
    spec["forecast_divergence"] = 0.4
    assert "⚠️" not in weather._created_message(spec, "id")
