"""Telegram bot — generate ideas, then approve one straight into the app.

Long-polling, single-process, no webhook/server needed:

    python -m arbus bot

Two things it does:

  1. /markets — generate a batch of candidate ideas (each shown with a #id and
     the EUR cost of gathering them).
  2. /pridėti <id> — take one idea all the way into Arbus: the bot researches
     and writes the full rules, asks you for the liquidity and an image, shows
     the final draft, and only creates the market after you confirm. The EUR
     cost of drafting is reported too.

The approval flow is a tiny per-chat state machine held in memory for the life
of the process — you reply to the bot's questions (a number, an image URL or
"auto", then "taip"/"ne"), so no plain message is acted on unless the bot just
asked for it.

Security: the bot only obeys the chat configured in TELEGRAM_CHAT_ID. If it is
empty the bot answers only /id (so you can discover your chat id).
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from pathlib import Path

import requests

from . import app as app_api, compose, config, feedback, images, llm, pipeline, store

log = logging.getLogger(__name__)

HELP = (
    "🍉 Arbus market agent\n\n"
    "/markets — generuoti idėjų partiją (numatyta "
    f"{config.DEFAULT_BATCH_SIZE})\n"
    "/markets 15 — generuoti 15 kandidatų\n"
    "/markets 15 fast — be interneto patikros (pigiau, rizikingiau)\n"
    "/pridėti <id> — paruošti idėją ir įkelti į Arbus (klaus likvidumo, "
    "nuotraukos, patvirtinimo)\n"
    "/atšaukti — nutraukti dabartinį įkėlimą\n"
    "/feedback <pastaba> — pamokyti botą (pvz. /feedback mažiau ekonomikos)\n"
    "/id — parodyti šio pokalbio id\n"
    "/help — ši žinutė"
)

TG_LIMIT = 3800  # keep under Telegram's 4096-char message cap

# Per-chat in-flight upload. {chat_id: {"step", "cand", "composed", "meta",
# "liquidity", "image_url", "spec"}}. Held in memory during a long-poll session;
# for the cron drain it is persisted to the committed bot state so a multi-step
# upload survives between runs.
SESSIONS: dict[str, dict] = {}
# The ideas from the most recent /markets, keyed by their #id, so /pridėti works
# even when the SQLite candidate DB is gone (CI does not keep it between runs).
IDEAS: dict[str, dict] = {}
_MAX_IDEAS = 400


# ── committed state (so the cron drain remembers across runs) ────────────────

def _load_state() -> dict:
    try:
        return json.loads(Path(config.BOT_STATE_PATH).read_text("utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _save_state(offset: int | None) -> None:
    """Persist the getUpdates offset, the pending sessions and the idea list.
    ComposedMarket is stored as a plain dict so the file is JSON."""
    sessions = {}
    for chat, s in SESSIONS.items():
        out = dict(s)
        comp = out.get("composed")
        if hasattr(comp, "model_dump"):
            out["composed"] = comp.model_dump()
        sessions[chat] = out
    path = Path(config.BOT_STATE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"offset": offset, "sessions": sessions,
               "ideas": dict(list(IDEAS.items())[-_MAX_IDEAS:])}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")


def _restore_globals(state: dict) -> None:
    """Load the persisted sessions/ideas into the module globals, rebuilding the
    ComposedMarket objects the flow works with."""
    SESSIONS.clear()
    IDEAS.clear()
    IDEAS.update(state.get("ideas") or {})
    for chat, s in (state.get("sessions") or {}).items():
        s = dict(s)
        comp = s.get("composed")
        if isinstance(comp, dict):
            try:
                s["composed"] = compose.ComposedMarket(**comp)
            except Exception:                            # noqa: BLE001 — drop a bad session
                continue
        SESSIONS[chat] = s


def _api(token: str, method: str, **params) -> dict:
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/{method}", json=params, timeout=90
    )
    resp.raise_for_status()
    return resp.json()


def _send(token: str, chat_id: str, text: str) -> None:
    """Send text, chunked to respect Telegram's message size limit."""
    while text:
        if len(text) <= TG_LIMIT:
            chunk, text = text, ""
        else:
            cut = text.rfind("\n", 0, TG_LIMIT)
            cut = cut if cut > 0 else TG_LIMIT
            chunk, text = text[:cut], text[cut:].lstrip("\n")
        _api(token, "sendMessage", chat_id=chat_id, text=chunk, disable_web_page_preview=True)


