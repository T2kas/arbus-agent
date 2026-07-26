"""Reading the Arbus app's own markets (Supabase/PostgREST or any JSON API)."""

from datetime import date, datetime, timedelta, timezone

import pytest
import requests

from arbus import app, config, publish

SUPABASE = ("https://crwtwtwljqypvgvvfmyo.supabase.co/rest/v1/markets"
            "?select=*,market_options!market_id(*)&order=created_at.desc")


class _Resp:
    def __init__(self, payload, status=200, text=""):
        self._payload, self.status_code, self.text = payload, status, text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_supabase_gets_the_key_in_both_headers(monkeypatch):
    """PostgREST rejects a bearer token without `apikey` — and the 401 reads
    like a wrong key, which is a long detour to debug."""
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(config, "ARBUS_API_KEY", "anon-key")
    headers = publish._headers()
    assert headers["apikey"] == "anon-key"
    assert headers["Authorization"] == "Bearer anon-key"


def test_plain_api_does_not_get_supabase_headers(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", "https://api.arbus.lt/markets")
    monkeypatch.setattr(config, "ARBUS_API_KEY", "k")
    assert "apikey" not in publish._headers()


def test_query_params_are_appended_to_a_url_that_already_has_some(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    assert publish._url_with("limit=5").endswith("&limit=5")
    monkeypatch.setattr(config, "ARBUS_API_URL", "https://api.arbus.lt/markets")
    assert publish._url_with("limit=5").endswith("?limit=5")


def test_reads_markets_and_finds_the_question_whatever_it_is_called(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    payload = [{"question": "Ar nedarbas viršys 7 %?", "created_at": "2026-07-20"},
               {"title": "Naujas Palangos meras"},
               {"id": 3}]
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(payload))
    rows, error = publish.fetch_app_markets()
    assert error == "" and len(rows) == 3
    assert publish.question_of(rows[0]) == "Ar nedarbas viršys 7 %?"
    assert publish.question_of(rows[1]) == "Naujas Palangos meras"
    assert publish.question_of(rows[2]) == ""       # unknown shape, not a crash


def test_app_errors_never_break_a_batch(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)

    def boom(*a, **k):
        raise requests.RequestException("connection reset")

    monkeypatch.setattr(requests, "request", boom)
    assert publish.app_questions() == []            # logged, not raised

    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(None, 401, "bad key"))
    assert publish.app_questions() == []


def test_app_questions_feed_the_duplicate_check(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(
        [{"question_lt": "Ar Žalgiris pateks į kitą etapą?"}]))
    live = publish.app_questions()
    assert live == ["Ar Žalgiris pateks į kitą etapą?"]
    from arbus import validate
    assert validate.is_duplicate("Ar į kitą etapą pateks Žalgiris?", live)


@pytest.mark.parametrize("payload", [{"data": [{"question": "a"}]},
                                     {"markets": [{"question": "a"}]}])
def test_wrapped_list_responses_are_unwrapped(monkeypatch, payload):
    monkeypatch.setattr(config, "ARBUS_API_URL", "https://api.arbus.lt/markets")
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(payload))
    rows, error = publish.fetch_app_markets()
    assert error == "" and rows == [{"question": "a"}]


# ── frozen markets in the app (the "nothing frozen" bug) ────────────────────

def _rows(monkeypatch, payload):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(config, "ARBUS_API_KEY", "k")
    monkeypatch.setattr(requests, "request", lambda *a, **k: _Resp(payload))


def test_app_paused_and_stopped_markets_are_seen_as_frozen(monkeypatch):
    _rows(monkeypatch, [
        {"id": "a", "question": "Ar A?", "status": "active"},
        {"id": "b", "question": "Ar B?", "status": "paused"},
        {"id": "c", "question": "Ar C?", "status": "Sustabdyta"},   # case-insensitive
    ])
    frozen, error = app.frozen_markets()
    assert error == ""
    assert [app.market_id_of(m) for m in frozen] == ["b", "c"]


