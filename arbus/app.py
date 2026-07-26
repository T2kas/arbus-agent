"""Read-only client for the Arbus app's own API (Supabase / PostgREST).

The generator's SQLite is not the truth about what users see — the app is. A
market can be published by hand, paused by an admin, or traded on heavily, and
none of that ever reaches this repo. This module is the eye: it reads markets,
their status, price history, recent trades and profiles, so the resolution
tooling can act on the real state instead of a local guess.

Everything here is **read-only and fail-safe**. Every call returns
`(data, error)` and never raises: a batch or a resolution sweep must not die
because the app is briefly unreachable.

Field names are looked up defensively (`_pick`) because the app's schema is
owned by the app team and will keep moving. `python -m arbus app --schema`
prints the columns each endpoint actually returns.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import requests

from . import config

log = logging.getLogger(__name__)

REST_MARKER = "/rest/v1/"


def configured() -> bool:
    return bool(config.ARBUS_API_URL)


def headers() -> dict[str, str]:
    """Auth headers.

    Supabase's PostgREST wants the key in BOTH `apikey` and `Authorization`;
    sending only the bearer token returns a 401 that reads like a bad key.
    """
    out = {"Content-Type": "application/json"}
    if config.ARBUS_API_KEY:
        out["Authorization"] = f"Bearer {config.ARBUS_API_KEY}"
        if "supabase.co" in config.ARBUS_API_URL:
            out["apikey"] = config.ARBUS_API_KEY
            out["Prefer"] = "return=representation"
    return out


def url_with(params: str) -> str:
    """Append query params to the configured markets URL, which already has some."""
    sep = "&" if "?" in config.ARBUS_API_URL else "?"
    return f"{config.ARBUS_API_URL}{sep}{params}"


def base_url() -> str:
    """The `.../rest/v1/` prefix, derived from the markets URL.

    Only the markets URL is configured, so every other endpoint is built from
    it. A non-PostgREST URL yields "" and the extra endpoints stay inert.
    """
    url = config.ARBUS_API_URL
    if REST_MARKER not in url:
        return ""
    return url.split(REST_MARKER)[0] + REST_MARKER


def _unwrap(data):
    if isinstance(data, dict):
        data = data.get("data") or data.get("markets") or data.get("result") or []
    return data if isinstance(data, list) else []


def _call(method: str, url: str, **kwargs) -> tuple[list[dict], str]:
    try:
        resp = requests.request(method, url, headers=headers(),
                                timeout=config.ARBUS_API_TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        return [], f"network error: {exc}"
    except Exception as exc:                      # defensive: never break a batch
        return [], f"unexpected error: {exc}"
    if resp.status_code >= 400:
        return [], f"HTTP {resp.status_code}: {resp.text[:200]}"
    try:
        return _unwrap(resp.json()), ""
    except ValueError:
        return [], f"response is not JSON: {resp.text[:120]}"


def _endpoint(path: str, query: str = "") -> tuple[list[dict], str]:
    base = base_url()
    if not base:
        return [], "ARBUS_API_URL is not a /rest/v1/ URL — extra endpoints unavailable"
    return _call("GET", f"{base}{path}" + (f"?{query}" if query else ""))


def _rpc(name: str, payload: dict | None = None) -> tuple[list[dict], str]:
    base = base_url()
    if not base:
        return [], "ARBUS_API_URL is not a /rest/v1/ URL — RPC unavailable"
    return _call("POST", f"{base}rpc/{name}", json=payload or {})


# ── field lookup: the app owns the schema, so never assume one name ─────────

def _pick(row: dict, *names, default=None):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def question_of(row: dict) -> str:
    value = _pick(row, "question_lt", "question", "title", "name", "text", default="")
    return value.strip() if isinstance(value, str) else ""


def status_of(row: dict) -> str:
    value = _pick(row, "status", "state", "market_status", default="")
    return str(value).strip().lower()


def market_id_of(row: dict) -> str:
    return str(_pick(row, "market_id", "id", default=""))


def _timestamp_of(row: dict) -> datetime | None:
    raw = _pick(row, "created_at", "timestamp", "inserted_at", "time", default="")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# ── the endpoints ───────────────────────────────────────────────────────────

def markets(limit: int = 100) -> tuple[list[dict], str]:
    if not configured():
        return [], "ARBUS_API_URL is not set"
    return _call("GET", url_with(f"limit={limit}"))


def is_frozen(row: dict) -> bool:
    """Trading stopped and nobody has been paid yet.

    The frozen list is deliberately broad (the app owns these strings and they
    vary: paused, pristabdyta, stopped, sustabdyta, closed...), so the settled
    list is what keeps an already-resolved market from being re-checked and
    re-billed on every run.
    """
    status = status_of(row)
    if status in config.APP_SETTLED_STATUSES:
        return False
    return status in config.APP_FROZEN_STATUSES


def frozen_markets(limit: int = 200) -> tuple[list[dict], str]:
    """Markets the app has paused/stopped — the ones an admin must decide.

    `arbus check` used to look only at this repo's SQLite, so a market paused
    in the app was invisible to it. The app's own status is the truth.
    """
    rows, error = markets(limit)
    if error:
        return [], error
    return [r for r in rows if is_frozen(r)], ""


def set_status(market_id: str, status: str) -> tuple[bool, str]:
    """Write a market's status back to the app (needs a service_role key).

    This is the only write in this module and it is never called automatically:
    `arbus watch --freeze` is opt-in, because stopping trading is a decision
    with money attached. With the anon key row-level security will refuse it,
    and the 401/403 is reported as-is rather than swallowed.
    """
    base = base_url()
    if not base:
        return False, "ARBUS_API_URL is not a /rest/v1/ URL"
    rows, error = _call("PATCH", f"{base}markets?id=eq.{market_id}",
                        json={"status": status})
    if error:
        return False, error
    return True, f"status set to {status}" + (f" ({len(rows)} row)" if rows else "")


def price_history(limit: int = 200) -> tuple[list[dict], str]:
    return _endpoint("option_price_history",
                     f"select=*&order=created_at.desc&limit={limit}")


def recent_trades() -> tuple[list[dict], str]:
    return _rpc("admin_recent_trades")


def profiles() -> tuple[list[dict], str]:
    return _rpc("admin_list_profiles")


# ── circuit breaker on real data ────────────────────────────────────────────

def option_to_market(market_rows: list[dict]) -> dict[str, str]:
    """{option id: market id} from the nested market_options the markets URL
    already selects — price history references options, decisions are per
    market."""
    mapping: dict[str, str] = {}
    for market in market_rows:
        mid = market_id_of(market)
        for option in (market.get("market_options") or market.get("options") or []):
            if isinstance(option, dict):
                oid = str(_pick(option, "id", "option_id", default=""))
                if oid:
                    mapping[oid] = mid
    return mapping


def price_moves(history: list[dict], option_map: dict[str, str],
                window_minutes: int = config.CB_WINDOW_MINUTES,
                now: datetime | None = None) -> dict[str, float]:
    """Largest price swing per market inside the window.

    History is per option and newest-first. The swing that matters is the whole
    range inside the window, not last-minus-first: a price pushed up and partly
    released still says someone knew something.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=window_minutes)
    per_option: dict[str, list[float]] = {}
    for row in history:
        ts = _timestamp_of(row)
        if ts is None or ts < cutoff:
            continue
        price = _pick(row, "price", "new_price", "value", "probability")
        oid = str(_pick(row, "option_id", "id", default=""))
        if price is None or not oid:
            continue
        try:
            per_option.setdefault(oid, []).append(float(price))
        except (TypeError, ValueError):
            continue

    moves: dict[str, float] = {}
    for oid, prices in per_option.items():
        mid = option_map.get(oid, oid)
        swing = max(prices) - min(prices)
        moves[mid] = max(moves.get(mid, 0.0), swing)
    return moves


