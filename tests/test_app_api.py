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


def test_price_moves_use_the_full_swing_inside_the_window():
    history = [
        {"option_id": "o1", "price": 0.30, "created_at": _at(1)},
        {"option_id": "o1", "price": 0.55, "created_at": _at(4)},   # peak
        {"option_id": "o1", "price": 0.31, "created_at": _at(9)},   # window opens at 0.31
    ]
    moves = app.price_moves(history, {"o1": "m1"}, window_minutes=10, now=NOW)
    assert round(moves["m1"], 2) == 0.25


def test_price_moves_measure_a_jump_from_before_the_window():
    """The user's case: 15% before the window, jumped to 62% one minute ago.
    Only the 62% row sits inside the window; the 15% is the entry baseline, so
    the swing is 0.47 — not the 0.0 that within-window min/max would give."""
    history = [
        {"option_id": "o1", "market_id": "m1", "price": 0.62, "created_at": _at(1)},
        {"option_id": "o1", "market_id": "m1", "price": 0.15, "created_at": _at(40)},
    ]
    moves = app.price_moves(history, window_minutes=10, now=NOW)
    assert round(moves["m1"], 2) == 0.47


def test_stable_binary_market_is_not_a_move():
    """TAIP 70 / NE 30, dead flat: the 0.40 spread between the two options must
    not read as movement. Each option's own swing is 0, so the market's is 0."""
    history = [
        {"option_id": "yes", "market_id": "m1", "price": 0.70, "created_at": _at(1)},
        {"option_id": "no", "market_id": "m1", "price": 0.30, "created_at": _at(1)},
        {"option_id": "yes", "market_id": "m1", "price": 0.70, "created_at": _at(6)},
        {"option_id": "no", "market_id": "m1", "price": 0.30, "created_at": _at(6)},
    ]
    moves = app.price_moves(history, window_minutes=10, now=NOW)
    assert moves["m1"] == 0.0


def test_single_option_swing_counts_even_in_a_binary_market():
    """A real 20pp push on the TAIP side still trips, unaffected by the NE side."""
    history = [
        {"option_id": "yes", "market_id": "m1", "price": 0.70, "created_at": _at(1)},
        {"option_id": "no", "market_id": "m1", "price": 0.30, "created_at": _at(1)},
        {"option_id": "yes", "market_id": "m1", "price": 0.50, "created_at": _at(6)},
        {"option_id": "no", "market_id": "m1", "price": 0.50, "created_at": _at(6)},
    ]
    moves = app.price_moves(history, window_minutes=10, now=NOW)
    assert round(moves["m1"], 2) == 0.20


def test_price_moves_ignores_a_jump_already_acted_on():
    """After the breaker closed on the 15%->62% jump, a reopen must not re-trip:
    with the watermark set just after that jump, the old rows are baseline only,
    so the market shows no fresh move."""
    history = [
        {"option_id": "o1", "market_id": "m1", "price": 0.62, "created_at": _at(6)},
        {"option_id": "o1", "market_id": "m1", "price": 0.15, "created_at": _at(9)},
    ]
    since = {"m1": NOW - timedelta(minutes=5)}     # we acted 5 min ago
    moves = app.price_moves(history, window_minutes=10, now=NOW, since=since)
    assert moves.get("m1", 0.0) == 0.0


def test_price_moves_trips_on_a_fresh_jump_after_the_watermark():
    """A new 20pp jump AFTER the watermark still trips — reopen + new movement."""
    history = [
        {"option_id": "o1", "market_id": "m1", "price": 0.85, "created_at": _at(1)},  # fresh
        {"option_id": "o1", "market_id": "m1", "price": 0.62, "created_at": _at(6)},  # acted
        {"option_id": "o1", "market_id": "m1", "price": 0.15, "created_at": _at(9)},  # acted
    ]
    since = {"m1": NOW - timedelta(minutes=5)}     # acted 5 min ago (after the 62% row)
    moves = app.price_moves(history, window_minutes=10, now=NOW, since=since)
    assert round(moves["m1"], 2) == 0.23           # 62% baseline -> 85%, not 15%->85%


