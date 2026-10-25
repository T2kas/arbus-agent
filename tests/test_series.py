"""Recurring weekly series markets: period maths, rules that the existing
resolvers can parse back, option finalisation, and the serial create gate."""

from datetime import date

from arbus import resolvers, series


# ── rules must round-trip through the resolvers that will settle the market ───

def test_cinema_rules_parse_back_to_the_week():
    rules = series.cinema_rules("2026-09-14", "2026-09-20")
    period = resolvers._cinema_period(series._cinema_series()["title"], rules)
    assert period == ("weekly", "2026-09-14", "2026-09-20")
    # and it must NOT read as a Lithuanian-films-only market
    assert resolvers._is_lt_film_market(rules) is False


def test_cinema_rules_parse_back_across_a_month_boundary():
    rules = series.cinema_rules("2026-09-28", "2026-10-04")
    period = resolvers._cinema_period("Kuris filmas bus žiūrimiausias Lietuvoje?", rules)
    assert period == ("weekly", "2026-09-28", "2026-10-04")


def test_music_rules_parse_back_to_year_and_week():
    rules = series.music_rules(2026, 38, "2026-09-14", "2026-09-20")
    q = "Kuri daina bus klausomiausia Lietuvoje?"
    assert resolvers._agata_target(q, rules) == (2026, 38)
    # the resolver must read this as the SINGLES chart (the rules mention albums
    # only to exclude them, which must not flip the choice to albums)
    low = resolvers._cnorm(f"{q}\n{rules}")
    assert not ("album" in low and "singl" not in low)


# ── probabilities ────────────────────────────────────────────────────────────

def test_pct_ints_sum_to_100_and_floor_at_1():
    pct = series._pct_ints([50.0, 30.0, 0.0, 0.0])
    assert sum(pct) == 100 and all(p >= 1 for p in pct)


def test_finalize_options_normalises_and_guarantees_catch_all():
    draft = series.SeriesDraft(options=[
        series.SeriesOption(label="Filmas A", probability=60),
        series.SeriesOption(label="Filmas B", probability=30),
    ], context="")
    opts = series.finalize_options(draft, "Kitas filmas")
    assert opts is not None
    assert sum(o["probability"] for o in opts) == 100
    assert [o["label"] for o in opts][-1] == "Kitas filmas"      # catch-all appended
    assert len(opts) == 3 and all(0 < o["probability"] < 100 for o in opts)


def test_finalize_options_dedups_and_keeps_one_catch_all():
    draft = series.SeriesDraft(options=[
        series.SeriesOption(label="Filmas A", probability=50),
        series.SeriesOption(label="Filmas A", probability=10),   # dup
        series.SeriesOption(label="Filmas B", probability=25),
        series.SeriesOption(label="Kitas filmas", probability=15),
    ], context="")
    opts = series.finalize_options(draft, "Kitas filmas")
    labels = [o["label"] for o in opts]
    assert labels.count("Filmas A") == 1
    assert labels.count("Kitas filmas") == 1
    assert sum(o["probability"] for o in opts) == 100


def test_finalize_options_rejects_too_thin():
    draft = series.SeriesDraft(options=[
        series.SeriesOption(label="Vienas filmas", probability=90),
    ], context="")
    assert series.finalize_options(draft, "Kitas filmas") is None


# ── the serial create gate ───────────────────────────────────────────────────

def _cinema_market(period_rules: str, resolved: bool = False, status: str = "open"):
    return {"id": "m1", "title": "Kuris filmas bus žiūrimiausias Lietuvoje?",
            "status": status, "rules": period_rules,
            "winning_option_id": "o1" if resolved else None,
            "market_options": [{"id": "o1", "label": "Filmas A"}]}


def test_has_live_blocks_a_second_open_market():
    rows = [_cinema_market(series.cinema_rules("2026-09-14", "2026-09-20"))]
    assert series._has_live(rows, series._cinema_series()) is True


def test_resolved_market_does_not_block():
    rows = [_cinema_market(series.cinema_rules("2026-09-14", "2026-09-20"),
                           resolved=True)]
    assert series._has_live(rows, series._cinema_series()) is False


def test_series_period_of_reads_the_market_week():
    row = _cinema_market(series.cinema_rules("2026-09-14", "2026-09-20"))
    assert series.series_period_of(row, series._cinema_series()) == \
        ("2026-09-14", "2026-09-20")


