import pytest
"""Daily fuel-price markets: detection, bucket math, the exact-date LEA match,
and the resolver (which must never settle on the wrong day's price)."""

from datetime import datetime, timezone

from arbus import fuel


def _daily_market(fuel_word="benzino A95", iso_lt="2026 m. rugsėjo 23 d.",
                  resolved=False, status="open"):
    labels = ["1,919 €/l arba mažiau", "Nuo 1,920 iki 1,930 €/l",
              "Nuo 1,931 iki 1,941 €/l", "Nuo 1,942 iki 1,959 €/l",
              "1,960 €/l arba daugiau"]
    return {"id": "m1", "status": status,
            "winning_option_id": "o2" if resolved else None,
            "title": f"Vidutinė {fuel_word} kaina Lietuvoje {iso_lt}?",
            "rules": f"Rinka bus išspręsta pagal LEA paskelbtą {iso_lt} vidutinę kainą.",
            "market_options": [{"id": f"o{i}", "label": l} for i, l in enumerate(labels)]}


# ── detection ────────────────────────────────────────────────────────────────

def test_detects_daily_petrol_and_diesel():
    assert fuel.fuel_target(_daily_market("benzino A95")) == ("benzinas", "2026-09-23")
    assert fuel.fuel_target(_daily_market("dyzelino")) == ("dyzelinas", "2026-09-23")


def test_skips_highest_ladder_and_threshold_markets():
    highest = {"title": "Aukščiausia vidutinė dyzelino kaina Lietuvoje 2026 m.",
               "rules": "aukščiausią vienos dienos kainą nuo 2026 m. rugsėjo 23 d.",
               "market_options": [{"id": "a", "label": "2,299 €/l arba mažiau"},
                                  {"id": "b", "label": "Nuo 2,300 iki 2,399 €/l"}]}
    assert fuel.fuel_target(highest) is None
    ladder = {"title": "Vidutinė benzino A95 kaina pasieks 2 €/l iki...?",
              "rules": "Taip laimi jeigu bent vienos dienos kaina 2,000 €/l",
              "market_options": [{"id": "a", "label": "2026 m. rugsėjo 30 d."},
                                 {"id": "b", "label": "2026 m. spalio 31 d."}]}
    assert fuel.fuel_target(ladder) is None
    threshold = {"title": "Ar vidutinė dyzelino kaina iki spalio pasieks 2,10 €/l?",
                 "rules": "bent kartą 2,100 €/l", "market_options":
                 [{"id": "a", "label": "Taip"}, {"id": "b", "label": "Ne"}]}
    assert fuel.fuel_target(threshold) is None


# ── bucket math ──────────────────────────────────────────────────────────────

def test_parse_price_bucket():
    assert fuel.parse_price_bucket("1,919 €/l arba mažiau") == (None, 1.919)
    assert fuel.parse_price_bucket("Nuo 1,920 iki 1,930 €/l") == (1.920, 1.930)
    assert fuel.parse_price_bucket("1,960 €/l arba daugiau") == (1.960, None)


def test_bucket_for_price_maps_to_the_right_band():
    bks = fuel.buckets_from_market(_daily_market())
    assert fuel.bucket_for_price(bks, 1.935)["label"].startswith("Nuo 1,931")
    assert fuel.bucket_for_price(bks, 1.900)["label"].endswith("arba mažiau")
    assert fuel.bucket_for_price(bks, 2.000)["label"].endswith("arba daugiau")


def test_build_buckets_center_holds_forecast_and_sums_100():
    bk = fuel.build_buckets(1.940)
    pcts = fuel.bucket_probabilities(1.940, bk, 0.012)
    assert sum(pcts) == 100 and all(p >= 1 for p in pcts)
    # the middle bucket must contain the forecast
    mid = bk[2]
    assert mid["lo"] <= 1.940 <= mid["hi"]


# ── the exact-date LEA match (the money-safety) ──────────────────────────────

def test_dated_bulletin_url_carries_the_date():
    # the date is IN the URL, so a different day's price can never be returned
    assert fuel._kdk_url("2026-09-23") == "https://www.ena.lt/Naujiena/kdk-20260923/"
    assert fuel._kdk_url("2026-09-23", "ndk") == "https://www.ena.lt/Naujiena/ndk-20260923/"
    hits = fuel._KDK_SLUG_RE.findall(
        "<loc>https://www.ena.lt/Naujiena/kdk-20260922/</loc>")
    assert hits and hits[0][1:] == ("2026", "09", "22")


