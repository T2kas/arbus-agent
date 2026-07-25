"""Offline tests for the deterministic validation gates."""

from datetime import date

from arbus import validate
from arbus.schemas import Candidate

TODAY = date(2026, 7, 13)


def make(**overrides) -> Candidate:
    base = dict(
        question_lt="Ar Žalgiris laimės rungtynes prieš Rytą?",
        market_type="binary",
        options_lt=["Taip", "Ne"],
        probabilities=[0.6, 0.4],
        category="sports",
        resolve_by="2026-07-20",
        duration_class="medium",
        resolution_hint_lt="Pagal oficialų LKL rungtynių rezultatą.",
        sources=["https://www.basketnews.lt/example"],
        rationale_en="Big rivalry game this week.",
    )
    base.update(overrides)
    return Candidate(**base)


def test_valid_candidate_passes():
    fixed, reason = validate.validate_candidate(make(), TODAY)
    assert reason is None
    assert fixed.duration_class == "medium"


def test_gambling_language_rejected():
    cand = make(question_lt="Ar lažybų koeficientas kris?")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "gambling" in reason


def test_bare_bet_conjunction_is_allowed():
    # "bet" = "but" in Lithuanian; must NOT trip the linter
    assert validate.lint_gambling("Komanda pralaimėjo, bet kovojo iki galo") == []


def test_istatymas_is_not_gambling():
    # "įstatymas" (law) contains "statym" but must NOT trip the linter
    assert validate.lint_gambling("Ar Seimas priims įstatymo pataisą?") == []
    assert validate.lint_gambling("Koks statymas laimės?") != []


def test_blocked_subjects_rejected():
    cand = make(question_lt="Ar grupės Šeškės daina pateks į Top 50 iki rugsėjo 1 d.?")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "blocked" in reason


def test_english_question_rejected():
    cand = make(question_lt="Will the team win the game?")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "Lithuanian" in reason


def test_past_resolve_date_rejected():
    cand = make(resolve_by="2026-07-10")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "future" in reason


def test_duration_reclassified_from_date():
    cand = make(resolve_by="2026-07-14", duration_class="long")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None and fixed.duration_class == "short"


def test_probabilities_normalized():
    cand = make(probabilities=[0.99, 0.11])
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None
    assert abs(sum(fixed.probabilities) - 1.0) < 0.01
    assert all(0.02 <= p <= 0.98 for p in fixed.probabilities)


def test_multi_accepts_2_to_6_options():
    # Two named options (a duel) are a valid multi market.
    ok = make(market_type="multi", options_lt=["Pakeis", "Paliks"],
              probabilities=[0.5, 0.5])
    fixed, reason = validate.validate_candidate(ok, TODAY)
    assert reason is None and fixed is not None
    # One option or seven are still degenerate.
    for opts in (["A"], ["A", "B", "C", "D", "E", "F", "G"]):
        bad = make(market_type="multi", options_lt=opts,
                   probabilities=[round(1 / len(opts), 3)] * len(opts))
        fixed, reason = validate.validate_candidate(bad, TODAY)
        assert fixed is None and "2-6" in reason


def test_vague_headline_rejected():
    for bad in [
        "Ar Vyriausybė priims panašaus lygio sprendimą?",
        "Ar artimiausią savaitę bus karšta?",
        "Ar bus paskelbtas sprendimas (pvz., naujas ambasadorius)?",
    ]:
        fixed, reason = validate.validate_candidate(make(question_lt=bad), TODAY)
        assert fixed is None and "vague" in reason, bad


def test_undefined_class_rejected_but_precise_threshold_allowed():
    bad = make(question_lt="Ar iki rugsėjo 15 d. bent vienas didelis influenceris paskelbs apie sugrįžimą?")
    fixed, reason = validate.validate_candidate(bad, TODAY)
    assert fixed is None and "vague" in reason
    ok = make(question_lt="Ar Vilniuje rugpjūčio mėnesį bent vieną dieną bus 30 laipsnių karščio?")
    fixed, reason = validate.validate_candidate(ok, TODAY)
    assert reason is None


