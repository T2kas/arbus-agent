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
