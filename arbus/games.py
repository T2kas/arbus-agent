"""Auto-create game markets for LKL (basketball) and TOPLYGA (football).

Every run reads the official schedules (lkl.lt, toplyga.lt) and, for each game
starting within GAMES_HORIZON_DAYS that has no market yet, looks up the
bookmakers' odds (betodds.py). Only games whose odds are published are created:
opening prices are the bookmaker consensus with the margin removed; games
without odds wait for the next run. Each market gets a "logo VS logo" picture
(matchimage.py) built from the leagues' own logos. LKL markets have two named
outcomes (dual chart), TOPLYGA three with „Lygiosios“.

The standings model (lkl_home_prob / top_probs) only feeds the context text and
tests — it never prices a live market.

Titles and templates match the hand-made October markets exactly ("Kauno
„Žalgiris“ vs Vilniaus „Rytas“"), so an existing market is never duplicated.
No LLM is used.
"""

from __future__ import annotations

import html as htmlmod
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import app as app_api, config, notify

TZ = ZoneInfo("Europe/Vilnius")
UA = {"User-Agent": "Mozilla/5.0 (arbus-agent games bot)"}
MONTHS = ["sausio", "vasario", "kovo", "balandžio", "gegužės", "birželio", "liepos",
          "rugpjūčio", "rugsėjo", "spalio", "lapkričio", "gruodžio"]

LKL_SCHEDULE = "https://lkl.lt/tvarkarastis"
LKL_TABLE = "https://lkl.lt/turnyrine-lentele"
TOP_SCHEDULE = "https://toplyga.lt/tvarkarastis/1"
TOP_TABLE = "https://toplyga.lt/turnyrine-lentele/1"

# Display names; must match markets already created by hand (dedupe is by title).
LKL_NAMES = {
    "Žalgiris": "Kauno „Žalgiris“", "Rytas": "Vilniaus „Rytas“",
    "Nevėžis-Paskolų klubas": "Kėdainių „Nevėžis-Paskolų klubas“",
    "Tauragė": "„Tauragė“", "Hipocredit": "„Hipocredit“", "Gargždai": "„Gargždai“",
    "Juventus": "Utenos „Juventus“", "Lietkabelis": "Panevėžio „Lietkabelis“",
    "Neptūnas": "Klaipėdos „Neptūnas“", "Šiauliai": "„Šiauliai“",
}
# Pre-season strength (expected win share). Blended with the real record, so it
# matters early in the season and fades as games are played.
LKL_PRIOR = {
    "Žalgiris": 0.88, "Rytas": 0.72, "Neptūnas": 0.55, "Lietkabelis": 0.55,
    "Šiauliai": 0.45, "Gargždai": 0.45, "Juventus": 0.40, "Hipocredit": 0.35,
    "Tauragė": 0.33, "Nevėžis-Paskolų klubas": 0.30,
}
LKL_PRIOR_WEIGHT = 8          # games' worth of weight the prior carries
LKL_HOME_EDGE = 0.06

TOP_NAMES = {
    "k-zalgiris": "„Kauno Žalgiris“", "suduva": "Marijampolės „Sūduva“",
    "transinvest": "Vilniaus „TransINVEST“", "zalgiris": "FK „Žalgiris“",
    "banga": "Gargždų „Banga“", "dziugas": "Telšių „Džiugas“",
    "panevezys": "FK „Panevėžys“", "hegelmann": "„Hegelmann“", "siauliai": "FK „Šiauliai“",
    "fa-siauliai": "FK „Šiauliai“",
}

IMG_ZAL = ("https://crwtwtwljqypvgvvfmyo.supabase.co/storage/v1/object/public/market-images/"
           "72281ef3-6b91-4f7c-b0ce-92b7c6ebac6c/77cffeba-467d-49a3-bbe5-c5bfe7115a6f.jpg")
IMG_TOP = ("https://crwtwtwljqypvgvvfmyo.supabase.co/storage/v1/object/public/market-images/"
           "3686a8dc-a32c-459b-af52-55089933ee92/d3a1ee67-6338-47c5-acb5-549a707c0656.jfif")