# ── batch generation ─────────────────────────────────────────────────────────

def _format_batch(result: pipeline.BatchResult, cost_line: str) -> str:
    lines = [
        f"🍉 Batch {result.batch_id}",
        f"✅ {len(result.accepted)} accepted | ⚠️ {result.needs_review} need review | "
        f"✗ {len(result.rejected)} rejected",
    ]
    if cost_line:
        lines.append(cost_line)
    lines.append("")
    for db_id, c, verdict, _note in result.accepted:
        flag = " ⚠️" if verdict == "UNCLEAR" else ""
        probs = " / ".join(f"{o} {p:.0%}" for o, p in zip(c.options_lt, c.probabilities))
        lines += [
            f"#{db_id}{flag} {c.question_lt}",
            f"   {probs}",
            f"   📅 iki {c.resolve_by} · {c.category} · {c.duration_class}",
            "",
        ]
    lines.append("Patinka viena? Rašyk /pridėti <id> ir įkelsiu ją į Arbus.")
    return "\n".join(lines)


def _cmd_markets(token: str, chat_id: str, parts: list[str]) -> None:
    count = config.DEFAULT_BATCH_SIZE
    if len(parts) > 1 and parts[1].isdigit():
        count = max(3, min(60, int(parts[1])))
    skip_verify = "fast" in [p.lower() for p in parts[1:]]
    _send(token, chat_id, f"Generuoju {count} rinkų kandidatų... ⏳ (kelios minutės)")
    llm.reset_usage()
    try:
        result = pipeline.run_batch(count=count, skip_verify=skip_verify,
                                    progress=lambda msg: log.info("%s", msg))
    except RuntimeError as exc:
        # A clear, actionable failure (bad provider key/model/credits, or nothing
        # survived) — show it instead of a generic "žiūrėk logą".
        _send(token, chat_id, f"❌ Nepavyko sugeneruoti: {str(exc)[:350]}")
        return
    for db_id, cand, _v, _n in result.accepted:         # remember ideas for /pridėti
        IDEAS[str(db_id)] = compose.summary_from_candidate(cand)
    cost = llm.usage_line()                              # "💶 kaina ~0.30 € (...)"
    cost_line = f"💶 idėjų rinkimas: {cost.replace('💶 kaina ', '')}" if cost else ""
    _send(token, chat_id, _format_batch(result, cost_line))


# ── the approve-one-idea → upload flow ───────────────────────────────────────

def _draft_preview(spec_like: dict, extra: str = "") -> str:
    opts = "\n".join(f"   {o['probability']:>3}%  {o['label']}" for o in spec_like["options"])
    lines = [
        f"· {spec_like['title']}",
        (f"  {spec_like.get('subtitle')}" if spec_like.get("subtitle") else ""),
        f"  📂 {spec_like.get('category', '?')} · uždaroma "
        f"{str(spec_like.get('closes_at', ''))[:16].replace('T', ' ')}",
        opts,
        "",
        "📜 Taisyklės:",
        (spec_like.get("rules") or "(nėra)")[:1400],
    ]
    if spec_like.get("context"):
        lines += ["", "ℹ️ Kontekstas:", spec_like["context"][:600]]
    if extra:
        lines += ["", extra]
    return "\n".join(l for l in lines if l != "")


