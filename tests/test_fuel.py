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