@dataclass
class Game:
    league: str            # "lkl" | "toplyga"
    start: datetime        # aware, Europe/Vilnius
    home: str              # source key: LKL short name / TOPLYGA slug
    away: str
    venue: str = ""
    home_logo: str = ""    # official logo URL (LKL: SVG, TOPLYGA: full-size PNG)
    away_logo: str = ""


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def lt_date(d: datetime) -> str:
    return f"{d.year} m. {MONTHS[d.month - 1]} {d.day} d."


# ── parsing ────────────────────────────────────────────────────────────────

_LT_DATE = re.compile(r"(\d{4}) m\. (\w+) (\d{1,2}) d\.")


def parse_lkl_schedule(page: str) -> list[Game]:
    """Upcoming LKL games ("VS" = not played yet) under their date headings.

    The page also lists the King Mindaugas Cup (data-championship="kmt"); only
    the LKL section is read, because the market rules say "LKL regular season"."""
    start = page.find('data-championship="lkl"')
    if start >= 0:
        end = page.find('data-championship=', start + 10)
        page = page[start:end if end > 0 else len(page)]
    games, day = [], None
    for m in re.finditer(r'<div class="font-semibold text-2xl[^"]*">(.*?)</div>'
                         r'|<div class="result-item">(.*?)(?=<div class="result-item">'
                         r'|<div class="font-semibold text-2xl|$)', page, flags=re.S):
        if m.group(1) is not None:
            dm = _LT_DATE.search(_clean(m.group(1)))
            day = None
            if dm and dm.group(2) in MONTHS:
                day = (int(dm.group(1)), MONTHS.index(dm.group(2)) + 1, int(dm.group(3)))
            continue
        block = m.group(2)
        if day is None or not re.search(r'rungtynes/\d+">\s*VS\s*<', block):
            continue
        tm = re.search(r'text-lg font-bold">\s*(\d{1,2}):(\d{2})', block)
        imgs = re.findall(r'<img src="([^"]*)" alt="([^"]+)"', block)
        if not tm or len(imgs) < 2:
            continue
        venue = re.search(r'text-lg font-bold">.*?</div>\s*<div>(.*?)</div>', block, flags=re.S)
        start = datetime(*day, int(tm.group(1)), int(tm.group(2)), tzinfo=TZ)
        logo = lambda src: src if src.startswith("http") else "https://lkl.lt" + src
        games.append(Game("lkl", start, imgs[0][1], imgs[1][1],
                          _clean(venue.group(1)) if venue else "",
                          logo(imgs[0][0]), logo(imgs[1][0])))
    return games


def parse_lkl_table(page: str) -> dict[str, tuple[int, int]]:
    """LKL short name -> (wins, losses), from the first (overall) table.

    The page also carries home/away split tables further down; the first row
    seen for a team is the overall one."""
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        name = re.search(r'class="team-title">(.*?)</a>', row, flags=re.S)
        w = re.search(r'num md success">\s*(\d+)', row)
        l = re.search(r'num md danger">\s*(\d+)', row)
        if name and w and l:
            out.setdefault(_clean(name.group(1)), (int(w.group(1)), int(l.group(1))))
    return out


def parse_top_schedule(page: str) -> list[Game]:
    """Unplayed TOPLYGA fixtures (empty result cell)."""
    games = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        dm = re.search(r"<td>\s*(\d{4})-(\d{2})-(\d{2}),\s*(\d{1,2}):(\d{2})\s*</td>", row)
        slugs = re.findall(r'toplyga\.lt/komanda/([a-z0-9-]+)"', row)
        result = re.search(r'<td class="result tac">(.*?)</td>', row, flags=re.S)
        if not dm or len(slugs) < 2 or not result:
            continue
        if re.search(r"\d+\s*[:\-]\s*\d+", _clean(result.group(1))):
            continue                                     # already played
        cells = re.findall(r'<td class="tr">(.*?)</td>', row, flags=re.S)
        venue = _clean(cells[-2]) if len(cells) >= 2 else ""
        logos = [full_size_logo(u) for u in
                 re.findall(r'<img class="f[lr]" src="([^"]+/storage/team/[^"]+)"', row)]
        y, mo, d, hh, mm = map(int, dm.groups())
        games.append(Game("toplyga", datetime(y, mo, d, hh, mm, tzinfo=TZ),
                          slugs[0], slugs[1], venue,
                          logos[0] if len(logos) > 0 else "", logos[1] if len(logos) > 1 else ""))
    return games