def _cmd_add(token: str, chat_id: str, parts: list[str]) -> None:
    if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
        _send(token, chat_id, "Nurodyk idėjos id, pvz.: /pridėti 12")
        return
    key = parts[1].lstrip("#")
    db_id = int(key)
    # Prefer the persisted idea list (works on stateless CI); fall back to the
    # local SQLite candidate DB for a developer running the long-poll bot.
    cand = IDEAS.get(key)
    if cand is None:
        conn = store.connect()
        row = store.get_market(conn, db_id)
        if conn is not None:
            conn.close()
        if row is None:
            _send(token, chat_id,
                  f"Idėjos #{db_id} neradau. Sugeneruok partiją su /markets.")
            return
        if str(row["status"]) == "rejected":
            _send(token, chat_id, f"#{db_id} buvo atmesta — jos nekelsiu.")
            return
        cand = compose.candidate_summary(row)
    _send(token, chat_id, f"Ruošiu #{db_id}: „{cand['question']}“ … ⏳ (tikrinu internete)")
    try:
        composed, meta = compose.compose(cand)
    except Exception:
        log.error("compose failed:\n%s", traceback.format_exc())
        _send(token, chat_id, "❌ Nepavyko paruošti idėjos. Žiūrėk boto logą.")
        return

    cost = meta.get("cost_eur") or 0.0
    if not composed.still_open:
        _send(token, chat_id,
              f"⚠️ Atrodo, šis įvykis jau įvyko arba nebeaktualus — nekelčiau.\n"
              f"💶 juodraščio kaina ~{cost:.2f} €\n\n"
              + _draft_preview({
                  "title": composed.title, "subtitle": composed.subtitle,
                  "category": composed.category, "closes_at": composed.closes_at,
                  "rules": composed.rules, "context": composed.context,
                  "options": [{"label": o.label, "probability": round(o.probability)}
                              for o in composed.options]}))
        return

    SESSIONS[chat_id] = {"step": "liquidity", "cand": cand, "composed": composed,
                         "meta": meta, "db_id": db_id}
    preview = _draft_preview({
        "title": composed.title, "subtitle": composed.subtitle,
        "category": composed.category, "closes_at": composed.closes_at,
        "rules": composed.rules, "context": composed.context,
        "options": [{"label": o.label, "probability": round(o.probability)}
                    for o in composed.options]})
    _send(token, chat_id,
          f"📝 Juodraštis paruoštas (💶 ~{cost:.2f} €):\n\n{preview}\n\n"
          "1/3 — koks LIKVIDUMAS? Parašyk skaičių (pvz. 50000).")


