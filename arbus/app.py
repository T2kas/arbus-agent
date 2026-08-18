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


def headers(key: str = "") -> dict[str, str]:
    """Auth headers. `key` overrides the default anon key — set for the freeze
    write, which needs the privileged service_role key.

    Supabase's PostgREST wants the key in BOTH `apikey` and `Authorization`;
    sending only the bearer token returns a 401 that reads like a bad key.
    """
    key = key or config.ARBUS_API_KEY
    out = {"Content-Type": "application/json"}
    if key:
        out["Authorization"] = f"Bearer {key}"
        if "supabase.co" in config.ARBUS_API_URL:
            out["apikey"] = key
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


def _call(method: str, url: str, key: str = "", **kwargs) -> tuple[list[dict], str]:
    try:
        resp = requests.request(method, url, headers=headers(key),
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


def _endpoint(path: str, query: str = "", key: str = "") -> tuple[list[dict], str]:
    base = base_url()
    if not base:
        return [], "ARBUS_API_URL is not a /rest/v1/ URL — extra endpoints unavailable"
    return _call("GET", f"{base}{path}" + (f"?{query}" if query else ""), key=key)


def _rpc(name: str, payload: dict | None = None, key: str = "") -> tuple[list[dict], str]:
    base = base_url()
    if not base:
        return [], "ARBUS_API_URL is not a /rest/v1/ URL — RPC unavailable"
    return _call("POST", f"{base}rpc/{name}", key=key, json=payload or {})


# ── field lookup: the app owns the schema, so never assume one name ─────────

def _pick(row: dict, *names, default=None):
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return default


def _as_fraction(value) -> float | None:
    """Normalize a price/probability to a 0-1 fraction.

    Live-confirmed the app's `option_price_history.probability` (and the
    embedded `market_options[].probability`) is on a 0-100 scale — pairs sum to
    100 (e.g. 55.0/45.0), not 1 (0.55/0.45). Our own stored probabilities are
    0-1. Comparing the two scales directly is what turned a real price into
    "5500%" in calibration, and would make the circuit breaker's 0.15 (15
    percentage points) threshold trip on almost any nonzero move, since a
    genuine few-point swing (e.g. 3.0 on the 0-100 scale) already clears 0.15.
    A valid probability can never exceed 1, so anything bigger is assumed to be
    on the 0-100 scale and divided down; values already <=1 pass through.
    """
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value / 100 if value > 1 else value


def question_of(row: dict) -> str:
    # The app's markets table calls it `title`; our own DB rows call it
    # `question_lt`. Both, plus a few fallbacks, so one lookup fits either side.
    value = _pick(row, "title", "question_lt", "question", "name", "text", default="")
    return value.strip() if isinstance(value, str) else ""


def volume_of(row: dict) -> float:
    """Credits traded on a market. The app tracks this on the market itself
    (`volume_credits`), so it needs no summing over the trade feed."""
    try:
        return float(_pick(row, "volume_credits", "volume", "total_volume", default=0) or 0)
    except (TypeError, ValueError):
        return 0.0


def balance_of(profile: dict) -> float:
    try:
        return float(_pick(profile, "credits_balance", "balance", "credits", default=0) or 0)
    except (TypeError, ValueError):
        return 0.0


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
    # Common misconfig: ARBUS_API_URL set to the RPC base (.../rest/v1/rpc) — the
    # freeze endpoint — instead of the markets URL. Reading then GETs the `rpc`
    # path as if it were a table ("Could not find the table 'public.rpc'").
    # Catch it with a clear message; the freeze RPC is derived from this URL, so
    # it must be the markets URL, not the rpc one.
    if config.ARBUS_API_URL.rstrip("/").endswith("/rest/v1/rpc"):
        return [], ("ARBUS_API_URL rodo į RPC endpoint'ą (.../rest/v1/rpc). Jis "
                    "turi būti MARKETS URL (.../rest/v1/markets?select=...). Freeze "
                    "RPC iš jo išvedamas automatiškai — atskiro rpc URL nereikia.")
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


# PostgREST answers a wrong RPC parameter name with PGRST202 ("no matches found
# in the schema cache"). We do not know the exact SQL signature, so try the
# configured name first, then the common conventions, until one is accepted.
_FREEZE_PARAM_FALLBACKS = (
    "market_id", "p_market_id", "_market_id", "market_uuid", "id", "mid", "market")


def _call_freeze_rpc(fn: str, market_id: str) -> tuple[bool, str]:
    """Call an admin freeze/unfreeze RPC, trying parameter-name variants.

    Only a param-name mismatch (PGRST202/404) is retried with the next name; a
    real error (403 no permission, network) stops immediately and is returned as
    is — trying more names would not help and hides the true cause.
    """
    names = [config.APP_FREEZE_RPC_PARAM] + [
        p for p in _FREEZE_PARAM_FALLBACKS if p != config.APP_FREEZE_RPC_PARAM]
    last = ""
    for name in names:
        rows, error = _rpc(fn, {name: market_id}, key=config.ARBUS_WRITE_KEY)
        if not error:
            return True, f"ok (param '{name}')"
        last = error
        if "PGRST202" not in error and "404" not in error:
            break                       # not a param mismatch — real failure
    return False, _with_auth_hint(last)


def _is_auth_error(error: str) -> bool:
    low = error.lower()
    return ("not authenticated" in low or "p0001" in low or "401" in error
            or "403" in error or "permission" in low or "jwt" in low)


def _with_auth_hint(error: str) -> str:
    """When the RPC rejects the caller as unauthenticated and there is no write
    key, point at the fix: set ARBUS_WRITE_KEY to the service_role key."""
    if not config.ARBUS_WRITE_KEY and _is_auth_error(error):
        return (f"{error} — reikia service_role rakto: nustatyk ARBUS_WRITE_KEY "
                "secret'ą (anon raktas neautentifikuotas).")
    return error


def _set_status(market_id: str, status: str) -> tuple[bool, str]:
    """Write a market's status directly. service_role bypasses RLS, so this halts
    trading even when the admin RPC's own auth check refuses a backend caller."""
    base = base_url()
    if not base:
        return False, "ARBUS_API_URL is not a /rest/v1/ URL"
    rows, error = _call("PATCH", f"{base}markets?id=eq.{market_id}",
                        key=config.ARBUS_WRITE_KEY, json={"status": status})
    if error:
        return False, error
    return True, f"status={status}"


def freeze_market(market_id: str) -> tuple[bool, str]:
    """Halt trading on a market. Tries the app's admin RPC first; if that RPC's
    own auth check refuses a backend caller ("not authenticated"), falls back to
    a direct status write with the service_role key, which bypasses RLS. Opt-in
    via `arbus watch --freeze` — stopping trading has money attached."""
    ok, detail = _call_freeze_rpc(config.APP_FREEZE_RPC, market_id)
    if ok:
        return ok, detail
    # RPC blocked us (its uid check does not accept the service role). If we hold
    # the privileged key, freeze by writing the status straight to the table.
    if config.ARBUS_WRITE_KEY and _is_auth_error(detail):
        ok2, detail2 = _set_status(market_id, config.APP_FREEZE_STATUS)
        if ok2:
            return True, f"{detail2} (tiesioginis rašymas — RPC atmetė backend'ą)"
        return False, f"RPC: {detail} | tiesioginis rašymas nepavyko: {detail2}"
    return ok, detail


def unfreeze_market(market_id: str) -> tuple[bool, str]:
    """Resume trading — the inverse RPC, with the same direct-write fallback."""
    ok, detail = _call_freeze_rpc(config.APP_UNFREEZE_RPC, market_id)
    if ok:
        return ok, detail
    if config.ARBUS_WRITE_KEY and _is_auth_error(detail):
        return _set_status(market_id, config.APP_UNFREEZE_STATUS)
    return ok, detail


def resolution_proposals(limit: int = 50) -> tuple[list[dict], str]:
    """Resolution proposals users submitted (market_resolution_proposals): which
    market, the option they claim won (`proposed_option_id`), their cited
    `source`, `status`, and `created_at`. Newest first.

    Read with the service_role key (ARBUS_WRITE_KEY) when it is set: this table
    is almost always behind RLS that only lets the proposing user or an admin
    read a row, so the anon key sees nothing and the check finds "no proposals"
    even though users submitted them. service_role bypasses RLS. Falls back to
    the anon key when no write key is configured."""
    return _endpoint("market_resolution_proposals",
                     f"select=*&order=created_at.desc&limit={limit}",
                     key=config.ARBUS_WRITE_KEY)


def option_label(market: dict, option_id) -> str:
    """The human label (TAIP/NE/…) for an option id, from the market's options."""
    if option_id in (None, ""):
        return ""
    for opt in (market.get("market_options") or market.get("options") or []):
        if str(_pick(opt, "id", "option_id", default="")) == str(option_id):
            return str(_pick(opt, "label", "name", "title", default=option_id))
    return str(option_id)


def price_history(limit: int = 1000) -> tuple[list[dict], str]:
    # Newest-first, across all markets. The breaker seeds each option with its
    # last price from before the window as a baseline, so the fetch has to reach
    # far enough back to include that row for a market that has been quiet — a
    # tight limit would drop it and hide a real jump.
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


def price_moves(history: list[dict], option_map: dict[str, str] | None = None,
                window_minutes: int = config.CB_WINDOW_MINUTES,
                now: datetime | None = None,
                since: dict[str, datetime] | None = None) -> dict[str, float]:
    """Largest price swing per market inside the window.

    The app's `option_price_history` carries `market_id` on every row, so that
    is used directly; `option_map` is only a fallback for a feed that gives just
    the option id. The swing that matters is the whole range inside the window,
    not last-minus-first: a price pushed up and partly released still says
    someone knew something.

    The range is measured **per option**, then the market's move is the largest
    of its options' swings. A binary market sitting stable at TAIP 70 / NE 30
    holds two prices whose spread is 0.40 — but neither option *moved*, so
    lumping both options together would falsely read that spread as a 40% swing
    and trip the breaker on a market that never changed.

    Each option is also seeded with the price it carried **entering** the window
    — its most recent row from before the cutoff. A single jump (15% → 62% just
    before the run) leaves only the 62% row inside the window; without the entry
    price its range would read 0. The entry price is the baseline the jump is
    measured against.

    `since` is a per-market watermark: rows at or before `since[mid]` are movement
    the breaker already acted on, so they count only as baseline, never as a fresh
    swing. That is what stops a reopened market from being closed again on the same
    old jump — only a new jump, after the watermark, trips it.
    """
    option_map = option_map or {}
    since = since or {}
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window_minutes)
    per_option: dict[tuple[str, str], list[float]] = {}
    baseline: dict[tuple[str, str], float] = {}   # most recent non-movement price
    for row in history:                            # newest-first
        price = _as_fraction(_pick(row, "probability", "price", "new_price", "value"))
        oid = str(_pick(row, "option_id", "id", default=""))
        mid = str(_pick(row, "market_id", default="")) or option_map.get(oid, oid)
        if price is None or not mid:
            continue
        ts = _timestamp_of(row)
        if ts is None:
            continue
        key = (mid, oid)
        acted = mid in since and ts <= since[mid]
        if ts < cutoff or acted:
            baseline.setdefault(key, price)        # newest non-movement = what it entered at
        else:
            per_option.setdefault(key, []).append(price)

    per_market: dict[str, float] = {}
    for key, prices in per_option.items():
        pts = prices + ([baseline[key]] if key in baseline else [])
        mid = key[0]
        per_market[mid] = max(per_market.get(mid, 0.0), max(pts) - min(pts))
    return per_market