def full_size_logo(url: str) -> str:
    """toplyga.lt/storage/team/a/b/conversions/x-small.png -> .../a/b/x.png (1000+ px)."""
    m = re.match(r"(.*/storage/team/[^/]+/[^/]+/)conversions/(.+?)-(?:small|medium)\.png$", url)
    return f"{m.group(1)}{m.group(2)}.png" if m else url


def parse_top_table(page: str) -> dict[str, tuple[int, int, int]]:
    """TOPLYGA slug -> (place, played, points)."""
    out = {}
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", page, flags=re.S):
        slug = re.search(r'toplyga\.lt/komanda/([a-z0-9-]+)"', row)
        nums = re.findall(r'<td class="ta[cl]">\s*([+-]?\d+)\s*</td>', row)
        if slug and len(nums) >= 9:
            out.setdefault(slug.group(1), (int(nums[0]), int(nums[1]), int(nums[-1])))
    return out


# ── pricing ────────────────────────────────────────────────────────────────

def lkl_strength(team: str, table: dict[str, tuple[int, int]]) -> float:
    w, l = table.get(team, (0, 0))
    prior = LKL_PRIOR.get(team, 0.45)
    return (prior * LKL_PRIOR_WEIGHT + w) / (LKL_PRIOR_WEIGHT + w + l)


def lkl_home_prob(home: str, away: str, table) -> int:
    a, b = lkl_strength(home, table), lkl_strength(away, table)
    p = a * (1 - b) / (a * (1 - b) + b * (1 - a))       # log5
    p = min(max(p + LKL_HOME_EDGE, 0.10), 0.90)
    return round(p * 100)


def top_probs(home: str, away: str, table) -> tuple[int, int, int]:
    def ppg(t):
        _, played, pts = table.get(t, (0, 0, 0))
        return pts / played if played else 1.3
    d = ppg(home) - ppg(away)
    ph = min(max(44 + 28 * d, 12), 75)
    pa = min(max(29 - 24 * d, 10), 72)
    pd = max(100 - ph - pa, 20)
    s = ph + pd + pa
    ph, pd = round(ph * 100 / s), round(pd * 100 / s)
    return ph, pd, 100 - ph - pd


# ── market specs (same templates as the October batch) ─────────────────────

def _close(g: Game) -> str:
    return g.start.astimezone(timezone.utc).isoformat()


def lkl_spec(g: Game, table, probs: list[int] | None = None) -> dict:
    home, away = LKL_NAMES.get(g.home, f"„{g.home}“"), LKL_NAMES.get(g.away, f"„{g.away}“")
    p = probs[0] if probs else lkl_home_prob(g.home, g.away, table)
    deadline = lt_date(g.start + timedelta(days=30))
    hw, hl = table.get(g.home, (0, 0))
    aw, al = table.get(g.away, (0, 0))
    ctx = (f"LKL lentelėje {home} turi {hw} pergal{'ę' if hw == 1 else 'es'} ir {hl} pralaimėjim{'ą' if hl == 1 else 'us'}, "
           f"{away} – {aw}–{al}. Rungtynės vyks {g.venue or 'šeimininkų arenoje'}."
           + (" Kauno „Žalgiris“ LKL yra aiškus favoritas, todėl įdomiausia – ar varžovai sugebės nustebinti."
              if "Žalgiris" in (g.home, g.away) and "Rytas" not in (g.home, g.away) else ""))
    return {
        "title": f"{home} vs {away}",
        "subtitle": f"LKL, {lt_date(g.start)} {g.start:%H:%M} Lietuvos laiku",
        "category": "sportas",
        "image_url": config.GAMES_LKL_IMAGE or IMG_ZAL,
        "liquidity": config.GAMES_LIQUIDITY,
        "rules": (
            f"Rinka bus išspręsta pagal oficialų {home} ir {away} 2026–2027 m. LKL reguliariojo sezono rungtynių, numatytų {lt_date(g.start)}, rezultatą.\n\n"
            "Vertinamas galutinis rezultatas, įskaitant visus pratęsimus. Lygiosios nėra galima baigtis.\n\n"
            "Jeigu rungtynės bus nutrauktos, tačiau lyga patvirtins galutinį rezultatą arba vienai komandai skirs techninę pergalę, naudojamas oficialus sprendimas.\n\n"
            f"Jeigu rungtynės bus nukeltos, vertinamas tos pačios suplanuotos dvikovos rezultatas, jeigu ji įvyks iki {deadline} imtinai. "
            "Jeigu iki šios datos nebus sužaista ir nebus patvirtintas oficialus rezultatas, galutinis rezultatas bus paskirstytas po 50% abiem baigtims.\n\n"
            "Rezultato šaltiniai yra oficialus LKL rungtynių puslapis (lkl.lt) ir komandų oficialūs puslapiai."),
        "context": ctx,
        "closes_at": _close(g),
        "options": [{"label": home, "probability": p}, {"label": away, "probability": 100 - p}],
    }