def test_option_windows_start_from_the_watermark_after_a_reopen():
    """The reopen alert shows the fresh jump (62%->85%), not the old 15%->85%."""
    history = [
        {"option_id": "o1", "market_id": "m1", "probability": 85, "created_at": _at(1)},
        {"option_id": "o1", "market_id": "m1", "probability": 62, "created_at": _at(6)},
        {"option_id": "o1", "market_id": "m1", "probability": 15, "created_at": _at(9)},
    ]
    since = {"m1": NOW - timedelta(minutes=5)}
    w = app.option_windows(history, window_minutes=10, now=NOW, since=since)
    assert w["o1"] == (0.62, 0.85)


def test_option_windows_report_start_and_end_per_option():
    """For the alert: TAIP went 60%→20%, NE 40%→80% inside the window."""
    history = [                                       # newest-first
        {"option_id": "yes", "probability": 20, "created_at": _at(1)},   # end
        {"option_id": "no", "probability": 80, "created_at": _at(1)},
        {"option_id": "yes", "probability": 60, "created_at": _at(8)},   # start
        {"option_id": "no", "probability": 40, "created_at": _at(8)},
    ]
    w = app.option_windows(history, window_minutes=10, now=NOW)
    assert w["yes"] == (0.60, 0.20) and w["no"] == (0.40, 0.80)


def test_option_windows_start_from_the_pre_window_price():
    """A jump just before the run shows 15%→62%, not 62%→62%: the start is the
    last price from before the window, not the first row inside it."""
    history = [
        {"option_id": "yes", "probability": 62, "created_at": _at(1)},
        {"option_id": "yes", "probability": 15, "created_at": _at(40)},  # before window
    ]
    w = app.option_windows(history, window_minutes=10, now=NOW)
    assert w["yes"] == (0.15, 0.62)


def test_format_option_moves_reads_from_the_yes_side():
    market = {"market_options": [
        {"id": "yes", "label": "TAIP", "sort_order": 0},
        {"id": "no", "label": "NE", "sort_order": 1}]}
    lines = app.option_move_lines(market, {"yes": (0.60, 0.20), "no": (0.40, 0.80)})
    assert app.format_option_moves(lines) == "TAIP 60%→20% · NE 40%→80%"


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


def test_resolved_market_settlement_jump_is_not_a_candidate(monkeypatch):
    """When a market resolves the price snaps to 100/0 — a 100pp 'move'. That is
    the settlement, not suspicious flow, so a resolved market never appears."""
    markets = [{"id": "m1", "question": "Ar X?", "status": "resolved",
                "market_options": [{"id": "o1"}, {"id": "o2"}]}]
    history = [{"option_id": "o1", "market_id": "m1", "price": 1.0, "created_at": _at(1)},
               {"option_id": "o1", "market_id": "m1", "price": 0.30, "created_at": _at(5)}]
    monkeypatch.setattr(app, "markets", lambda *a, **k: (markets, ""))
    monkeypatch.setattr(app, "price_history", lambda *a, **k: (history, ""))
    monkeypatch.setattr(app, "recent_trades", lambda *a, **k: (
        [{"market_id": "m1", "user_id": u, "created_at": _at(2)} for u in "abc"], ""))
    rows, error = app.breaker_candidates(window_minutes=10, now=NOW)
    assert error == "" and rows == []          # resolved market filtered out entirely


def test_frozen_market_without_a_proposal_becomes_a_check_item(monkeypatch):
    """A market frozen in the app (no proposal row) must still trigger the AI
    check — the freeze itself is the trigger, not only the proposals table."""
    from arbus import aicheck
    frozen = [{"id": "m9", "status": "užšaldyta", "question": "Ar Y?",
               "market_options": [{"id": "yes", "label": "TAIP"}]}]
    monkeypatch.setattr(app, "markets", lambda *a, **k: (frozen, ""))
    items, err = aicheck.pending_frozen_proposals(set(), limit=50)
    assert err == "" and len(items) == 1
    assert items[0]["market"]["id"] == "m9"
    assert items[0]["proposal"] == {"market_id": "m9", "frozen_status": "užšaldyta"}


