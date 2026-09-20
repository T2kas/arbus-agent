"""Composing a stored candidate into an app-ready spec (pure assembly)."""

import json

from arbus import compose


def _composed(**kw):
    base = dict(title="Ar X įvyks?", subtitle="2026 m.", category="sportas",
                rules="Sprendžiama pagal oficialų šaltinį.", context="Kontekstas.",
                options=[compose.ComposedOption(label="Taip", probability=60),
                         compose.ComposedOption(label="Ne", probability=40)],
                closes_at="2026-10-01T23:45:00+03:00", still_open=True)
    base.update(kw)
    return compose.ComposedMarket(**base)


def _cand(**kw):
    base = dict(question="Ar X įvyks?", options=["Taip", "Ne"], probabilities=[0.6, 0.4],
                category="sports", resolve_by="2026-10-05", resolution_hint="",
                sources=[], image_url="", image_source="")
    base.update(kw)
    return base


def test_build_spec_probabilities_sum_to_100():
    spec = compose.build_spec(_composed(), _cand(), liquidity=50000, image_url="")
    assert sum(o["probability"] for o in spec["options"]) == 100
    assert spec["liquidity"] == 50000


def test_build_spec_falls_back_to_candidate_options_when_model_gives_none():
    spec = compose.build_spec(_composed(options=[]), _cand(), liquidity=1000, image_url="")
    assert [o["label"] for o in spec["options"]] == ["Taip", "Ne"]
    assert sum(o["probability"] for o in spec["options"]) == 100


def test_category_kept_when_valid_else_mapped_from_candidate():
    assert compose.build_spec(_composed(category="sportas"), _cand(),
                              liquidity=1000, image_url="")["category"] == "sportas"
    # invalid model category → mapped from the candidate's english slug
    assert compose.build_spec(_composed(category="???"), _cand(category="economics"),
                              liquidity=1000, image_url="")["category"] == "ekonomika"


def test_closes_at_fallback_uses_resolve_by_when_unparseable():
    spec = compose.build_spec(_composed(closes_at="not-a-date"),
                              _cand(resolve_by="2026-10-05"), liquidity=1000, image_url="")
    assert spec["closes_at"].startswith("2026-10-05T23:45")


def test_closes_at_kept_when_valid():
    spec = compose.build_spec(_composed(closes_at="2026-10-01T20:00:00+03:00"),
                              _cand(), liquidity=1000, image_url="")
    assert spec["closes_at"].startswith("2026-10-01T20:00")


def test_candidate_summary_parses_json_columns():
    row = {"question_lt": "Ar X?", "options_json": json.dumps(["Taip", "Ne"]),
           "probabilities_json": json.dumps([0.7, 0.3]), "category": "sports",
           "resolve_by": "2026-10-05", "resolution_hint_lt": "hint",
           "sources_json": json.dumps(["https://a", "https://b"]),
           "image_url": "https://img", "image_source": "https://a"}
    cand = compose.candidate_summary(row)
    assert cand["options"] == ["Taip", "Ne"] and cand["sources"] == ["https://a", "https://b"]
    assert cand["image_url"] == "https://img"