def _flow_reply(token: str, chat_id: str, text: str, has_photo: bool) -> None:
    """Handle a plain reply while an upload is in progress for this chat."""
    sess = SESSIONS.get(chat_id)
    if not sess:
        return
    step = sess["step"]

    if step == "liquidity":
        digits = "".join(ch for ch in text if ch.isdigit())
        if not digits:
            _send(token, chat_id, "Parašyk likvidumą skaičiumi, pvz. 50000.")
            return
        sess["liquidity"] = max(1000, int(digits))
        sess["step"] = "image"
        srcs = sess["cand"].get("sources") or []
        hint = " (turiu šaltinių — galiu parinkti pats)" if srcs else ""
        _send(token, chat_id,
              f"2/3 — NUOTRAUKA? Atsiųsk paveikslėlio URL, arba parašyk „auto“{hint}, "
              "arba „be“ (be nuotraukos).")
        return

    if step == "image":
        low = text.strip().lower()
        if has_photo:
            _send(token, chat_id,
                  "Programėlei reikia viešo paveikslėlio URL — atsiųsto failo "
                  "panaudoti negaliu. Parašyk URL arba „auto“ (parinksiu pats).")
            return
        if low in ("auto", "automatiškai", "pats"):
            url, _src = images.image_for_sources(sess["cand"].get("sources") or [])
            sess["image_url"] = url or ""
            note = f"parinkau: {url}" if url else "neradau tinkamos — kelsiu be nuotraukos"
            _send(token, chat_id, f"🖼️ {note}")
        elif low in ("be", "nera", "nėra", "skip", "praleisti"):
            sess["image_url"] = ""
        elif text.strip().startswith("http"):
            sess["image_url"] = text.strip()
        else:
            _send(token, chat_id, "Nesupratau. Atsiųsk URL, „auto“ arba „be“.")
            return
        spec = compose.build_spec(sess["composed"], sess["cand"],
                                  liquidity=sess["liquidity"],
                                  image_url=sess.get("image_url", ""))
        sess["spec"] = spec
        sess["step"] = "confirm"
        cost = sess["meta"].get("cost_eur") or 0.0
        img_line = f"🖼️ {spec['image_url']}" if spec["image_url"] else "🖼️ (be nuotraukos)"
        _send(token, chat_id,
              "3/3 — GALUTINIS JUODRAŠTIS:\n\n"
              + _draft_preview(spec, f"💧 likvidumas: {spec['liquidity']}\n{img_line}\n"
                                     f"💶 juodraščio kaina ~{cost:.2f} €")
              + "\n\nĮkelti į Arbus? Rašyk „taip“ (arba /patvirtinti). „ne“ atšauks.")
        return

    if step == "confirm":
        low = text.strip().lower().lstrip("/")
        if low in ("taip", "patvirtinti", "ikelti", "įkelti", "yes", "ok"):
            _upload(token, chat_id, sess)
        elif low in ("ne", "atšaukti", "atsaukti", "cancel", "no"):
            SESSIONS.pop(chat_id, None)
            _send(token, chat_id, "Atšaukta — nieko neįkėliau.")
        else:
            _send(token, chat_id, "Parašyk „taip“ (įkelti) arba „ne“ (atšaukti).")
        return


def _upload(token: str, chat_id: str, sess: dict) -> None:
    if not config.ARBUS_WRITE_KEY:
        _send(token, chat_id,
              "❌ Nėra ARBUS_WRITE_KEY (service_role) — įkelti negaliu. "
              "Nustatyk raktą ir bandyk vėl.")
        SESSIONS.pop(chat_id, None)
        return
    spec = sess["spec"]
    ok, detail = app_api.create_market(spec)
    cost = sess["meta"].get("cost_eur") or 0.0
    SESSIONS.pop(chat_id, None)
    if ok:
        _send(token, chat_id,
              f"✅ Įkelta į Arbus!\n· {spec['title']}\n  id: {detail}\n"
              f"  💧 likvidumas {spec['liquidity']} · uždaroma "
              f"{str(spec['closes_at'])[:16].replace('T', ' ')}\n"
              f"  💶 kaina (paruošimas + įkėlimas) ~{cost:.2f} €")
    else:
        _send(token, chat_id, f"❌ Nepavyko įkelti: {detail[:300]}")


# ── command routing ──────────────────────────────────────────────────────────

def _handle(token: str, chat_id: str, msg: dict) -> None:
    text = (msg.get("text") or msg.get("caption") or "").strip()
    has_photo = bool(msg.get("photo"))

    if text.startswith("/"):
        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        if cmd == "/markets":
            _cmd_markets(token, chat_id, parts)
        elif cmd in ("/pridėti", "/prideti", "/add", "/ikelti", "/įkelti"):
            _cmd_add(token, chat_id, parts)
        elif cmd in ("/atšaukti", "/atsaukti", "/cancel"):
            if SESSIONS.pop(chat_id, None):
                _send(token, chat_id, "Atšaukta.")
            else:
                _send(token, chat_id, "Nieko nevyksta.")
        elif cmd in ("/patvirtinti", "/taip") and chat_id in SESSIONS:
            _flow_reply(token, chat_id, "taip", has_photo)
        elif cmd == "/feedback":
            note = text[len("/feedback"):].strip()
            if not note:
                _send(token, chat_id,
                      "Parašyk pastabą po komandos, pvz.:\n/feedback mažiau ekonomikos rinkų")
            else:
                line = feedback.append_feedback(note)
                _send(token, chat_id, f"✍️ Įrašyta, į tai atsižvelgsiu:\n{line}")
        elif cmd in ("/help", "/start"):
            _send(token, chat_id, HELP)
        # unknown commands ignored
        return

    # Not a command: only meaningful mid-flow (a reply to the bot's question).
    if chat_id in SESSIONS:
        _flow_reply(token, chat_id, text, has_photo)


