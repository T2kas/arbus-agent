"""LKL / TOPLYGA game-market bot: parsing, pricing, dedupe (offline)."""

from datetime import datetime, timezone

from arbus import games

LKL_SCHEDULE = """
<div class="schedule-holder">
<div class="font-semibold text-2xl mt-8 mb-2"> 2026 m. spalio 10 d. (šeštadienis) </div>
<div class="result-item"> <div class="sm:flex"> <div class="location sm:text-left sm:w-1/4">
 <div class="text-lg font-bold"> 16:30 </div> <div> Arena Vilnius , Vilnius </div> </div>
 <div class="battle-row"> <a href="https://lkl.lt/komandos/rytas"><strong>RYT</strong>
 <img src="/media/teams/svg/a.svg" alt="Rytas"> </a> <span class="time">
 <a href="https://lkl.lt/rungtynes/11595">VS</a> </span>
 <a href="https://lkl.lt/komandos/neptunas"> <img src="/media/teams/svg/b.svg" alt="Neptūnas"> </a></div></div></div>
<div class="result-item"> <div class="location"> <div class="text-lg font-bold"> 19:00 </div>
 <div> Kalnapilio arena , Panevėžys </div> </div>
 <img src="/x.svg" alt="Lietkabelis"> <a href="https://lkl.lt/rungtynes/11590">88 : 70</a>
 <img src="/y.svg" alt="Hipocredit"> </div>
<div class="font-semibold text-2xl mt-8 mb-2"> 2026 m. spalio 11 d. (sekmadienis) </div>
<div class="result-item"> <div class="location"> <div class="text-lg font-bold"> 17:00 </div>
 <div> Kėdainių arena , Kėdainiai </div> </div>
 <img src="/n.svg" alt="Nevėžis-Paskolų klubas"> <a href="https://lkl.lt/rungtynes/11600">VS</a>
 <img src="/z.svg" alt="Žalgiris"> </div>
</div>
"""

LKL_TABLE = """
<table><tr><td class="team"><a href="x" class="team-title"> Žalgiris </a></td>
<td><span class="num md">4</span></td><td><span class="num md success">4</span></td>
<td><span class="num md danger">0</span></td></tr>
<tr><td><a href="x" class="team-title"> Rytas </a></td><td><span class="num md success">0</span></td>
<td><span class="num md danger">1</span></td></tr>
<tr><td><a href="x" class="team-title"> Neptūnas </a></td><td><span class="num md success">2</span></td>
<td><span class="num md danger">2</span></td></tr></table>
<table><tr><td><a href="x" class="team-title"> Žalgiris </a></td><td><span class="num md success">2</span></td>
<td><span class="num md danger">0</span></td></tr></table>
"""

TOP_SCHEDULE = """
<table><tr><th class="tal date2">22 turas</th></tr>
<tr class=""> <td> 2026-10-10, 14:15 </td>
 <td class="tr"> <a href="https://toplyga.lt/komanda/zalgiris"> Žalgiris </a> </td>
 <td class="result tac"> </td>
 <td class="tl"> <a href="https://toplyga.lt/komanda/suduva"> Sūduva </a> </td>
 <td class="tr"> FK „Žalgiris“ namų stadionas </td> <td class="tr"> <a href="t">Bilietai</a> </td> </tr>
<tr class=""> <td> 2026-09-20, 18:00 </td>
 <td class="tr"> <a href="https://toplyga.lt/komanda/fa-siauliai"> Šiauliai </a> </td>
 <td class="result tac"> <a href="r">0 : 2</a> </td>
 <td class="tl"> <a href="https://toplyga.lt/komanda/k-zalgiris"> K. Žalgiris </a> </td>
 <td class="tr"> Šiaulių stadionas </td> <td class="tr"> </td> </tr></table>
"""

TOP_TABLE = """
<table><tr><td class="tal">1</td><td class="tal"><a href="https://toplyga.lt/komanda/k-zalgiris"> KŽ </a></td>
<td class="tac">28</td><td class="tac">14</td><td class="tac">9</td><td class="tac">5</td>
<td class="tac">56</td><td class="tac">20</td><td class="tac">+36</td><td class="tac">51</td></tr>
<tr><td class="tal">2</td><td class="tal"><a href="https://toplyga.lt/komanda/suduva"> S </a></td>
<td class="tac">28</td><td class="tac">13</td><td class="tac">11</td><td class="tac">4</td>
<td class="tac">39</td><td class="tac">25</td><td class="tac">+14</td><td class="tac">50</td></tr>
<tr><td class="tal">4</td><td class="tal"><a href="https://toplyga.lt/komanda/zalgiris"> Ž </a></td>
<td class="tac">28</td><td class="tac">12</td><td class="tac">8</td><td class="tac">8</td>
<td class="tac">40</td><td class="tac">30</td><td class="tac">+10</td><td class="tac">44</td></tr></table>
"""