def traders_per_market(trades: list[dict], window_minutes: int = config.CB_WINDOW_MINUTES,
                       now: datetime | None = None) -> dict[str, set[str]]:
    """{market id: distinct user ids who traded inside the window}."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=window_minutes)
    out: dict[str, set[str]] = {}
    for trade in trades:
        ts = _timestamp_of(trade)
        if ts is not None and ts < cutoff:
            continue
        mid = str(_pick(trade, "market_id", default=""))
        user = str(_pick(trade, "user_id", "profile_id", "username", default=""))
        if mid and user:
            out.setdefault(mid, set()).add(user)
    return out


# ── market health: dead, important, overdue, mispriced ──────────────────────

def trade_stats(trades: list[dict], days: int = config.DEAD_MARKET_DAYS,
                now: datetime | None = None) -> dict[str, dict]:
    """{market id: {trades, users, volume}} over the window."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    out: dict[str, dict] = {}
    for trade in trades:
        ts = _timestamp_of(trade)
        if ts is not None and ts < cutoff:
            continue
        mid = str(_pick(trade, "market_id", default=""))
        if not mid:
            continue
        stat = out.setdefault(mid, {"trades": 0, "users": set(), "volume": 0.0})
        stat["trades"] += 1
        user = _pick(trade, "user_id", "profile_id", "username", default="")
        if user:
            stat["users"].add(str(user))
        try:
            stat["volume"] += abs(float(_pick(trade, "amount", "arbucks", "stake",
                                              "total", "shares", default=0) or 0))
        except (TypeError, ValueError):
            pass
    return {mid: {"trades": s["trades"], "users": len(s["users"]),
                  "volume": s["volume"]} for mid, s in out.items()}


