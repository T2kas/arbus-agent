"""Simulated batch runs proving themed quotas hold on the ACCEPTED batch.

The live pipeline cannot be exercised offline, so these tests drive run_batch
with a fake LLM whose behaviour is controlled per theme. That reproduces the
real failure — informative themes losing candidates at validation while culture
survives — and verifies the top-up rounds correct it.
"""

import sys
import types

sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))

import pytest  # noqa: E402

from arbus import config, pipeline  # noqa: E402
from arbus.schemas import Candidate, CandidateBatch  # noqa: E402

GOOD = {
    "market_type": "binary", "options_lt": ["Taip", "Ne"], "probabilities": [0.4, 0.6],
    "resolve_by": "2026-12-01", "duration_class": "long",
    "resolution_hint_lt": "Pagal oficialius duomenis.",
    "sources": ["https://www.lrt.lt/a"], "rationale_en": "topical",
}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Wire the pipeline to fakes: no network, no DB reuse, no images."""
    # store.connect binds its default db_path at import, so patch the call site.
    db = str(tmp_path / "t.db")
    real_connect = pipeline.store.connect
    monkeypatch.setattr(pipeline.store, "connect", lambda *a, **k: real_connect(db))
    monkeypatch.setattr(config, "MIN_RESOLVE_DATE", "")
    monkeypatch.setattr(config, "IMAGES_ENABLED", False)
    monkeypatch.setattr(config, "REPORT_DIR", str(tmp_path / "reports"))
    monkeypatch.setattr(config, "EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.setattr(pipeline.harvest, "harvest",
                        lambda *a, **k: [{"source": "LRT", "title": "Antraštė",
                                          "link": "", "published": "2026-07-24T10:00:00+00:00"}])
    monkeypatch.setattr(pipeline.harvest, "headlines_block", lambda items: "- antraštė")
    monkeypatch.setattr(pipeline.pulse, "pulse", lambda *a, **k: [])
    monkeypatch.setattr(pipeline.pulse, "pulse_block", lambda s: "")
    monkeypatch.setattr(pipeline.feedback, "load_feedback", lambda *a, **k: "")
    monkeypatch.setattr(pipeline.llm, "provider", lambda stage=None: "perplexity")
    # Verification is not under test here — accept everything.
    monkeypatch.setattr(pipeline.verify, "verify_candidates",
                        lambda cands, today, **kw: [("OPEN", "ok")] * len(cands))
    return monkeypatch


def _install_fake_llm(monkeypatch, quality):
    """quality(theme) -> True if that theme's candidates should pass validation."""
    state = {"theme": "", "calls": 0, "n": 0, "subject": 0}

    def fake_research(prompt, system, **kw):
        state["calls"] += 1
        # The chunk mandate is echoed in the prompt; recover the theme from it.
        for label, _, focus in config.DRAFT_THEMES:
            if focus[:60] in prompt:
                state["theme"] = label
                break
        marker = "DRAFT exactly "
        state["n"] = int(prompt.split(marker)[1].split()[0])
        return "drafted"

    # Questions must be genuinely distinct in BOTH subject and predicate, or the
    # dedupe gate rejects them as near-duplicates of each other and masks the
    # behaviour under test.
    SUBJECTS = ["Seimas", "Vyriausybė", "Ignitis", "Telia", "Žalgiris", "Rytas",
                "prezidentas", "Vilniaus taryba", "Maxima", "Vinted",
                "Lietuvos bankas", "LHMT", "Litgrid", "Sodra", "VMI", "LRT",
                "Panevėžys", "Šiauliai", "Alytus", "Utena"]
    PREDICATES = ["padidins mokesčius", "paskelbs naują programą",
                  "atidarys biurą Kaune", "pasirašys sutartį su tiekėju",
                  "sumažins kainas", "priims biudžeto pataisą",
                  "pradės rekonstrukciją", "paskirs naują vadovą",
                  "išleis obligacijas", "laimės konkursą",
                  "pristatys metinę ataskaitą", "uždarys filialą",
                  "įdiegs naują sistemą", "gaus ES finansavimą",
                  "surengs viešą aptarimą", "pakeis darbo tvarką",
                  "investuos į infrastruktūrą", "sustabdys projektą",
                  "paskelbs konkurso rezultatus", "pertvarkys struktūrą"]

    def fake_structure(prompt, model, **kw):
        theme, n = state["theme"], state["n"]
        cands = []
        for _ in range(n):
            i = state["subject"]
            state["subject"] += 1
            subject = SUBJECTS[i % len(SUBJECTS)]
            predicate = PREDICATES[(i // len(SUBJECTS) + i) % len(PREDICATES)]
            q = f"Ar {subject} {predicate} iki spalio?"
            fields = dict(GOOD)
            if not quality(theme):
                # Missing grounding URLs is a non-repairable rejection, so the
                # theme loses these candidates for good — exactly the situation
                # top-ups exist to correct.
                fields["sources"] = ["LRT"]
            cands.append(Candidate(question_lt=q, category=theme, **fields))
        return CandidateBatch(candidates=cands)

    monkeypatch.setattr(pipeline.llm, "research", fake_research)
    monkeypatch.setattr(pipeline.llm, "structure", fake_structure)
    return state


def test_topup_recovers_a_failing_theme(harness):
    """Geopolitics fails at first, then recovers — quota must still be met."""
    seen = {"geo_calls": 0}

    def quality(theme):
        if theme == "valstybė ir geopolitika":
            seen["geo_calls"] += 1
            return seen["geo_calls"] > 1   # first chunk fails, top-ups succeed
        return True

    _install_fake_llm(harness, quality)
    result = pipeline.run_batch(count=15)
    accepted = result.accepted_by_theme
    target = result.target_by_theme
    assert accepted["valstybė ir geopolitika"] >= target["valstybė ir geopolitika"], \
        f"top-up failed to recover: {dict(accepted)}"


def test_culture_cannot_take_over_the_batch(harness):
    """The real failure mode: only culture survives. It must not inherit the batch."""
    _install_fake_llm(harness, lambda theme: theme == "kultūra ir visuomenė")
    result = pipeline.run_batch(count=15)
    culture = result.accepted_by_theme["kultūra ir visuomenė"]
    assert culture <= result.target_by_theme["kultūra ir visuomenė"], \
        "culture exceeded its quota by absorbing other themes' share"
    assert len(result.accepted) == culture, "only culture should have survived"


def test_no_topup_when_every_theme_delivers(harness):
    state = _install_fake_llm(harness, lambda theme: True)
    result = pipeline.run_batch(count=15)
    assert sum(result.accepted_by_theme.values()) == 15
    # exactly one drafting call per planned chunk — no wasted top-up rounds
    assert state["calls"] == len(pipeline._theme_chunks(15, config.DRAFT_CHUNK_SIZE))


def test_report_records_theme_yield(harness):
    _install_fake_llm(harness, lambda theme: True)
    result = pipeline.run_batch(count=15)
    text = open(result.report_path, encoding="utf-8").read()
    assert "Yield per theme" in text
    assert "valstybė ir geopolitika" in text