def test_base_url_is_derived_from_the_markets_url(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    assert app.base_url() == "https://crwtwtwljqypvgvvfmyo.supabase.co/rest/v1/"
    monkeypatch.setattr(config, "ARBUS_API_URL", "https://api.arbus.lt/markets")
    assert app.base_url() == ""            # extra endpoints stay inert


# ── circuit breaker on real price/trade data ────────────────────────────────

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


def _at(minutes_ago):
    return (NOW - timedelta(minutes=minutes_ago)).isoformat()


def test_price_moves_use_the_window_and_the_full_swing():
    history = [
        {"option_id": "o1", "price": 0.30, "created_at": _at(1)},
        {"option_id": "o1", "price": 0.55, "created_at": _at(4)},   # +25pp swing
        {"option_id": "o1", "price": 0.31, "created_at": _at(9)},
        {"option_id": "o1", "price": 0.90, "created_at": _at(60)},  # outside window
    ]
    moves = app.price_moves(history, {"o1": "m1"}, window_minutes=10, now=NOW)
    assert round(moves["m1"], 2) == 0.25


def test_breaker_needs_the_users_too(monkeypatch):
    markets = [{"id": "m1", "question": "Ar X?",
                "market_options": [{"id": "o1"}, {"id": "o2"}]}]
    history = [{"option_id": "o1", "price": 0.20, "created_at": _at(1)},
               {"option_id": "o1", "price": 0.60, "created_at": _at(5)}]

    def one_whale(*a, **k):
        return [{"market_id": "m1", "user_id": "whale", "created_at": _at(2)}], ""

    def a_crowd(*a, **k):
        return ([{"market_id": "m1", "user_id": u, "created_at": _at(2)}
                 for u in ("a", "b", "c")], "")

    monkeypatch.setattr(app, "markets", lambda *a, **k: (markets, ""))
    monkeypatch.setattr(app, "price_history", lambda *a, **k: (history, ""))

    monkeypatch.setattr(app, "recent_trades", one_whale)
    rows, _ = app.breaker_candidates(window_minutes=10, now=NOW)
    assert rows[0]["users"] == 1 and rows[0]["tripped"] is False

    monkeypatch.setattr(app, "recent_trades", a_crowd)
    rows, _ = app.breaker_candidates(window_minutes=10, now=NOW)
    assert rows[0]["users"] == 3 and rows[0]["tripped"] is True


def test_missing_trades_endpoint_still_reports_moves(monkeypatch):
    monkeypatch.setattr(app, "markets", lambda *a, **k: ([{"id": "m1"}], ""))
    monkeypatch.setattr(app, "price_history", lambda *a, **k: (
        [{"option_id": "m1", "price": 0.1, "created_at": _at(1)},
         {"option_id": "m1", "price": 0.5, "created_at": _at(3)}], ""))
    monkeypatch.setattr(app, "recent_trades", lambda *a, **k: ([], "HTTP 404"))
    rows, error = app.breaker_candidates(window_minutes=10, now=NOW)
    assert error == "" and rows[0]["users"] == 0 and rows[0]["tripped"] is False


# ── the alert renders from an app record, not just a DB row ────────────────

def test_alert_renders_from_an_app_market_record():
    from arbus import notify

    text = notify.resolution_message(
        {"id": "abc", "question": "Ar nedarbas viršys 7 %?", "status": "sustabdyta",
         "resolve_by": "2026-10-01", "resolution_criteria": "Pagal Statistiką.",
         "market_options": [{"label": "Taip"}, {"label": "Ne"}]},
        None, "AI: rezultatas dar nepaskelbtas")
    assert "#abc" in text and "Taip / Ne" in text
    assert "sustabdyta" in text and "Pagal Statistiką." in text


# ── health signals (arbus stats) ────────────────────────────────────────────

def test_settled_markets_are_never_re_checked(monkeypatch):
    """`closed` is in the frozen list on purpose, so without the settled list a
    resolved market would be AI-checked and billed on every single run."""
    _rows(monkeypatch, [
        {"id": "a", "status": "closed"},
        {"id": "b", "status": "resolved"},
        {"id": "c", "status": "atšaukta"},
    ])
    frozen, _ = app.frozen_markets()
    assert [app.market_id_of(m) for m in frozen] == ["a"]


def test_trade_stats_count_volume_users_and_bets():
    trades = [
        {"market_id": "m1", "user_id": "a", "amount": 5000, "created_at": _at(60)},
        {"market_id": "m1", "user_id": "b", "amount": 12000, "created_at": _at(120)},
        {"market_id": "m1", "user_id": "a", "amount": -500, "created_at": _at(180)},
        {"market_id": "m2", "user_id": "c", "amount": 10, "created_at": _at(60 * 24 * 30)},
    ]
    stats = app.trade_stats(trades, days=7, now=NOW)
    assert stats["m1"] == {"trades": 3, "users": 2, "volume": 17500.0}
    assert "m2" not in stats                   # older than the window


def test_overdue_markets_are_the_ones_still_trading_past_their_date():
    rows = [
        {"id": "a", "status": "active", "resolve_by": "2026-07-01"},   # overdue
        {"id": "b", "status": "active", "resolve_by": "2026-12-01"},
        {"id": "c", "status": "paused", "resolve_by": "2026-07-01"},   # already stopped
        {"id": "d", "status": "active", "resolve_by": "nonsense"},
    ]
    overdue = app.overdue_markets(rows, today=date(2026, 7, 26))
    assert [r["id"] for r in overdue] == ["a"]


def test_latest_price_wins_because_history_is_newest_first():
    history = [{"option_id": "o1", "price": 0.71, "created_at": _at(1)},
               {"option_id": "o1", "price": 0.20, "created_at": _at(500)}]
    assert app.latest_prices(history) == {"o1": 0.71}


def test_freezing_reports_what_the_app_said(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(requests, "request",
                        lambda *a, **k: _Resp(None, 403, "row-level security"))
    ok, detail = app.set_status("m1", "paused")
    assert ok is False and "403" in detail     # anon key can read, not write


def test_overdue_open_markets_are_checked_too(monkeypatch, tmp_path):
    """The Sinkevičius case: decided weeks ago, nobody paused it, so nothing
    was checking it while the AMM kept trading against a known outcome."""
    from arbus import aicheck, store

    _rows(monkeypatch, [
        {"id": "old", "question": "Ar M. Sinkevičius taps premjeru?",
         "status": "active", "resolve_by": "2020-01-01"},
        {"id": "live", "question": "Ar Žalgiris laimės?", "status": "active",
         "resolve_by": "2030-01-01"},
    ])
    conn = store.connect(str(tmp_path / "t.db"))
    rows, error = aicheck.pending_app_markets(conn)
    assert error == ""
    assert [app.market_id_of(r) for r in rows] == ["old"]