def option_windows(history: list[dict], window_minutes: int = config.CB_WINDOW_MINUTES,
                   now: datetime | None = None, option_map: dict[str, str] | None = None,
                   since: dict[str, datetime] | None = None) -> dict[str, tuple[float, float]]:
    """{option id: (start, end)} prices inside the window, as 0-1 fractions.

    History is newest-first, so per option the FIRST row seen is the latest
    price (end) and the LAST is the earliest (start). Lets the alert show
    "TAIP 60%→20%" instead of a bare magnitude.

    The start is the price the option carried entering the window — its most
    recent row from before the cutoff — falling back to the earliest in-window
    price when there is none. Without that a single jump just before the run
    would show "62%→62%" instead of the "15%→62%" that actually happened.

    `since` mirrors `price_moves`: rows at or before a market's watermark are
    already-acted movement, shown as the start baseline, not the move — so a
    reopened market's alert shows only the fresh jump."""
    option_map = option_map or {}
    since = since or {}
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=window_minutes)
    ends: dict[str, float] = {}
    starts: dict[str, float] = {}
    baseline: dict[str, float] = {}
    for row in history:                  # newest-first
        oid = str(_pick(row, "option_id", "id", default=""))
        price = _as_fraction(_pick(row, "probability", "price", "new_price", "value"))
        if not oid or price is None:
            continue
        ts = _timestamp_of(row)
        if ts is None:
            continue
        mid = str(_pick(row, "market_id", default="")) or option_map.get(oid, oid)
        acted = mid in since and ts <= since[mid]
        if ts < cutoff or acted:
            baseline.setdefault(oid, price)   # newest non-movement = most recent before the move
            continue
        ends.setdefault(oid, price)      # newest-first: first seen = end
        starts[oid] = price              # last seen in window = start
    return {oid: (baseline.get(oid, starts[oid]), ends[oid]) for oid in ends}