def latest_prices(history: list[dict]) -> dict[str, float]:
    """{option id: most recent price}. History arrives newest-first."""
    prices: dict[str, float] = {}
    for row in history:
        oid = str(_pick(row, "option_id", "id", default=""))
        price = _pick(row, "price", "new_price", "value", "probability")
        if oid and oid not in prices and price is not None:
            try:
                prices[oid] = float(price)
            except (TypeError, ValueError):
                continue
    return prices


def is_open(row: dict) -> bool:
    status = status_of(row)
    return (status not in config.APP_SETTLED_STATUSES
            and status not in config.APP_FROZEN_STATUSES)


def overdue_markets(market_rows: list[dict], today: date | None = None) -> list[dict]:
    """Still trading after their own resolution date.

    Under an AMM this is the most expensive state a market can be in: the
    outcome may already be public while the price has not moved, and every
    trade against that stale price is a loss taken by the house.
    """
    today = today or date.today()
    out = []
    for row in market_rows:
        if not is_open(row):
            continue
        raw = str(_pick(row, "resolve_by", "resolves_at", "closes_at",
                        "end_date", "deadline", default=""))[:10]
        try:
            if raw and date.fromisoformat(raw) < today:
                out.append(row)
        except ValueError:
            continue
    return out


def breaker_candidates(window_minutes: int = config.CB_WINDOW_MINUTES,
                       now: datetime | None = None) -> tuple[list[dict], str]:
    """Markets whose live flow trips the circuit breaker.

    This is where `resolution.circuit_breaker_tripped` finally gets real data:
    a price swing AND several distinct users behind it. Returns one dict per
    market: {market, move, users, tripped}.
    """
    from . import resolution

    market_rows, error = markets()
    if error:
        return [], error
    history, error = price_history()
    if error:
        return [], error
    trades, trade_error = recent_trades()
    if trade_error:
        log.warning("recent trades unavailable (%s) — user counts will be 0", trade_error)
        trades = []

    moves = price_moves(history, option_to_market(market_rows), window_minutes, now)
    traders = traders_per_market(trades, window_minutes, now)
    by_id = {market_id_of(m): m for m in market_rows}

    out = []
    for mid, move in moves.items():
        users = len(traders.get(mid, ()))
        out.append({
            "market": by_id.get(mid, {"id": mid}),
            "move": move,
            "users": users,
            "tripped": resolution.circuit_breaker_tripped(move, users),
        })
    out.sort(key=lambda item: item["move"], reverse=True)
    return out, ""
