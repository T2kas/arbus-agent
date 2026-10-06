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


def test_plan_creates_only_games_inside_the_week_and_not_existing():
    pages = {games.LKL_SCHEDULE: LKL_SCHEDULE, games.LKL_TABLE: LKL_TABLE,
             games.TOP_SCHEDULE: TOP_SCHEDULE, games.TOP_TABLE: TOP_TABLE}
    now = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    existing = {"Kėdainių „Nevėžis-Paskolų klubas“ vs Kauno „Žalgiris“"}
    specs, errors = games.plan(now, 7, fetch=pages.__getitem__, existing_titles=existing)
    assert errors == []
    assert [s["title"] for s in specs] == ["Vilniaus „Rytas“ vs Klaipėdos „Neptūnas“",
                                           "FK „Žalgiris“ vs Marijampolės „Sūduva“"]
    # a week later nothing in the horizon
    later = datetime(2026, 10, 20, tzinfo=timezone.utc)
    assert games.plan(later, 7, fetch=pages.__getitem__, existing_titles=set())[0] == []


def test_a_league_outage_does_not_stop_the_other():
    def fetch(url):
        if "lkl" in url:
            raise OSError("down")
        return {games.TOP_SCHEDULE: TOP_SCHEDULE, games.TOP_TABLE: TOP_TABLE}[url]
    now = datetime(2026, 10, 6, tzinfo=timezone.utc)
    specs, errors = games.plan(now, 7, fetch=fetch, existing_titles=set())
    assert len(specs) == 1 and errors and errors[0].startswith("LKL")
