"""LKC weekly most-watched-film resolver: date parsing, Weekly-vs-Weekend link,
and reading the max of the 'Žiūrovų sk. (ADM)' column (not revenue rank #1)."""

import io

import pytest

from arbus import resolvers

RULES = ("Rinka bus išspręsta pagal filmą, surinkusį daugiausia žiūrovų Lietuvos "
         "kino teatruose nuo 2026 m. rugsėjo 4 d. iki rugsėjo 10 d. imtinai. "
         "Naudojamas „Žiūrovų sk. (ADM)“ stulpelis.")


def test_cinema_target_parses_the_week():
    assert resolvers._cinema_target("Kuris filmas žiūrimiausias?", RULES) == (
        "2026-09-04", "2026-09-10")


def test_cinema_target_ignores_non_cinema_markets():
    assert resolvers._cinema_target("Kas laimės Seimo rinkimus 2026?", "politika") is None


def test_weekly_link_is_picked_over_weekend():
    html = ('<a href="/docs/rep/Savaitgalio (Weekend) TOP 2026.09.04-09.06.xlsx">a</a>'
            '<a href="/docs/rep/Savaitės (Weekly) TOP 2026.09.04-2026.09.10.xlsx">b</a>')
    url = resolvers._lkc_weekly_url("2026-09-04", "2026-09-10", html)
    assert "Weekly" in url and "Weekend" not in url and url.endswith(".xlsx")


def _xlsx(rows):
    openpyxl = pytest.importorskip("openpyxl")
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_parse_takes_max_adm_not_revenue_rank_and_skips_total():
    data = _xlsx([
        ["Rugsėjo 4-10 top", None, None, None, None, None],           # title row
        ["#", "LW", "Filmas (Movie)", "Pajamos (GBO)", "Žiūrovų sk. (ADM)", "Seansų"],
        [1, 2, "RevKing", 99999, 3000, 150],       # rank #1 by revenue, low ADM
        [2, 1, "CrowdFave", 50000, 9000, 255],     # top by ADM
        [3, "N", "Third", 20000, 4000, 100],
        ["", "", "Total (35)", "", 47722, ""],     # totals row → must be skipped
    ])
    top = resolvers.parse_cinema_xlsx(data)
    assert top[0] == ("CrowdFave", 9000.0)         # ADM winner, not revenue rank #1
    assert all(name != "Total (35)" for name, _ in top)
    assert [n for n, _ in top][:3] == ["CrowdFave", "Third", "RevKing"]


def test_cinema_fact_empty_before_week_ends(monkeypatch):
    # A future week → no report yet → no fact (market stays unresolved).
    fact = resolvers.cinema_fact("Kuris filmas žiūrimiausias?",
                                 RULES.replace("2026 m. rugsėjo", "2099 m. rugsėjo"))
    assert fact == ""


def test_monthly_and_yearly_markets_are_not_weekly():
    assert resolvers._cinema_target("Kuris filmas žiūrimiausias rugsėjį?",
                                    "kino teatruose 2026 m. rugsėjo 1 d. iki rugsėjo 30 d. ADM") is None
    assert resolvers._cinema_target("Kuris filmas žiūrimiausias 2026 metais?",
                                    "kino teatruose per 2026 metus, ADM") is None


def test_period_detection_weekly_monthly_yearly():
    assert resolvers._cinema_period("Kuris filmas žiūrimiausias?", RULES)[0] == "weekly"
    assert resolvers._cinema_period(
        "Kuris filmas žiūrimiausias rugsėjį?",
        "kino teatruose 2026 m. rugsėjo mėnesį pagal žiūrovus") == ("monthly", 2026, 9)
    assert resolvers._cinema_period(
        "Kuris lietuviškas filmas žiūrimiausias 2026 metais?",
        "kino teatruose per 2026 metus pagal žiūrovus") == ("yearly", 2026)
    assert resolvers._cinema_period("Kas laimės rinkimus?", "politika 2026 m.") is None


def test_lt_film_market_flag():
    assert resolvers._is_lt_film_market("Kuris lietuviškas filmas...")
    assert not resolvers._is_lt_film_market("Kuris filmas...")


def test_monthly_and_yearly_url_selection():
    html = ('<a href="/docs/x/2026 Liepa.xlsx">m</a>'
            '<a href="/docs/x/Savaitės (Weekly) TOP 2026.07.01-2026.07.07.xlsx">w</a>')
    assert "2026%20Liepa" in resolvers._lkc_monthly_url(2026, 7, html)
    assert resolvers._lkc_monthly_url(2026, 9, html) == ""      # September file not present
    yhtml = '<a href="/docs/x/2025 TOP su menesiais_lkc_suvestine.xlsx">y</a>'
    assert "2025%20TOP" in resolvers._lkc_yearly_url(2025, yhtml)