def test_non_url_sources_rejected():
    cand = make(sources=["LRT", "Lrytas"])
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "source" in reason


def test_mixed_sources_filtered_to_urls():
    cand = make(sources=["LRT", "https://www.lrt.lt/x"])
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None and fixed.sources == ["https://www.lrt.lt/x"]


def test_min_resolve_date_enforced():
    from datetime import date as d
    cand = make(resolve_by="2026-07-20")
    fixed, reason = validate.validate_candidate(cand, TODAY, min_resolve=d(2026, 8, 1))
    assert fixed is None and "launch" in reason
    cand = make(resolve_by="2026-08-05")
    fixed, reason = validate.validate_candidate(cand, TODAY, min_resolve=d(2026, 8, 1))
    assert reason is None


def test_105_style_subjective_options_rejected():
    # The real #105 (Žemaitaitis): options are moods/tempo, not checkable outcomes.
    cand = make(
        question_lt="Kas iki spalio 1 d. bus konservatorių sprendimas R. Žemaitaičio atžvilgiu?",
        market_type="multi",
        options_lt=[
            "Paremti apkaltos procesą ir aktyviai jį stumti",
            "Paremti apkaltą, bet ją palikti „ant lėto“ be aktyvių veiksmų",
            "Nebeparemti apkaltos ir ieškoti alternatyvaus sprendimo",
            "Tema bus faktiškai „padėta į stalčių“ be aiškaus viešo sprendimo",
        ],
        probabilities=[0.45, 0.25, 0.15, 0.15],
        resolve_by="2026-10-01",
    )
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "unresolvable" in reason


def test_main_stance_framing_rejected():
    cand = make(
        question_lt="Kas bus pagrindinis viešai įvardytas konservatorių (TS-LKD) "
                    "sprendimas dėl apkaltos?",
    )
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "unresolvable" in reason


def test_concrete_multi_options_still_pass():
    # Named, mutually exclusive, checkable winners — must NOT trip the linter.
    cand = make(
        question_lt="Kas bus aukščiausiai „Spotify Top 50 Lietuva“?",
        market_type="multi",
        options_lt=["Jessica Shy", "8 Kambarys", "Omerta", "Kita"],
        probabilities=[0.3, 0.25, 0.2, 0.25],
        resolve_by="2026-08-05",
    )
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None and fixed is not None


def test_vague_options_rejected():
    # Options must be as clear as the headline — no "panašus"/"pvz." filler.
    cand = make(
        question_lt="Kas laimės rinkimus 2026 m.?",
        market_type="multi",
        options_lt=["Partija A", "Partija B", "Panašus rezultatas kaip pernai"],
        probabilities=[0.4, 0.4, 0.2],
    )
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "options" in reason


def test_slang_option_rejected():
    cand = make(
        question_lt="Ką Vyriausybė nuspręs dėl mokesčio 2026 m.?",
        market_type="multi",
        options_lt=["Priims mokestį", "Atmes mokestį", "Nuleis ant stabdžių"],
        probabilities=[0.4, 0.4, 0.2],
    )
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "unresolvable" in reason


def test_parentheses_in_headline_rejected():
    # #92: parenthetical detail must move to the rules.
    cand = make(question_lt="Ar egzotinis gyvūnas (ne Lietuvai būdingas) bus "
                            "užfiksuotas Lietuvoje iki 2026-09-30?",
                resolve_by="2026-09-30")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "headline format" in reason


def test_viesai_in_headline_rejected():
    # #84/Oksana Pikul: "viešai" is rules-only noise in a headline.
    cand = make(question_lt="Ar Oksana Pikul iki 2026-08-10 viešai paskelbs pareiškimą?",
                resolve_by="2026-08-10")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "headline format" in reason


def test_unmeasurable_image_and_mood_rejected():
    # #85 "kardinaliai pakeistą įvaizdį" and #84 "emocinga reakcija" are unmeasurable.
    for bad in ["Ar Vaidas Baumila išlaikys kardinaliai pakeistą įvaizdį iki 2026-09-01?",
                "Ar Oksana Pikul paskelbs dar vieną emocingą reakciją iki 2026-08-10?"]:
        fixed, reason = validate.validate_candidate(make(question_lt=bad, resolve_by="2026-09-01"), TODAY)
        assert fixed is None and "vague" in reason, bad


