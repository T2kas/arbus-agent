"""Reading the Arbus app's own markets (Supabase/PostgREST or any JSON API)."""

import pytest
import requests

from arbus import config, publish

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
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    rows, error = publish.fetch_app_markets()
    assert error == "" and len(rows) == 3
    assert publish.question_of(rows[0]) == "Ar nedarbas viršys 7 %?"
    assert publish.question_of(rows[1]) == "Naujas Palangos meras"
    assert publish.question_of(rows[2]) == ""       # unknown shape, not a crash


def test_app_errors_never_break_a_batch(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)

    def boom(*a, **k):
        raise requests.RequestException("connection reset")

    monkeypatch.setattr(requests, "get", boom)
    assert publish.app_questions() == []            # logged, not raised

    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(None, 401, "bad key"))
    assert publish.app_questions() == []


def test_app_questions_feed_the_duplicate_check(monkeypatch):
    monkeypatch.setattr(config, "ARBUS_API_URL", SUPABASE)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(
        [{"question_lt": "Ar Žalgiris pateks į kitą etapą?"}]))
    live = publish.app_questions()
    assert live == ["Ar Žalgiris pateks į kitą etapą?"]
    from arbus import validate
    assert validate.is_duplicate("Ar į kitą etapą pateks Žalgiris?", live)


@pytest.mark.parametrize("payload", [{"data": [{"question": "a"}]},
                                     {"markets": [{"question": "a"}]}])
def test_wrapped_list_responses_are_unwrapped(monkeypatch, payload):
    monkeypatch.setattr(config, "ARBUS_API_URL", "https://api.arbus.lt/markets")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(payload))
    rows, error = publish.fetch_app_markets()
    assert error == "" and rows == [{"question": "a"}]