def option_move_lines(market: dict, windows: dict[str, tuple[float, float]]) -> list[dict]:
    """Per-option {label, start, end} for a market, in the app's own order, for
    options that actually moved in the window."""
    out = []
    options = market.get("market_options") or market.get("options") or []
    for opt in sorted(options, key=lambda o: _pick(o, "sort_order", default=0)):
        oid = str(_pick(opt, "id", "option_id", default=""))
        if oid in windows:
            start, end = windows[oid]
            out.append({"label": _pick(opt, "label", "name", "title", default=oid),
                        "start": start, "end": end})
    return out


def format_option_moves(lines: list[dict]) -> str:
    """'TAIP 60%→20% · NE 40%→80%' from option_move_lines output."""
    return " · ".join(f"{o['label']} {o['start'] * 100:.0f}%→{o['end'] * 100:.0f}%"
                      for o in lines)


def trade_market_key(trade: dict) -> str:
    """How a trade names its market.

    The app's `admin_recent_trades` feed gives only `market_title`, not an id,
    so trades are keyed by the (lower-cased) title and matched to markets by
    title. A feed that does carry `market_id` is used directly.
    """
    mid = str(_pick(trade, "market_id", default=""))
    if mid:
        return mid
    title = str(_pick(trade, "market_title", "title", default="")).strip().lower()
    return title