def test_frozen_item_is_skipped_when_a_proposal_already_covers_it(monkeypatch):
    from arbus import aicheck
    frozen = [{"id": "m9", "status": "užšaldyta"}]
    monkeypatch.setattr(app, "markets", lambda *a, **k: (frozen, ""))
    items, _ = aicheck.pending_frozen_proposals({"m9"}, limit=50)
    assert items == []                       # proposal takes priority for that market


def test_proposal_key_distinguishes_proposals_from_freezes():
    from arbus import main
    assert main._proposal_key({"id": 7, "created_at": "t1"}) == "7:t1"
    # No proposal id: keyed by market + frozen status, so a re-freeze re-checks.
    assert main._proposal_key({"market_id": "m9", "frozen_status": "užšaldyta"}) \
        == "frozen:m9:užšaldyta"


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
    ok, detail = app.freeze_market("m1")
    assert ok is False and "403" in detail     # anon key can read, not write


def test_freeze_calls_the_admin_rpc_with_the_market_id(monkeypatch):
    """Freezing hits the app's rpc/admin_freeze_market with the market id and the
    privileged write key, not a raw table PATCH."""
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(config, "ARBUS_WRITE_KEY", "service-key")
    monkeypatch.setattr(config, "APP_FREEZE_RPC", "admin_freeze_market")
    monkeypatch.setattr(config, "APP_FREEZE_RPC_PARAM", "market_id")
    seen = {}

    def fake_request(method, url, **k):
        seen["method"], seen["url"] = method, url
        seen["json"], seen["headers"] = k.get("json"), k.get("headers")
        return _Resp([{"ok": True}], 200)

    monkeypatch.setattr(requests, "request", fake_request)
    ok, detail = app.freeze_market("m-42")
    assert ok is True
    assert seen["method"] == "POST" and seen["url"].endswith("/rpc/admin_freeze_market")
    assert seen["json"] == {"market_id": "m-42"}
    assert seen["headers"]["Authorization"] == "Bearer service-key"


def test_freeze_tries_other_param_names_when_market_id_is_wrong(monkeypatch):
    """Live: admin_freeze_market has a param that is NOT `market_id`, so the
    first call returns PGRST202. The next common name must be tried instead of
    giving up."""
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(config, "APP_FREEZE_RPC_PARAM", "market_id")
    bodies = []

    def fake_request(method, url, **k):
        body = k.get("json")
        bodies.append(body)
        if "market_id" in body:                      # wrong name → PGRST202/404
            return _Resp({"code": "PGRST202"}, 404, "PGRST202: no matches found")
        return _Resp([{"ok": True}], 200)            # p_market_id accepted

    monkeypatch.setattr(requests, "request", fake_request)
    ok, detail = app.freeze_market("m-9")
    assert ok is True and "p_market_id" in detail
    assert bodies[0] == {"market_id": "m-9"} and bodies[1] == {"p_market_id": "m-9"}


def test_freeze_stops_on_a_real_error_not_a_param_mismatch(monkeypatch):
    """A 403 (no permission) is not a param problem — do not try every name, just
    report it. With no write key there is no direct-write fallback either."""
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(config, "ARBUS_WRITE_KEY", "")
    calls = []
    monkeypatch.setattr(requests, "request",
                        lambda *a, **k: calls.append(1) or _Resp(None, 403, "RLS"))
    ok, detail = app.freeze_market("m-1")
    assert ok is False and "403" in detail and len(calls) == 1   # tried once


def test_freeze_falls_back_to_a_direct_status_write_when_rpc_rejects_backend(monkeypatch):
    """Live: admin_freeze_market raises 'not authenticated' because it checks a
    user uid the service role does not have. With the service_role key we can
    halt trading by writing the status straight to the table (bypasses RLS)."""
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(config, "ARBUS_WRITE_KEY", "service-key")
    monkeypatch.setattr(config, "APP_FREEZE_STATUS", "paused")
    seen = []

    def fake_request(method, url, **k):
        seen.append((method, url))
        if "/rpc/" in url:
            return _Resp({"code": "P0001"}, 400, "P0001: not authenticated")
        return _Resp([{"id": "m-1", "status": "paused"}], 200)   # direct PATCH ok

    monkeypatch.setattr(requests, "request", fake_request)
    ok, detail = app.freeze_market("m-1")
    assert ok is True and "status=paused" in detail
    assert any(m == "PATCH" and "markets?id=eq.m-1" in u for m, u in seen)


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