def test_lkl_schedule_keeps_only_unplayed_games_with_dates():
    gs = games.parse_lkl_schedule(LKL_SCHEDULE)
    assert [(g.home, g.away) for g in gs] == [("Rytas", "Neptūnas"),
                                               ("Nevėžis-Paskolų klubas", "Žalgiris")]
    assert gs[0].start == datetime(2026, 10, 10, 16, 30, tzinfo=games.TZ)
    assert gs[0].venue == "Arena Vilnius , Vilnius"
    assert gs[1].start.day == 11 and gs[1].start.hour == 17


def test_lkl_table_uses_the_overall_table_not_the_splits():
    t = games.parse_lkl_table(LKL_TABLE)
    assert t["Žalgiris"] == (4, 0) and t["Rytas"] == (0, 1)


def test_top_schedule_skips_played_and_reads_time():
    gs = games.parse_top_schedule(TOP_SCHEDULE)
    assert len(gs) == 1 and (gs[0].home, gs[0].away) == ("zalgiris", "suduva")
    assert gs[0].start == datetime(2026, 10, 10, 14, 15, tzinfo=games.TZ)


def test_top_table():
    t = games.parse_top_table(TOP_TABLE)
    assert t["k-zalgiris"] == (1, 28, 51) and t["zalgiris"] == (4, 28, 44)


def test_lkl_pricing_is_sane_and_bounded():
    t = games.parse_lkl_table(LKL_TABLE)
    assert games.lkl_home_prob("Žalgiris", "Rytas", t) > 60
    assert games.lkl_home_prob("Nevėžis-Paskolų klubas", "Žalgiris", t) >= 10
    assert games.lkl_home_prob("Rytas", "Rytas", t) == 56          # equal teams: home edge only


def test_top_pricing_sums_to_100():
    t = games.parse_top_table(TOP_TABLE)
    ph, pd, pa = games.top_probs("zalgiris", "suduva", t)
    assert ph + pd + pa == 100 and pd >= 18


def test_specs_match_the_hand_made_templates():
    t = games.parse_lkl_table(LKL_TABLE)
    g = games.parse_lkl_schedule(LKL_SCHEDULE)[1]
    s = games.lkl_spec(g, t)
    assert s["title"] == "Kėdainių „Nevėžis-Paskolų klubas“ vs Kauno „Žalgiris“"
    assert s["closes_at"] == "2026-10-11T14:00:00+00:00"
    assert [o["label"] for o in s["options"]] == ["Kėdainių „Nevėžis-Paskolų klubas“", "Kauno „Žalgiris“"]
    ft = games.top_spec(games.parse_top_schedule(TOP_SCHEDULE)[0], games.parse_top_table(TOP_TABLE))
    assert ft["title"] == "FK „Žalgiris“ vs Marijampolės „Sūduva“"
    assert [o["label"] for o in ft["options"]][1] == "Lygiosios"


BE_LKL_FIXTURES = """
<table><tr><td class="h-text-left"><a href="/basketball/lithuania/lkl/bc-rytas-neptunas/xfh9p4BG/" class="in-match"><span>BC Rytas</span> - <span>Neptunas</span></a></td>
<td class="table-main__datetime">10.10. 14:50</td></tr>
<tr><td><a href="/basketball/lithuania/lkl/nevezis-zalgiris-kaunas/AbCdEfGh/" class="in-match"><span>Nevezis</span> - <span>Zalgiris Kaunas</span></a></td>
<td class="table-main__datetime">11.10. 15:00</td></tr></table>
"""
BE_TOP_FIXTURES = """
<table><tr><td><a href="/football/lithuania/toplyga/zalgiris-suduva/ppFZhH74/" class="in-match"><span>Zalgiris</span> - <span>Suduva</span></a></td>
<td class="table-main__datetime">10.10. 12:15</td></tr></table>
"""
ODDS_HA = {"odds": "<table><tr><td>a</td><td data-odd=\"1.40\"></td><td data-odd=\"3.00\"></td></tr>"
                   "<tr><td>b</td><td data-odd=\"1.38\"></td><td data-odd=\"3.10\"></td></tr>"
                   "<tr><td>c</td><td data-odd=\"1.44\"></td><td data-odd=\"2.90\"></td></tr></table>"}
ODDS_1X2 = {"odds": "".join(f"<tr><td data-odd=\"{h}\"></td><td data-odd=\"{d}\"></td><td data-odd=\"{a}\"></td></tr>"
                            for h, d, a in [(2.6, 3.2, 2.7), (2.5, 3.3, 2.8), (2.55, 3.25, 2.75)])}
NO_ODDS = {"odds": "<div class=\"nodata\">Unfortunately there wasn't any bookmaker offering odds</div>"}


