"""Offline tests for rebuilding the dedupe corpus from exports/."""

import json

from arbus import store


def _export(tmp_path, name, rows):
    d = tmp_path / "exports"
    d.mkdir(exist_ok=True)
    (d / name).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return str(d)


ROW = {
    "id": 1, "verify_verdict": "OPEN", "verify_note": "ok",
    "question_lt": "Ar nedarbo lygis viršys 7 % iki spalio?",
    "market_type": "binary", "options_lt": ["Taip", "Ne"],
    "probabilities": [0.4, 0.6], "category": "ekonomika",
    "resolve_by": "2026-10-01", "duration_class": "long",
    "resolution_hint_lt": "Pagal Statistikos departamentą.",
    "sources": ["https://osp.stat.gov.lt/x"], "rationale_en": "topical",
}


def test_imports_markets_from_exports(tmp_path):
    d = _export(tmp_path, "batch_2026-07-25T1353.json", [ROW])
    conn = store.connect()
    imported, _ = store.rebuild_from_exports(conn, d)
    assert imported == 1
    assert store.recent_questions(conn) == [ROW["question_lt"]]


def test_rerun_is_idempotent(tmp_path):
    d = _export(tmp_path, "batch_a.json", [ROW])
    conn = store.connect()
    store.rebuild_from_exports(conn, d)
    imported, skipped = store.rebuild_from_exports(conn, d)
    assert imported == 0 and skipped == 1


def test_unreadable_and_invalid_rows_are_skipped(tmp_path):
    d = _export(tmp_path, "batch_b.json", [ROW, {"question_lt": "Ar be laukų?"}])
    (tmp_path / "exports" / "batch_broken.json").write_text("{not json",
                                                            encoding="utf-8")
    conn = store.connect()
    imported, skipped = store.rebuild_from_exports(conn, d)
    assert imported == 1 and skipped == 1     # broken file ignored, bad row skipped


def test_missing_export_dir_is_harmless(tmp_path):
    conn = store.connect()
    assert store.rebuild_from_exports(conn, str(tmp_path / "nope")) == (0, 0)