def test_single_date_from_title():
    assert fuel._single_date("… Lietuvoje 2026 m. rugsėjo 23 d.?") == "2026-09-23"
    assert fuel._single_date("2026-09-24 something") == "2026-09-24"


# ── resolver ─────────────────────────────────────────────────────────────────

def _wire(monkeypatch):
    calls = {"resolve": [], "tg": []}
    monkeypatch.setattr(fuel.app_api, "resolve_market",
                        lambda mid, oid: (calls["resolve"].append((mid, oid)), (True, "ok"))[1])
    monkeypatch.setattr(fuel.notify, "send", lambda m: calls["tg"].append(m))
    return calls


def test_resolves_to_the_bucket_containing_the_published_price(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(fuel, "lea_price_for_date",
                        lambda iso, f, **k: (1.935, "https://ena.lt/x"))
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    reports, changed = fuel.resolve_markets([_daily_market()], now, fuel._tz(), {},
                                            alert=True, do_resolve=True)
    # 1.935 lands in the "Nuo 1,931 iki 1,941" bucket = option o2
    assert calls["resolve"] == [("m1", "o2")]
    assert reports[0]["status"] == "resolved"


def test_waits_when_the_price_is_not_published_yet(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(fuel, "lea_price_for_date", lambda iso, f, **k: (None, ""))
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    reports, _ = fuel.resolve_markets([_daily_market()], now, fuel._tz(), {},
                                      alert=True, do_resolve=True)
    assert calls["resolve"] == []                         # no price → no resolution


def test_does_not_resolve_a_future_day(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(fuel, "lea_price_for_date",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not fetch")))
    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)   # market is for the 23rd
    reports, _ = fuel.resolve_markets([_daily_market()], now, fuel._tz(), {},
                                      alert=True, do_resolve=True)
    assert calls["resolve"] == []


def test_already_resolved_market_is_skipped(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(fuel, "lea_price_for_date", lambda *a, **k: (1.935, "u"))
    now = datetime(2026, 9, 24, 12, tzinfo=timezone.utc)
    reports, _ = fuel.resolve_markets([_daily_market(resolved=True)], now, fuel._tz(), {},
                                      alert=True, do_resolve=True)
    assert calls["resolve"] == []


def test_new_market_reuses_previous_days_image(monkeypatch):
    """New daily markets take the image from the latest existing daily market of
    that fuel, so changing yesterday's image propagates."""
    from datetime import datetime, timezone
    monkeypatch.setattr(fuel, "latest_lea_price", lambda f, **k: (2.0, "u"))
    existing = _daily_market("benzino A95", "2026 m. rugsėjo 22 d.")
    existing["image_url"] = "https://img/yesterday.jpg"
    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    specs = fuel.plan_new_markets([existing], now, fuel._tz())
    petrol = [s for s in specs if s["fuel"] == "benzinas"]
    assert petrol and petrol[0]["image_url"] == "https://img/yesterday.jpg"
    # closes late the evening BEFORE the market's date (Sept 23): LEA's live
    # map shows the morning submissions
    assert petrol[0]["closes_at"].startswith("2026-09-22T23:30")


def test_new_market_takes_image_from_resolved_fuel_market(monkeypatch):
    """The open rows hold no fuel market once yesterday's resolved: the picture
    comes from the newest resolved one, else the long-used default."""
    from datetime import datetime, timezone
    monkeypatch.setattr(fuel, "latest_lea_price", lambda f, **k: (2.0, "u"))
    old = _daily_market("benzino A95", "2026 m. rugsėjo 22 d.", resolved=True)
    old["image_url"] = "https://img/resolved.jpg"
    now = datetime(2026, 9, 22, 12, tzinfo=timezone.utc)
    specs = fuel.plan_new_markets([], now, fuel._tz(), image_rows=[old])
    by_fuel = {s["fuel"]: s["image_url"] for s in specs}
    assert by_fuel["benzinas"] == "https://img/resolved.jpg"
    assert by_fuel["dyzelinas"] == fuel.DEFAULT_IMAGES["dyzelinas"]


def test_publication_days_skip_weekends_and_holidays():
    from datetime import date
    assert fuel.is_publication_day(date(2026, 9, 25))        # Friday
    assert not fuel.is_publication_day(date(2026, 9, 26))    # Saturday
    assert not fuel.is_publication_day(date(2026, 9, 27))    # Sunday
    assert not fuel.is_publication_day(date(2026, 12, 24))   # Christmas Eve (Thu)
    assert fuel._easter(2026) == date(2026, 4, 5)
    assert not fuel.is_publication_day(date(2026, 4, 6))     # Easter Monday
    # Friday's run creates MONDAY's market, not Saturday's
    assert fuel.next_publication_days(date(2026, 9, 25), 1) == [date(2026, 9, 28)]


def test_friday_creation_targets_monday(monkeypatch):
    monkeypatch.setattr(fuel, "latest_lea_price", lambda f, **k: (2.0, "u"))
    now = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)      # Fri 13:00 Vilnius
    specs = fuel.plan_new_markets([], now, fuel._tz())
    assert {s["date"] for s in specs} == {"2026-09-28"}


def test_weekend_market_is_flagged_not_guessed(monkeypatch):
    calls = _wire(monkeypatch)
    monkeypatch.setattr(fuel, "lea_price_for_date",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no fetch")))
    now = datetime(2026, 9, 28, 12, tzinfo=timezone.utc)
    m = _daily_market("dyzelino", "2026 m. rugsėjo 26 d.")   # Saturday
    state = {}
    reports, changed = fuel.resolve_markets([m], now, fuel._tz(), state,
                                            alert=True, do_resolve=True)
    assert calls["resolve"] == [] and changed
    assert reports[0]["status"] == "error" and len(calls["tg"]) == 1
    # alerted once only
    fuel.resolve_markets([m], now, fuel._tz(), state, alert=True, do_resolve=True)
    assert len(calls["tg"]) == 1


def test_tuesday_jump_moves_the_book_up():
    from zoneinfo import ZoneInfo

    from arbus import fuel

    tz = ZoneInfo("Europe/Vilnius")
    tue = fuel.market_spec("benzinas", "2026-10-27", "", 1.950, tz)    # Tuesday
    thu = fuel.market_spec("benzinas", "2026-10-29", "", 1.950, tz)    # Thursday
    # Tuesday's middle bucket sits ~3.6 ct above the last published price
    lo_mid = fuel.parse_price_bucket(tue["options"][2]["label"])[0]
    assert lo_mid >= 1.980
    assert "Antradieniais" in tue["context"]
    # Thursday drifts down and is tight (σ 1 ct): the middle bucket is the favourite
    p = [o["probability"] for o in thu["options"]]
    assert p[2] == max(p) and fuel.parse_price_bucket(thu["options"][2]["label"])[1] < 1.950


def test_drift_adds_days_without_a_bulletin():
    from datetime import date
    from arbus import config
    m = config.FUEL_WEEKDAY_MODEL
    tue = date(2026, 11, 3)
    # normal Tuesday: base = Monday's bulletin
    assert fuel.drift_and_sigma(tue, date(2026, 11, 2)) == pytest.approx(m[1])
    # Monday 2 Nov 2026 was a holiday → base is Friday: Monday's drift is added
    d, s = fuel.drift_and_sigma(tue, date(2026, 10, 30))
    assert d == pytest.approx(m[0][0] + m[1][0])
    assert s == pytest.approx((m[0][1] ** 2 + m[1][1] ** 2) ** 0.5)
    # Monday from Friday is just Monday's coefficients (they already cover the weekend)
    assert fuel.drift_and_sigma(date(2026, 11, 9), date(2026, 11, 6)) == pytest.approx(m[0])


def _hist(changes_by_week, start="2026-08-03"):
    """A bulletin history: each week Mon..Fri gets the given changes (€/l) from
    the previous bulletin, both fuels the same."""
    from datetime import date, timedelta
    h, p, d = {}, 2.0, date.fromisoformat(start)
    for week in changes_by_week:
        for i, ch in enumerate(week):
            p = round(p + ch, 4)
            h[(d + timedelta(days=i)).isoformat()] = {"benzinas": p, "dyzelinas": p + 0.3}
        d += timedelta(days=7)
    return h


def test_recent_weekday_model_follows_the_last_weeks():
    from datetime import date
    from arbus import config
    week = [-0.01, +0.04, -0.02, -0.01, +0.005]
    h = _hist([week] * 12)
    m = fuel.recent_weekday_model(h, date(2026, 10, 27))
    assert m[1][0] == pytest.approx(0.04)          # Tuesday jump re-fitted to +4 ct
    assert m[4][0] == pytest.approx(0.005)         # Friday now rises
    assert m[1][1] == config.FUEL_SIGMA_FLOOR      # no spread -> the σ floor
    # too little history -> the static table
    assert fuel.recent_weekday_model(_hist([week] * 2), date(2026, 8, 20)) == config.FUEL_WEEKDAY_MODEL


def test_recent_model_skips_changes_across_a_holiday():
    from datetime import date
    h = _hist([[-0.01, +0.04, -0.02, -0.01, 0.0]] * 12)
    h.pop("2026-10-19")                             # a Monday without a bulletin
    h["2026-10-20"] = {"benzinas": 1.0, "dyzelinas": 1.3}   # wild Fri->Tue gap, then back
    h["2026-10-21"] = dict(h["2026-10-20"])
    m = fuel.recent_weekday_model(h, date(2026, 10, 27))
    assert m[1][0] == pytest.approx(0.04)          # the Fri->Tue pair is not a "Tuesday"


def test_update_history_fetches_only_missing_days():
    from datetime import date
    calls = []

    def fetch(iso):
        calls.append(iso)
        return {} if iso == "2026-11-03" else {"benzinas": 1.9}
    state = {}
    fuel.update_history(state, date(2026, 11, 3), weeks=1, fetch=fetch)
    assert "2026-11-02" not in calls                # Vėlinės: no bulletin, not fetched
    assert "2026-11-03" not in state["_fuel_hist"]  # today's not out yet: retried later
    n = len(calls)
    fuel.update_history(state, date(2026, 11, 3), weeks=1, fetch=fetch)
    assert calls[n:] == ["2026-11-03"]


def test_plan_uses_the_recent_model(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(fuel, "latest_lea_price",
                        lambda f, **k: (2.0, "https://www.ena.lt/Naujiena/kdk-20261026/"))
    monkeypatch.setattr(fuel, "update_history",
                        lambda state, today, **k: _hist([[-0.01, +0.03, -0.02, -0.01, 0.0]] * 12))
    now = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)          # Monday -> Tuesday's market
    spec = [s for s in fuel.plan_new_markets([], now, fuel._tz(), state={})
            if s["fuel"] == "benzinas"][0]
    lo, hi = fuel.parse_price_bucket(spec["options"][2]["label"])
    assert lo <= 2.03 <= hi                         # +3 ct, not the static +6.25 ct


def test_no_market_opened_after_its_evening_close(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(fuel, "latest_lea_price", lambda f, **k: (2.0, "u"))
    late = datetime(2026, 9, 22, 20, 0, tzinfo=timezone.utc)       # 23:00 Vilnius
    assert fuel.plan_new_markets([], late, fuel._tz()) == []


def test_monday_market_closes_on_friday_and_holiday_gap_is_skipped(monkeypatch):
    from datetime import datetime, timezone
    monkeypatch.setattr(fuel, "latest_lea_price",
                        lambda f, **k: (2.0, "https://www.ena.lt/Naujiena/kdk-20260925/"))
    fri = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)
    specs = fuel.plan_new_markets([], fri, fuel._tz())
    assert specs and all(s["closes_at"].startswith("2026-09-25T23:30") for s in specs)
    # Monday 2 Nov 2026 (Vėlinės): Tuesday's market would start from Friday's price
    monkeypatch.setattr(fuel, "latest_lea_price",
                        lambda f, **k: (2.0, "https://www.ena.lt/Naujiena/kdk-20261030/"))
    hol = datetime(2026, 11, 2, 11, tzinfo=timezone.utc)
    assert fuel.plan_new_markets([], hol, fuel._tz()) == []