def _process_update(token: str, allowed: str, upd: dict) -> None:
    msg = upd.get("message") or upd.get("channel_post") or {}
    text = (msg.get("text") or msg.get("caption") or "").strip()
    chat_id = str(msg.get("chat", {}).get("id", ""))
    if not chat_id or (not text and not msg.get("photo")):
        return
    if text.split()[:1] and text.split()[0].lower().startswith("/id"):
        _send(token, chat_id, f"Chat id: {chat_id}")
        return
    if not allowed:
        _send(token, chat_id,
              "TELEGRAM_CHAT_ID nesukonfigūruotas. Įrašyk šio pokalbio id "
              f"({chat_id}) į .env ir perkrauk botą.")
        return
    if chat_id != allowed:
        log.warning("ignoring message from chat %s — bot only obeys TELEGRAM_CHAT_ID "
                    "%s (send the command THERE). Text was: %r",
                    chat_id, allowed, text[:40])
        return
    try:
        _handle(token, chat_id, msg)
    except Exception:
        log.error("handler failed:\n%s", traceback.format_exc())
        _send(token, chat_id, "❌ Klaida. Žiūrėk boto logą.")


def run() -> int:
    """Long-poll forever (interactive dev/local use)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set (see .env)")
        return 1
    allowed = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    me = _api(token, "getMe")["result"]["username"]
    log.info("bot @%s polling; allowed chat: %s", me, allowed or "(none — /id only)")

    offset: int | None = None
    while True:
        try:
            updates = _api(token, "getUpdates", timeout=50,
                           **({"offset": offset} if offset else {}))
        except requests.RequestException as exc:
            log.warning("getUpdates failed: %s — retrying", exc)
            continue
        for upd in updates.get("result", []):
            offset = upd["update_id"] + 1
            _process_update(token, allowed, upd)


def poll_once() -> int:
    """Drain pending Telegram updates once and exit — for a scheduled (cron) run
    with no always-on process. Offset, in-flight uploads and the idea list are
    kept in a committed state file so the multi-step /pridėti flow survives
    between runs and each message is handled exactly once."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("TELEGRAM_BOT_TOKEN is not set (see .env)")
        return 1
    allowed = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    state = _load_state()
    _restore_globals(state)
    offset = state.get("offset")

    try:
        updates = _api(token, "getUpdates", timeout=0,
                       **({"offset": offset} if offset else {})).get("result", [])
    except requests.RequestException as exc:
        log.warning("getUpdates failed: %s", exc)
        return 1

    if not updates:
        _save_state(offset)                              # keep sessions/ideas warm
        print("Nėra naujų žinučių.")
        return 0

    # Consume the offset FIRST so an expensive command (generation) is never
    # re-run. We do this two ways, because on CI the committed state file is only
    # written by a later git step that a cancelled/killed job never reaches:
    #   1) persist the offset locally, and
    #   2) ACK it to Telegram now — calling getUpdates with the new offset drops
    #      these updates server-side, so they are never redelivered even if this
    #      job dies mid-generation or its state is never committed.
    # The trade-off is deliberate: a command interrupted mid-run is dropped
    # rather than re-generated, which is the right call for an expensive batch.
    new_offset = updates[-1]["update_id"] + 1
    _save_state(new_offset)
    try:
        _api(token, "getUpdates", offset=new_offset, timeout=0)
    except requests.RequestException as exc:
        log.warning("could not ack updates to Telegram: %s", exc)
    print(f"Apdoroju {len(updates)} žinutę(-es)…")
    for upd in updates:
        _process_update(token, allowed, upd)
    _save_state(new_offset)                              # persist sessions/ideas changes
    return 0