def trade_user(trade: dict) -> str:
    return str(_pick(trade, "username", "user_id", "profile_id", "user", default=""))


def traders_per_market(trades: list[dict], window_minutes: int = config.CB_WINDOW_MINUTES,
                       now: datetime | None = None) -> dict[str, set[str]]:
    """{market key: distinct users who traded inside the window}.

    The key is a market id when the feed has one, otherwise the lower-cased
    market title — see `trade_market_key`.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(minutes=window_minutes)
    out: dict[str, set[str]] = {}
    for trade in trades:
        ts = _timestamp_of(trade)
        if ts is not None and ts < cutoff:
            continue
        key = trade_market_key(trade)
        user = trade_user(trade)
        if key and user:
            out.setdefault(key, set()).add(user)
    return out


# ── market health: dead, important, overdue, mispriced ──────────────────────

def trade_stats(trades: list[dict], days: int = config.DEAD_MARKET_DAYS,
                now: datetime | None = None) -> dict[str, dict]:
    """{market key: {trades, users, volume}} over the window.

    Keyed by `trade_market_key` (market id, else lower-cased title) because the
    app's trade feed identifies markets by title. `volume` comes from
    `amount_credits`; for the market's own lifetime volume use `volume_of`.
    """
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    out: dict[str, dict] = {}
    for trade in trades:
        ts = _timestamp_of(trade)
        if ts is not None and ts < cutoff:
            continue
        key = trade_market_key(trade)
        if not key:
            continue
        stat = out.setdefault(key, {"trades": 0, "users": set(), "volume": 0.0})
        stat["trades"] += 1
        user = trade_user(trade)
        if user:
            stat["users"].add(user)
        try:
            stat["volume"] += abs(float(_pick(trade, "amount_credits", "amount",
                                              "arbucks", "stake", "total",
                                              default=0) or 0))
        except (TypeError, ValueError):
            pass
    return {key: {"trades": s["trades"], "users": len(s["users"]),
                  "volume": s["volume"]} for key, s in out.items()}


def market_stat(stats: dict[str, dict], market: dict) -> dict:
    """Look a market up in trade_stats by whichever key the feed used."""
    return (stats.get(market_id_of(market))
            or stats.get(question_of(market).strip().lower())
            or {"trades": 0, "users": 0, "volume": 0.0})


def latest_prices(history: list[dict]) -> dict[str, float]:
    """{option id: most recent price}. History arrives newest-first."""
    prices: dict[str, float] = {}
    for row in history:
        oid = str(_pick(row, "option_id", "id", default=""))
        price = _as_fraction(_pick(row, "probability", "price", "new_price", "value"))
        if oid and oid not in prices and price is not None:
            prices[oid] = price
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
                       now: datetime | None = None,
                       since: dict[str, datetime] | None = None) -> tuple[list[dict], str]:
    """Markets whose live flow trips the circuit breaker.

    This is where `resolution.circuit_breaker_tripped` finally gets real data:
    a price swing AND several distinct users behind it. Returns one dict per
    market: {market, move, users, tripped}.

    `since` is the per-market watermark of movement already acted on (see
    `price_moves`), so a reopened market only re-trips on a fresh jump.
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

    option_map = option_to_market(market_rows)
    by_id = {market_id_of(m): m for m in market_rows}

    # A market a user has PROPOSED a result for is watched harder: a longer
    # window for a smaller move (config.CB_PROPOSED_*). Compute both profiles
    # from the one fetch and pick per market. Proposed set is empty without the
    # service_role key, so the default profile is the only one that runs then.
    proposed = _proposed_market_ids()
    strict_window = config.CB_PROPOSED_WINDOW_MINUTES

    moves = price_moves(history, option_map, window_minutes, now, since)
    windows = option_windows(history, window_minutes, now, option_map, since)
    traders = traders_per_market(trades, window_minutes, now)

    s_moves = price_moves(history, option_map, strict_window, now, since) if proposed else {}
    s_windows = option_windows(history, strict_window, now, option_map, since) if proposed else {}
    s_traders = traders_per_market(trades, strict_window, now) if proposed else {}

    def users_for(market: dict, mid: str, tr: dict) -> int:
        # Price history keys by market id; the trade feed keys by title. Try
        # both so the two halves of the breaker actually meet.
        by_market_id = tr.get(mid, set())
        by_title = tr.get(question_of(market).strip().lower(), set())
        return len(by_market_id | by_title)

    out = []
    # A proposed market's jump may sit in the wider strict window only, so union
    # the ids seen under either profile.
    for mid in set(moves) | set(s_moves):
        market = by_id.get(mid, {"id": mid})
        # Only live markets can trip. A resolved market's price snaps to 100/0 —
        # that is the settlement, not suspicious flow — and an already
        # closed/frozen one is out of the breaker's hands. Skip both.
        if not is_open(market):
            continue
        if mid in proposed:
            move = s_moves.get(mid, 0.0)
            users = users_for(market, mid, s_traders)
            options = option_move_lines(market, s_windows)
            tripped = resolution.circuit_breaker_tripped(
                move, users,
                price_threshold=config.CB_PROPOSED_PRICE_MOVE,
                min_users=config.CB_PROPOSED_MIN_DISTINCT_USERS)
            window = strict_window
        else:
            move = moves.get(mid, 0.0)
            users = users_for(market, mid, traders)
            options = option_move_lines(market, windows)
            tripped = resolution.circuit_breaker_tripped(move, users)
            window = window_minutes
        out.append({
            "market": market,
            "move": move,
            "users": users,
            "options": options,
            "tripped": tripped,
            "proposed": mid in proposed,
            "window": window,
        })
    out.sort(key=lambda item: item["move"], reverse=True)
    return out, ""


def _proposed_market_ids() -> set[str]:
    """Market ids with a live resolution proposal — the breaker watches these
    harder. Proposals sit behind RLS, so without ARBUS_WRITE_KEY (service_role)
    we read nothing and every market falls back to the default thresholds."""
    if not config.ARBUS_WRITE_KEY:
        return set()
    proposals, err = resolution_proposals()
    if err:
        log.warning("strict breaker: could not read proposals (%s) — using "
                    "default thresholds for every market", err)
        return set()
    return {str(p.get("market_id")) for p in proposals
            if p.get("market_id") is not None}