def top_spec(g: Game, table, probs: list[int] | None = None) -> dict:
    home, away = TOP_NAMES.get(g.home, f"„{g.home}“"), TOP_NAMES.get(g.away, f"„{g.away}“")
    ph, pd, pa = probs if probs else top_probs(g.home, g.away, table)
    deadline = lt_date(g.start + timedelta(days=30))
    hp, ap = table.get(g.home), table.get(g.away)
    ctx = (f"TOPLYGOS lentelėje {home} yra {hp[0]}-oje vietoje su {hp[2]} taškais, "
           f"{away} – {ap[0]}-oje su {ap[2]} taškais." if hp and ap else
           f"TOPLYGOS rungtynės tarp {home} ir {away}.")
    return {
        "title": f"{home} vs {away}",
        "subtitle": f"TOPLYGA, {lt_date(g.start)} {g.start:%H:%M}",
        "category": "sportas",
        "image_url": config.GAMES_TOP_IMAGE or IMG_TOP,
        "liquidity": config.GAMES_LIQUIDITY,
        "rules": (
            f"Rinka bus išspręsta pagal oficialų {home} ir {away} TOPLYGOS rungtynių, numatytų {lt_date(g.start)}, rezultatą.\n\n"
            "Vertinamas rezultatas pasibaigus 90 minučių ir teisėjo pridėtam laikui. Jeigu rungtynės baigsis lygiosiomis, laimės baigtis „Lygiosios“.\n\n"
            "Jeigu rungtynės bus sustabdytos, tačiau Lietuvos futbolo federacija oficialiai patvirtins galutinį rezultatą arba vienai komandai skirs techninę pergalę, naudojamas šis rezultatas.\n\n"
            f"Jeigu rungtynės bus nukeltos, vertinamos tos pačios komandų poros nukeltos rungtynės, jeigu jos įvyks iki {deadline} imtinai. "
            "Jeigu iki šios datos rungtynės nebus sužaistos ir nebus patvirtintas oficialus rezultatas, galutinis rezultatas bus paskirstytas po lygiai visoms trims baigtims.\n\n"
            "Rezultato šaltiniai yra oficialus TOPLYGOS tvarkaraštis (toplyga.lt) ir Lietuvos futbolo federacija."),
        "context": ctx,
        "closes_at": _close(g),
        "options": [{"label": home, "probability": ph}, {"label": "Lygiosios", "probability": pd},
                    {"label": away, "probability": pa}],
    }


# ── run ────────────────────────────────────────────────────────────────────

def _get(url: str) -> str:
    import requests

    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    return r.text