def test_source_attribution_in_headline_rejected():
    # #97: "pagal ... duomenis" belongs in the rules, not the headline.
    cand = make(question_lt="Ar Jūros šventėje Klaipėdoje pagal savivaldybės duomenis "
                            "dalyvių skaičius viršys 300 tūkst.?",
                resolve_by="2026-08-15")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "headline format" in reason


def test_main_content_focus_rejected():
    # #99: "pagrindiniu turinio akcentu" — an unmeasurable 'main focus'.
    cand = make(question_lt="Ar Justas Pečeliūnas paskelbs projektą, kurio pagrindiniu "
                            "turinio akcentu bus skyrybos?",
                resolve_by="2026-09-15")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "unresolvable" in reason


def test_day_precision_date_in_headline_rejected():
    # #110/#111/#115/#116: exact dates belong in resolve_by + rules, not the headline.
    for bad in [
        "Ar Vilniuje bent vieną dieną tarp 2026 m. rugpjūčio 1–31 d. bus ≥30 mm kritulių?",
        "Ar Jūros šventėje bus pranešta apie 0,5 mln. lankytojų iki 2026 m. rugpjūčio 15 d.?",
        "Ar Airinė Palšytė iki 2026-10-31 įveiks 1,96 m aukštį?",
    ]:
        fixed, reason = validate.validate_candidate(
            make(question_lt=bad, resolve_by="2026-10-31"), TODAY)
        assert fixed is None and "headline format" in reason, bad


def test_month_reference_in_headline_allowed():
    # The user's own rewrite: a month is fine, a day-precision date is not.
    cand = make(question_lt="Ar Vilniuje rugpjūčio mėnesį bus užfiksuotas ≥30 mm "
                            "paros kritulių kiekis?",
                resolve_by="2026-09-05")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None and fixed is not None


def test_causal_link_market_rejected():
    # #109: "will X's closure encourage Y" — causation is never reported by a source.
    cand = make(question_lt="Ar „Mere“ uždarymas paskatins kito žemų kainų tinklo atėjimą?")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "unresolvable" in reason


def test_oficialiai_in_headline_rejected():
    # #111: "oficialų" is rules-only, like "viešai".
    cand = make(question_lt="Ar rinktinė patirs oficialų žaidėjų boikotą?")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "headline format" in reason


def test_binary_statement_headline_rejected():
    # #112: a statement + Taip/Ne makes "Taip" meaningless.
    cand = make(question_lt="LeBrono Jameso sezonas Filadelfijos „76ers“ klube")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert fixed is None and "question" in reason


def test_multi_title_headline_still_allowed():
    # Titles remain legal for multi-outcome markets.
    cand = make(question_lt="Naujas Palangos meras", market_type="multi",
                options_lt=["A", "B", "C"], probabilities=[0.4, 0.35, 0.25])
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None and fixed is not None


def test_clean_title_headline_passes():
    # Polymarket-style title, no date, no source — must pass.
    cand = make(question_lt="Naujas Palangos meras",
                market_type="multi",
                options_lt=["Kandidatas A", "Kandidatas B", "Kandidatas C"],
                probabilities=[0.4, 0.35, 0.25],
                resolve_by="2026-09-01")
    fixed, reason = validate.validate_candidate(cand, TODAY)
    assert reason is None and fixed is not None


def test_pagrindinis_prizas_is_not_flagged():
    # "pagrindinis" near a non-stance noun must stay legal.
    assert validate.lint_unresolvable("Ar pagrindinis festivalio prizas atiteks X?", ["Taip", "Ne"]) == []


def test_duplicate_detected_despite_wording():
    existing = ["Ar Žalgiris liepos 20 d. laimės LKL rungtynes prieš Rytą?"]
    assert validate.is_duplicate("Ar rungtynes prieš Rytą liepos 20 d. laimės Žalgiris?", existing)
    assert validate.is_duplicate("Ar Vilniuje liepą bus 35 laipsniai karščio?", existing) is None