def test_read_film_rows_monthly_country_and_viewers():
    data = _xlsx([
        ["Lietuvos kino teatruose TOP (2026)", None, None, None, None, None],
        ["Eil. Nr.", "Filmo pavadinimas", "Filmo pavadinimas orginalo kalba",
         "Kilmės šalis", "Pajamos", "Žiūrovų skaičius"],
        [1, "Šeima į gamtą", "Family", "LT", 973310, 121497],
        [2, "Avataras", "Avatar", "US", 999999, 102187],       # more revenue, fewer viewers
        ["", "Iš viso", "", "", None, 223684],                 # totals row
    ])
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data))
    rows = resolvers._read_film_rows(wb.worksheets[0])
    assert [r[0] for r in rows] == ["Šeima į gamtą", "Avataras"]        # total skipped
    assert dict((n, v) for n, v, c in rows)["Šeima į gamtą"] == 121497  # viewers, not revenue
    assert dict((n, c) for n, v, c in rows)["Šeima į gamtą"] == "LT"    # country captured


# ── film → option matching and proactive resolve ─────────────────────────────

from datetime import datetime, timezone                              # noqa: E402

from arbus import weather                                            # noqa: E402


def _opts(*labels):
    return [{"id": f"o{i}", "label": l} for i, l in enumerate(labels)]


def test_match_named_option():
    o, kind = weather.match_cinema_option(
        "Odisėja (Odyssey, The)", _opts("Odisėja", "Žmogus-voras", "Kitas filmas"))
    assert kind == "named" and o["label"] == "Odisėja"


def test_match_falls_back_to_other():
    o, kind = weather.match_cinema_option(
        "Visai naujas (Whatever)", _opts("Odisėja", "Žmogus-voras", "Kitas filmas"))
    assert kind == "other" and o["label"] == "Kitas filmas"


def test_match_no_match_without_other():
    o, kind = weather.match_cinema_option("Nežinomas", _opts("Odisėja", "Žmogus-voras"))
    assert o is None and kind == "no_match"


_CINEMA_RULES = "kino teatruose nuo 2026 m. rugsėjo 4 d. iki rugsėjo 10 d. ADM"


def test_resolve_cinema_resolves_the_named_winner(monkeypatch):
    market = {"id": "m1", "status": "closed", "winning_option_id": None,
              "title": "Kuris filmas bus žiūrimiausias Lietuvoje?", "rules": _CINEMA_RULES,
              "market_options": _opts("Odisėja", "Žmogus-voras", "Kitas filmas")}
    monkeypatch.setattr(weather.config, "ARBUS_WRITE_KEY", "svc")
    monkeypatch.setattr(weather.resolvers, "cinema_top", lambda q, r: {
        "desc": "2026-09-04–10", "url": "http://x.xlsx", "lt_only": False,
        "top": [("Odisėja (Odyssey, The)", 8971.0), ("Žmogus-voras", 6015.0)]})
    calls = []
    monkeypatch.setattr(weather.app_api, "resolve_market",
                        lambda mid, oid: (calls.append((mid, oid)), (True, "ok"))[1])
    monkeypatch.setattr(weather.notify, "send", lambda m: None)
    reports, changed = weather._resolve_cinema(
        [market], datetime.now(timezone.utc), weather._tz(), {}, alert=True, do_resolve=True)
    assert calls == [("m1", "o0")] and reports[0]["status"] == "resolved" and changed


def test_resolve_cinema_alerts_on_a_tie(monkeypatch):
    market = {"id": "m1", "status": "closed", "winning_option_id": None,
              "title": "Kuris filmas žiūrimiausias?", "rules": _CINEMA_RULES,
              "market_options": _opts("Odisėja", "Žmogus-voras", "Kitas filmas")}
    monkeypatch.setattr(weather.config, "ARBUS_WRITE_KEY", "svc")
    monkeypatch.setattr(weather.resolvers, "cinema_top", lambda q, r: {
        "desc": "2026-09-04–10", "url": "u", "lt_only": False,
        "top": [("Odisėja", 5000.0), ("Žmogus-voras", 5000.0)]})
    calls, sent = [], []
    monkeypatch.setattr(weather.app_api, "resolve_market",
                        lambda mid, oid: (calls.append(1), (True, "ok"))[1])
    monkeypatch.setattr(weather.notify, "send", lambda m: sent.append(m))
    reports, _ = weather._resolve_cinema(
        [market], datetime.now(timezone.utc), weather._tz(), {}, alert=True, do_resolve=True)
    assert calls == [] and reports[0]["reason"] == "cinema tie" and sent