def plan(now: datetime | None = None, horizon_days: int | None = None,
         fetch=_get, existing_titles: set[str] | None = None,
         odds_fetch=None) -> tuple[list[dict], list[str], list[str]]:
    """Specs for games within the horizon that have no market yet AND published
    bookmaker odds. Returns (specs, waiting, errors): `waiting` lists games
    skipped because odds are not out yet — they are retried on the next run."""
    from . import betodds

    odds_fetch = odds_fetch or betodds._get
    now = now or datetime.now(timezone.utc)
    horizon = now + timedelta(days=horizon_days or config.GAMES_HORIZON_DAYS)
    min_lead = now + timedelta(hours=config.GAMES_MIN_LEAD_HOURS)
    errors, waiting, candidates = [], [], []

    if existing_titles is None:
        rows, err = app_api.markets(600)
        if err:
            return [], [], [f"app: {err}"]          # cannot dedupe -> create nothing
        existing_titles = {r.get("title") for r in rows if r.get("status") != "resolved"}

    sources = []
    if "lkl" in config.GAMES_LEAGUES:
        sources.append(("lkl", "LKL", LKL_SCHEDULE, LKL_TABLE, parse_lkl_schedule, parse_lkl_table, lkl_spec))
    if "toplyga" in config.GAMES_LEAGUES:
        sources.append(("toplyga", "TOPLYGA", TOP_SCHEDULE, TOP_TABLE, parse_top_schedule, parse_top_table, top_spec))
    for league, name, sched_url, table_url, parse_s, parse_t, make in sources:
        try:
            games = parse_s(fetch(sched_url))
            table = parse_t(fetch(table_url))
        except Exception as e:          # one league down must not stop the other
            errors.append(f"{name}: {type(e).__name__}: {e}")
            continue
        todo = [g for g in games if min_lead <= g.start <= horizon
                and make(g, table)["title"] not in existing_titles]
        if not todo:
            continue
        try:
            fixtures = betodds.fixtures_for(league, odds_fetch)
        except Exception as e:
            errors.append(f"{name} koeficientai: {type(e).__name__}: {e}")
            continue
        for g in todo:
            title = make(g, table)["title"]
            fx = betodds.find(fixtures, g.home, g.away, g.start)
            mo = None
            if fx:
                try:
                    mo = betodds.match_odds(league, fx, odds_fetch)
                except Exception as e:
                    errors.append(f"{title}: koeficientai {type(e).__name__}: {e}")
                    continue
            if mo is None:
                waiting.append(title)
                continue
            spec = make(g, table, betodds.to_percent(mo.probs))
            spec["_odds_bookmakers"] = mo.bookmakers
            spec["_logos"] = [g.home_logo, g.away_logo] if g.home_logo and g.away_logo else mo.logos
            spec["_image_name"] = re.sub(r"[^a-z0-9]+", "-", betodds.fold(
                f"{league} {g.start:%Y%m%d %H%M} {g.home} {g.away}")).strip("-")
            spec["_caption"] = f"{'LKL' if league == 'lkl' else 'TOPLYGA'} · {MONTHS[g.start.month - 1]} {g.start.day} d. {g.start:%H:%M}"
            candidates.append(spec)

    seen, fresh = set(), []
    for s in candidates:
        if s["title"] not in seen:
            seen.add(s["title"])
            fresh.append(s)
    return fresh, waiting, errors


def _public_spec(spec: dict) -> dict:
    return {k: v for k, v in spec.items() if not k.startswith("_")}


def run(dry_run: bool = False, alert: bool = True) -> list[dict]:
    from . import matchimage

    specs, waiting, errors = plan()
    reports = []
    for s in specs:
        odds_note = f"koef. iš {s['_odds_bookmakers']} bendrovių"
        if dry_run:
            reports.append({"title": s["title"], "status": "would-create", "options": s["options"],
                            "detail": odds_note})
            continue
        img = matchimage.game_image(s["_logos"], s["_image_name"], s["_caption"])
        if img:
            s["image_url"] = img
        ok, detail = app_api.create_market(_public_spec(s))
        reports.append({"title": s["title"], "status": "created" if ok else "error",
                        "detail": detail if not ok else odds_note + ("" if img else ", be VS paveikslėlio")})
    for t in waiting:
        reports.append({"title": t, "status": "waiting", "detail": "laukiama koeficientų"})
    if alert and not dry_run and (any(r["status"] != "waiting" for r in reports) or errors):
        made = [r for r in reports if r["status"] == "created"]
        bad = [r for r in reports if r["status"] == "error"]
        lines = [f"🏀⚽ Rungtynių rinkos: sukurta {len(made)}, laukia koeficientų {len(waiting)}"]
        lines += [f"✅ {r['title']} ({r['detail']})" for r in made]
        lines += [f"❌ {r['title']}: {r['detail']}" for r in bad]
        lines += [f"⚠️ {e}" for e in errors]
        notify.send("\n".join(lines))
    for e in errors:
        reports.append({"title": "-", "status": "error", "detail": e})
    return reports