def test_music_target_week_is_todays_iso_week():
    d = date(2026, 9, 16)
    assert series.music_target_week(d) == d.isocalendar()[:2]


def test_music_target_week_rolls_forward_after_thursday_close():
    assert series.music_target_week(date(2026, 10, 22)) == (2026, 43)   # Thu
    assert series.music_target_week(date(2026, 10, 23)) == (2026, 44)   # Fri
    assert series.music_target_week(date(2026, 10, 25)) == (2026, 44)   # Sun
    assert series.music_target_week(date(2026, 12, 25)) == (2026, 53)
    assert series.music_target_week(date(2027, 1, 1)) == (2027, 1)


def test_newest_image_reused_from_existing_row():
    rows = [{"title": "Kuris filmas bus žiūrimiausias Lietuvoje?",
             "image_url": "https://img/cinema.jpg", "rules": "", "status": "open"}]
    assert series._newest_image(rows, series._cinema_series()) == \
        "https://img/cinema.jpg"


def test_series_research_uses_openai_when_configured(monkeypatch):
    """Weekly cinema/music research must run on SERIES_LLM_PROVIDER (OpenAI)."""
    from arbus import llm
    seen = {}
    monkeypatch.setattr(series.config, "SERIES_LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm, "available_providers", lambda: ["anthropic", "openai"])
    monkeypatch.setattr(llm, "research",
                        lambda *a, **k: (seen.__setitem__("research", k.get("force_provider")), "t")[1])
    monkeypatch.setattr(llm, "structure", lambda *a, **k: (
        seen.__setitem__("structure", k.get("force_provider")),
        series.SeriesDraft(options=[series.SeriesOption(label="A", probability=50),
                                    series.SeriesOption(label="B", probability=30)],
                           context="c"))[1])
    monkeypatch.setattr(series, "music_spec_from_chart", lambda *a, **k: None)   # force the LLM path
    spec, meta = series.build_spec(series._music_series(), (2026, 40), "", series._tz())
    assert spec is not None
    assert seen == {"research": "openai", "structure": "openai"}


def test_series_provider_falls_back_without_key(monkeypatch):
    from arbus import llm
    monkeypatch.setattr(series.config, "SERIES_LLM_PROVIDER", "openai")
    monkeypatch.setattr(llm, "available_providers", lambda: ["anthropic"])
    assert series.series_provider() is None      # → normal provider choice


def test_music_priced_from_last_weeks_chart_without_llm(monkeypatch):
    from arbus import llm, resolvers

    chart = [(1, "Different Dimension, QV", "Sakartvelas - Special Version"),
             (2, "Jessica Shy", "Kas Kaltas"), (3, "Different Dimension, QV", "Palangoj"),
             (4, "Jessica Shy", "Jeigu Būčiau"), (5, "Jessica Shy", "Žvėris")]
    asked = []
    monkeypatch.setattr(resolvers, "_agata_url", lambda y, w, *a: (asked.append((y, w)), ("u", "h"))[1])
    monkeypatch.setattr(resolvers, "parse_agata_rows", lambda html: chart)

    def no_llm(*a, **k):
        raise AssertionError("music must not call the LLM")
    monkeypatch.setattr(llm, "research", no_llm)
    spec, meta = series.build_spec(series._music_series(), (2026, 43), "img", series._tz())
    assert asked == [(2026, 42)]                                  # last week's chart
    probs = [o["probability"] for o in spec["options"]]
    assert probs == [80, 6, 3, 1, 1, 9] and sum(probs) == 100
    assert spec["options"][0]["label"] == "Different Dimension, QV – „Sakartvelas - Special Version“"
    assert spec["options"][-1]["label"] == "Kita daina"
    assert spec["closes_at"].startswith("2026-10-22T21:00")       # Thursday, before Friday's chart


def test_music_week_one_reads_last_years_final_week(monkeypatch):
    from arbus import resolvers

    asked = []
    monkeypatch.setattr(resolvers, "_agata_url", lambda y, w, *a: (asked.append((y, w)), None)[1])
    assert series.music_spec_from_chart(series._music_series(), (2027, 1), "", "r", "s", series._tz()) is None
    assert asked == [(2026, 53)]
