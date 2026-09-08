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
    monkeypatch.setattr(weather.app_api, "resolve_market",
                        lambda mid, oid: (calls["resolve"].append((mid, oid)), (True, "ok"))[1])
    monkeypatch.setattr(weather.app_api, "freeze_market",
                        lambda mid: (calls["freeze"].append(mid), (True, "ok"))[1])
    monkeypatch.setattr(weather.notify, "send", lambda msg: calls["telegram"].append(msg))
    return calls


def _obs(*pairs):
    return {"observations": [{"observationTimeUtc": t, "airTemperature": v}
                             for t, v in pairs]}


def test_early_max_locks_top_bucket_and_freezes_first(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    # A 21.5 °C reading appears midday — the top bucket is now final.
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 12:00:00", 21.5)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day",
                        lambda s, iso: ([{"observationTimeUtc": "2026-09-10 12:00:00",
                                          "airTemperature": 21.5}], ["url"], "sum"))
    now = datetime(2026, 9, 10, 12, 5, tzinfo=timezone.utc)
    reports, err = weather.run(now=now)
    assert err == ""
    assert wired["freeze"] == ["m1"]                     # closed trading first
    assert wired["resolve"] == [("m1", "b4")]            # then top bucket wins
    assert reports[0]["status"] == "resolved" and reports[0]["via"] == "early_max"


def test_end_of_day_resolves_the_matching_bucket_without_freezing(wired, monkeypatch):
    # By end of day the app has already closed trading (status 'closed'), so the
    # bot neither re-closes nor freezes during resolve.
    monkeypatch.setattr(weather.app_api, "markets",
                        lambda *a, **k: ([_market(_OPTS, status="closed")], ""))
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 12:00:00", 18.9)), "{raw}"))
    # Full day incl. the 23:00 LT (=20:00 UTC) reading; max 18.9 → bucket b2.
    day = [{"observationTimeUtc": "2026-09-10 12:00:00", "airTemperature": 18.9},
           {"observationTimeUtc": "2026-09-10 20:00:00", "airTemperature": 14.0}]
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: (day, ["u1", "u2"], "sum"))
    now = datetime(2026, 9, 10, 21, 30, tzinfo=timezone.utc)   # already 09-11 in Vilnius
    reports, _ = weather.run(now=now)
    assert wired["freeze"] == []                          # app closed it at 16:00, not us
    assert wired["resolve"] == [("m1", "b2")]
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
    reports, _ = weather.run(now=datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc))
    assert reports == [] and wired["resolve"] == []


def test_no_double_resolve_across_runs(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 12:00:00", 21.5)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day",
                        lambda s, iso: ([{"observationTimeUtc": "2026-09-10 12:00:00",
                                          "airTemperature": 21.5}], ["url"], "sum"))
    now = datetime(2026, 9, 10, 12, 5, tzinfo=timezone.utc)
    weather.run(now=now)
    weather.run(now=now)                                 # second pass sees state.resolved
    assert wired["resolve"] == [("m1", "b4")]            # resolved exactly once


def test_closes_todays_market_at_14_but_does_not_resolve(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 12:00:00", 18.9)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: ([], [], "x"))
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)   # 15:00 Vilnius (>14)
    reports, _ = weather.run(now=now)
    assert wired["freeze"] == ["m1"]           # trading closed
    assert wired["resolve"] == []              # but NOT resolved (max<21, day not over)
    assert reports[0]["status"] == "watch" and reports[0]["closed"] is True


def test_does_not_close_a_future_day_market(wired, monkeypatch):
    fut = _market(_OPTS, title="Aukščiausia temperatūra Kaune rugsėjo 11 d.?",
                  subtitle="2026 m. rugsėjo 11 d.")
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([fut], ""))
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 12:00:00", 18.9)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: ([], [], "x"))
    now = datetime(2026, 9, 10, 12, 0, tzinfo=timezone.utc)   # 15:00 Vilnius, but market is 09-11
    reports, _ = weather.run(now=now)
    assert wired["freeze"] == [] and wired["resolve"] == []   # left in peace


def test_does_not_close_before_14(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 08:00:00", 15.0)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day", lambda s, iso: ([], [], "x"))
    now = datetime(2026, 9, 10, 9, 0, tzinfo=timezone.utc)    # 12:00 Vilnius (<14)
    reports, _ = weather.run(now=now)
    assert wired["freeze"] == []


def test_no_resolve_flag_only_monitors(wired, monkeypatch):
    monkeypatch.setattr(weather.app_api, "markets", lambda *a, **k: ([_market(_OPTS)], ""))
    monkeypatch.setattr(weather, "fetch",
                        lambda url, timeout=20: (_obs(("2026-09-10 12:00:00", 21.5)), "{raw}"))
    monkeypatch.setattr(weather, "fetch_lt_day",
                        lambda s, iso: ([{"observationTimeUtc": "2026-09-10 12:00:00",
                                          "airTemperature": 21.5}], ["url"], "sum"))
    now = datetime(2026, 9, 10, 12, 5, tzinfo=timezone.utc)
    reports, _ = weather.run(now=now, do_resolve=False)
    assert wired["resolve"] == [] and wired["freeze"] == []
    assert reports[0]["status"] == "would-resolve"
