"""Offline tests for the app-publishing payload."""

import json
import sys
import types

sys.modules.setdefault("feedparser", types.ModuleType("feedparser"))

from arbus import publish, store  # noqa: E402
from arbus.schemas import Candidate  # noqa: E402


def _row(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    cand = Candidate(
        question_lt="Ar nedarbo lygis viršys 7 % iki spalio?",
        market_type="binary", options_lt=["Taip", "Ne"], probabilities=[0.4, 0.6],
        category="ekonomika", resolve_by="2026-10-01", duration_class="long",
        resolution_hint_lt="Pagal Statistikos departamento duomenis.",
        sources=["https://osp.stat.gov.lt/x"], rationale_en="live topic",
        image_url="https://lrt.lt/a.jpg", image_source="https://lrt.lt/straipsnis",
    )
    market_id = store.insert_candidate(conn, cand, "b1", "candidate")
    conn.commit()
    return conn, store.get_market(conn, market_id)


def test_payload_shape_is_complete(tmp_path):
    _conn, row = _row(tmp_path)
    p = publish.market_payload(row)
    assert p["external_id"].startswith("arbus-")
    assert p["question"].startswith("Ar nedarbo")
    assert p["options"] == ["Taip", "Ne"]
    assert p["probabilities"] == [0.4, 0.6]
    assert p["image_url"] == "https://lrt.lt/a.jpg"
    assert p["language"] == "lt"
    assert p["resolve_by"] == "2026-10-01"
    json.dumps(p)  # must be JSON-serializable


def test_publish_without_url_is_refused(tmp_path, monkeypatch):
    _conn, row = _row(tmp_path)
    monkeypatch.setattr(publish.config, "ARBUS_API_URL", "")
    ok, detail = publish.publish_market(row)
    assert ok is False and "ARBUS_API_URL" in detail


def test_publish_sends_bearer_token(tmp_path, monkeypatch):
    _conn, row = _row(tmp_path)
    seen = {}

    class _R:
        status_code = 201
        text = "ok"

    def fake_post(url, headers=None, json=None, timeout=None):
        seen.update(url=url, headers=headers, body=json)
        return _R()

    monkeypatch.setattr(publish.config, "ARBUS_API_URL", "https://api.arbus.lt/markets")
    monkeypatch.setattr(publish.config, "ARBUS_API_KEY", "secret")
    monkeypatch.setattr(publish.requests, "post", fake_post)
    ok, detail = publish.publish_market(row)
    assert ok and "201" in detail
    assert seen["headers"]["Authorization"] == "Bearer secret"
    assert seen["body"]["question"].startswith("Ar nedarbo")


def test_http_error_reported_not_raised(tmp_path, monkeypatch):
    _conn, row = _row(tmp_path)

    class _R:
        status_code = 422
        text = "bad payload"

    monkeypatch.setattr(publish.config, "ARBUS_API_URL", "https://api.arbus.lt/markets")
    monkeypatch.setattr(publish.requests, "post", lambda *a, **k: _R())
    ok, detail = publish.publish_market(row)
    assert ok is False and "422" in detail


def test_mark_published_records_timestamp(tmp_path):
    conn, row = _row(tmp_path)
    publish.mark_published(conn, row["id"], "HTTP 201")
    conn.commit()
    assert store.get_market(conn, row["id"])["published_at"]