def test_check_does_not_skip_a_market_it_checked_before(monkeypatch, tmp_path):
    """`arbus check` is manual and deliberate — it must always show what is
    frozen now, not go quiet because a previous run recorded the market. That
    silence was the "arbus app shows it closed, but check says nothing" bug."""
    from arbus import aicheck, store

    _rows(monkeypatch, [
        {"id": "m1", "title": "Ar OG VERSION koncertuos?", "status": "closed",
         "closes_at": "2030-01-01"},
    ])
    conn = store.connect(str(tmp_path / "t.db"))

    first, _ = aicheck.pending_app_markets(conn)
    assert [app.market_id_of(r) for r in first] == ["m1"]

    # record a check, exactly as review_app_market would
    conn.execute("INSERT OR REPLACE INTO app_checks (app_market_id, checked_at) "
                 "VALUES ('m1', '2026-07-27T00:00:00+00:00')")
    conn.commit()

    again, _ = aicheck.pending_app_markets(conn)
    assert [app.market_id_of(r) for r in again] == ["m1"]   # still checked


# ── the app's REAL schema (from `arbus app --schema`) ───────────────────────
# markets: title, closes_at, rules, volume_credits, status, market_options, ...
# option_price_history: market_id, option_id, probability, created_at
# admin_recent_trades: amount_credits, username, market_title, side, created_at
# admin_list_profiles: credits_balance, username, display_name, ...

def test_real_market_fields_are_read():
    market = {"id": "m1", "title": "Ar nedarbas viršys 7 %?", "closes_at": "2026-10-01",
              "rules": "Pagal Statistiką.", "volume_credits": 18000, "status": "open",
              "market_options": [{"id": "o1", "label": "Taip"}]}
    assert app.question_of(market) == "Ar nedarbas viršys 7 %?"
    assert app.volume_of(market) == 18000.0
    assert app.is_open(market) and not app.is_frozen(market)


def test_real_price_history_uses_probability_and_market_id():
    # Live-confirmed scale: option_price_history.probability is 0-100, not 0-1
    # (real pairs summed to exactly 100 — 55.0/45.0, 44.23/55.77, 60/40). Values
    # here are already <=1 to check the pass-through path specifically.
    history = [
        {"market_id": "m1", "option_id": "o1", "probability": 0.30, "created_at": _at(1)},
        {"market_id": "m1", "option_id": "o1", "probability": 0.62, "created_at": _at(5)},
    ]
    moves = app.price_moves(history, window_minutes=10, now=NOW)   # no option map needed
    assert round(moves["m1"], 2) == 0.32
    assert app.latest_prices(history) == {"o1": 0.30}              # newest first


def test_real_app_scale_is_0_to_100_not_a_fraction(monkeypatch):
    """The actual bug: a market's two options were {'probability': 55.0} and
    {'probability': 45.0} live — summing to 100. Read as a raw fraction, that
    made calibration report a live price of "5500%", and would make the
    circuit breaker's 0.15 (15-point) threshold trip on almost any nonzero
    move, since even a 1-point real swing (1.0 on this scale) already clears
    0.15 taken literally."""
    history = [
        {"market_id": "m1", "option_id": "o1", "probability": 45.0, "created_at": _at(1)},
        {"market_id": "m1", "option_id": "o1", "probability": 55.0, "created_at": _at(5)},
    ]
    assert app.latest_prices(history) == {"o1": 0.45}       # normalized to a fraction
    moves = app.price_moves(history, window_minutes=10, now=NOW)
    assert round(moves["m1"], 2) == 0.10                    # a real 10pp move, not 10.0


def test_as_fraction_passes_through_values_already_in_range():
    assert app._as_fraction(0.42) == 0.42
    assert app._as_fraction(1.0) == 1.0                     # boundary: a sure thing
    assert app._as_fraction(None) is None
    assert app._as_fraction("not a number") is None