def _fetchers(odds_by_id):
    import json

    pages = {games.LKL_SCHEDULE: LKL_SCHEDULE, games.LKL_TABLE: LKL_TABLE,
             games.TOP_SCHEDULE: TOP_SCHEDULE, games.TOP_TABLE: TOP_TABLE}

    def odds_fetch(url, **headers):
        if url.endswith("/lkl/fixtures/"):
            return BE_LKL_FIXTURES
        if url.endswith("/toplyga/fixtures/"):
            return BE_TOP_FIXTURES
        for mid, body in odds_by_id.items():
            if f"/match-odds-old/{mid}/" in url:
                return json.dumps(body)
        return "<html></html>"                       # match page: no logos
    return pages.__getitem__, odds_fetch


def test_plan_creates_only_games_with_published_odds():
    fetch, odds_fetch = _fetchers({"xfh9p4BG": ODDS_HA, "ppFZhH74": ODDS_1X2})
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    existing = {"Kėdainių „Nevėžis-Paskolų klubas“ vs Kauno „Žalgiris“"}
    specs, waiting, errors = games.plan(now, 7, fetch=fetch, existing_titles=existing,
                                        odds_fetch=odds_fetch)
    assert errors == [] and waiting == []
    titles = [s["title"] for s in specs]
    assert titles == ["Vilniaus „Rytas“ vs Klaipėdos „Neptūnas“",
                      "FK „Žalgiris“ vs Marijampolės „Sūduva“"]
    rytas = specs[0]["options"]
    assert 66 <= rytas[0]["probability"] <= 70 and sum(o["probability"] for o in rytas) == 100
    foot = [o["probability"] for o in specs[1]["options"]]
    assert sum(foot) == 100 and 25 <= foot[1] <= 33          # draw from the bookmakers
    assert specs[0]["_logos"][0].endswith(".svg")            # official LKL logo


def test_no_odds_means_wait_not_create():
    fetch, odds_fetch = _fetchers({"xfh9p4BG": NO_ODDS, "ppFZhH74": NO_ODDS, "AbCdEfGh": NO_ODDS})
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    specs, waiting, errors = games.plan(now, 7, fetch=fetch, existing_titles=set(),
                                        odds_fetch=odds_fetch)
    assert specs == [] and len(waiting) == 3


def test_a_league_outage_does_not_stop_the_other():
    _, odds_fetch = _fetchers({"ppFZhH74": ODDS_1X2})

    def fetch(url):
        if "lkl" in url:
            raise OSError("down")
        return {games.TOP_SCHEDULE: TOP_SCHEDULE, games.TOP_TABLE: TOP_TABLE}[url]
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    specs, waiting, errors = games.plan(now, 7, fetch=fetch, existing_titles=set(),
                                        odds_fetch=odds_fetch)
    assert len(specs) == 1 and errors and errors[0].startswith("LKL")


def test_betexplorer_names_map_to_our_teams():
    from arbus import betodds

    assert betodds.team_key("lkl", "Zalgiris Kaunas") == "Žalgiris"
    assert betodds.team_key("lkl", "Neptunas Klaipeda") == "Neptūnas"
    assert betodds.team_key("lkl", "Jonava") == "Hipocredit"
    assert betodds.team_key("toplyga", "FK Kauno Zalgiris") == "k-zalgiris"
    assert betodds.team_key("toplyga", "Zalgiris") == "zalgiris"
    assert betodds.team_key("toplyga", "FA Siauliai") == "fa-siauliai"
    assert betodds.team_key("toplyga", "Dziugas Telsiai") == "dziugas"


def test_consensus_removes_margin_and_needs_three_books():
    from arbus import betodds

    probs, n = betodds.parse_odds(ODDS_HA["odds"], 2)
    assert n == 3 and abs(sum(probs) - 1) < 1e-9 and 0.67 < probs[0] < 0.69
    assert betodds.parse_odds(NO_ODDS["odds"], 2) == ([], 0)
    assert betodds.to_percent([0.995, 0.005]) == [97, 3]       # floor keeps the long shot tradable


def test_toplyga_logo_upgraded_to_full_size():
    assert games.full_size_logo("https://toplyga.lt/storage/team/19/18974/conversions/logo-1202-small.png") \
        == "https://toplyga.lt/storage/team/19/18974/logo-1202.png"


def test_vs_image_renders():
    pytest = __import__("pytest")
    pytest.importorskip("PIL")
    import io

    from PIL import Image

    from arbus import matchimage

    buf = io.BytesIO()
    Image.new("RGBA", (40, 40), (200, 0, 0, 255)).save(buf, "PNG")
    png = matchimage.compose(buf.getvalue(), buf.getvalue(), "LKL")
    assert Image.open(io.BytesIO(png)).size == (1200, 675)
