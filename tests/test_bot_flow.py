"""The Telegram approve-one-idea → upload state machine (offline, mocked I/O)."""

import pytest

from arbus import bot, compose


@pytest.fixture
def wired(monkeypatch):
    sent: list[str] = []
    created: list[dict] = []
    monkeypatch.setattr(bot, "_send", lambda token, chat, text: sent.append(text))
    # a fixed compose result — no LLM, no network
    composed = compose.ComposedMarket(
        title="Ar X įvyks iki 2026 m. spalio?", subtitle="2026 m.", category="sportas",
        rules="Sprendžiama pagal oficialų šaltinį (LT laiku).", context="Kontekstas.",
        options=[compose.ComposedOption(label="Taip", probability=55),
                 compose.ComposedOption(label="Ne", probability=45)],
        closes_at="2026-10-01T23:45:00+03:00", still_open=True)
    monkeypatch.setattr(bot.compose, "compose",
                        lambda cand, today=None: (composed, {"cost_eur": 0.07,
                                                             "model": "m", "provider": "p"}))
    monkeypatch.setattr(bot.store, "connect", lambda: None)
    monkeypatch.setattr(bot.store, "get_market", lambda conn, mid: {
        "status": "candidate", "question_lt": "Ar X įvyks?",
        "options_json": '["Taip","Ne"]', "probabilities_json": "[0.55,0.45]",
        "category": "sports", "resolve_by": "2026-10-05", "resolution_hint_lt": "",
        "sources_json": '["https://a"]', "image_url": "", "image_source": ""})
    monkeypatch.setattr(bot.images, "image_for_sources",
                        lambda srcs: ("https://img/auto.jpg", "https://a"))
    monkeypatch.setattr(bot.app_api, "create_market",
                        lambda spec: (created.append(spec), (True, "mkt-123"))[1])
    monkeypatch.setattr(bot.config, "ARBUS_WRITE_KEY", "svc")
    bot.SESSIONS.clear()
    return sent, created


def test_full_add_flow_uploads_after_confirm(wired):
    sent, created = wired
    tok, chat = "t", "42"

    bot._handle(tok, chat, {"text": "/pridėti 7"})
    assert bot.SESSIONS[chat]["step"] == "liquidity"

    bot._handle(tok, chat, {"text": "50000"})
    assert bot.SESSIONS[chat]["step"] == "image"
    assert bot.SESSIONS[chat]["liquidity"] == 50000

    bot._handle(tok, chat, {"text": "auto"})
    assert bot.SESSIONS[chat]["step"] == "confirm"
    spec = bot.SESSIONS[chat]["spec"]
    assert spec["image_url"] == "https://img/auto.jpg"
    assert sum(o["probability"] for o in spec["options"]) == 100

    bot._handle(tok, chat, {"text": "taip"})
    assert len(created) == 1
    assert created[0]["title"].startswith("Ar X")
    assert created[0]["liquidity"] == 50000
    assert chat not in bot.SESSIONS                     # session cleared after upload


def test_image_url_reply_is_used(wired):
    sent, created = wired
    tok, chat = "t", "42"
    bot._handle(tok, chat, {"text": "/pridėti 7"})
    bot._handle(tok, chat, {"text": "50000"})
    bot._handle(tok, chat, {"text": "https://my/pic.png"})
    assert bot.SESSIONS[chat]["spec"]["image_url"] == "https://my/pic.png"


def test_cancel_stops_the_flow(wired):
    sent, created = wired
    tok, chat = "t", "42"
    bot._handle(tok, chat, {"text": "/pridėti 7"})
    bot._handle(tok, chat, {"text": "/atšaukti"})
    assert chat not in bot.SESSIONS
    bot._handle(tok, chat, {"text": "50000"})           # stray reply ignored now
    assert created == []


def test_confirm_no_does_not_upload(wired):
    sent, created = wired
    tok, chat = "t", "42"
    bot._handle(tok, chat, {"text": "/pridėti 7"})
    bot._handle(tok, chat, {"text": "50000"})
    bot._handle(tok, chat, {"text": "be"})
    bot._handle(tok, chat, {"text": "ne"})
    assert created == [] and chat not in bot.SESSIONS


def test_photo_at_image_step_is_rejected_with_hint(wired):
    sent, created = wired
    tok, chat = "t", "42"
    bot._handle(tok, chat, {"text": "/pridėti 7"})
    bot._handle(tok, chat, {"text": "50000"})
    bot._handle(tok, chat, {"photo": [{"file_id": "abc"}]})
    assert bot.SESSIONS[chat]["step"] == "image"          # still waiting
    assert any("URL" in m for m in sent)


def test_rejected_candidate_is_not_launched(wired, monkeypatch):
    sent, created = wired
    monkeypatch.setattr(bot.store, "get_market", lambda conn, mid: {
        "status": "rejected", "question_lt": "x", "options_json": "[]",
        "probabilities_json": "[]", "category": "", "resolve_by": "",
        "resolution_hint_lt": "", "sources_json": "[]", "image_url": "", "image_source": ""})
    bot._handle("t", "42", {"text": "/pridėti 7"})
    assert "42" not in bot.SESSIONS and any("atmesta" in m for m in sent)