def test_real_trades_key_by_title_and_use_amount_credits():
    trades = [
        {"market_title": "Ar Žalgiris laimės?", "username": "a",
         "amount_credits": 9000, "side": "buy", "created_at": _at(60)},
        {"market_title": "Ar Žalgiris laimės?", "username": "b",
         "amount_credits": 8000, "side": "sell", "created_at": _at(120)},
        {"market_title": "Ar Žalgiris laimės?", "username": "a",
         "amount_credits": 100, "side": "buy", "created_at": _at(180)},
    ]
    stats = app.trade_stats(trades, days=7, now=NOW)
    key = "ar žalgiris laimės?"
    assert stats[key] == {"trades": 3, "users": 2, "volume": 17100.0}

    market = {"id": "m9", "title": "Ar Žalgiris laimės?"}
    assert app.market_stat(stats, market)["users"] == 2   # matched by title, not id


def test_breaker_joins_title_keyed_trades_to_id_keyed_prices(monkeypatch):
    """The real feeds key differently — prices by market_id, trades by title.
    The breaker must still line them up on the same market."""
    market = {"id": "m1", "title": "Ar X laimės?"}
    monkeypatch.setattr(app, "markets", lambda *a, **k: ([market], ""))
    monkeypatch.setattr(app, "price_history", lambda *a, **k: (
        [{"market_id": "m1", "option_id": "o1", "probability": 0.20, "created_at": _at(1)},
         {"market_id": "m1", "option_id": "o1", "probability": 0.55, "created_at": _at(5)}], ""))
    monkeypatch.setattr(app, "recent_trades", lambda *a, **k: (
        [{"market_title": "Ar X laimės?", "username": u, "created_at": _at(2)}
         for u in ("a", "b", "c")], ""))
    rows, _ = app.breaker_candidates(window_minutes=10, now=NOW)
    assert rows[0]["users"] == 3 and rows[0]["tripped"] is True


def test_real_profile_balance_is_read():
    assert app.balance_of({"username": "x", "credits_balance": 1250}) == 1250.0


# ── freeze uses the privileged write key; env helpers tolerate empty vars ─────

def test_headers_default_to_the_anon_key(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_KEY", "anon-key")
    monkeypatch.setattr(config, "ARBUS_API_URL",
                        "https://x.supabase.co/rest/v1/markets?select=*")
    h = app.headers()
    assert h["Authorization"] == "Bearer anon-key"


def test_headers_use_the_write_key_when_given(monkeypatch):
    """Freezing passes the service_role key so RLS accepts the write, without
    exposing it for ordinary reads."""
    monkeypatch.setattr(config, "ARBUS_API_KEY", "anon-key")
    monkeypatch.setattr(config, "ARBUS_API_URL",
                        "https://x.supabase.co/rest/v1/markets?select=*")
    h = app.headers("service-key")
    assert h["Authorization"] == "Bearer service-key" and h["apikey"] == "service-key"


def test_env_int_and_float_fall_back_on_empty_or_bad(monkeypatch):
    """GitHub Actions passes an unset `${{ vars.X }}` as '' — that must not crash
    config import; it falls back to the default."""
    monkeypatch.setenv("CB_TEST_INT", "")
    assert config._env_int("CB_TEST_INT", 10) == 10
    monkeypatch.setenv("CB_TEST_INT", "7")
    assert config._env_int("CB_TEST_INT", 10) == 7
    monkeypatch.setenv("CB_TEST_FLOAT", "not-a-number")
    assert config._env_float("CB_TEST_FLOAT", 0.20) == 0.20
    monkeypatch.setenv("CB_TEST_FLOAT", "0.35")
    assert config._env_float("CB_TEST_FLOAT", 0.20) == 0.35


def test_option_label_resolves_the_proposed_option():
    """A proposal cites proposed_option_id; the alert needs the human label."""
    market = {"market_options": [{"id": "a", "label": "TAIP"},
                                 {"id": "b", "label": "NE"}]}
    assert app.option_label(market, "b") == "NE"
    assert app.option_label(market, "zzz") == "zzz"      # unknown id passes through
