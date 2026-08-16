"""Telegram bot — type /markets in your chat, get a fresh batch back.

Long-polling, single-process, no webhook/server needed:

    python -m arbus bot

Security: the bot only obeys the chat configured in TELEGRAM_CHAT_ID.
If TELEGRAM_CHAT_ID is empty it answers only /id (so you can discover your
chat id) and ignores everything else.
"""

from __future__ import annotations

import logging
import os
import traceback

import requests

from . import config, feedback, pipeline

log = logging.getLogger(__name__)

HELP = (
    "🍉 Arbus market agent\n\n"
    "/markets — generate a batch (default "
    f"{config.DEFAULT_BATCH_SIZE})\n"
    "/markets 15 — generate 15 candidates\n"
    "/markets 15 fast — skip web verification (cheaper, riskier)\n"
    "/feedback <pastaba> — teach the bot (e.g. /feedback mažiau ekonomikos)\n"
    "/id — show this chat's id\n"
    "/help — this message"
)

TG_LIMIT = 3800  # keep under Telegram's 4096-char message cap


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


def _format_batch(result: pipeline.BatchResult) -> str:
    lines = [
        f"🍉 Batch {result.batch_id}",
        f"✅ {len(result.accepted)} accepted | ⚠️ {result.needs_review} need review | "
        f"✗ {len(result.rejected)} rejected",
        "",
    ]
    for db_id, c, verdict, _note in result.accepted:
        flag = " ⚠️" if verdict == "UNCLEAR" else ""
        probs = " / ".join(f"{o} {p:.0%}" for o, p in zip(c.options_lt, c.probabilities))
        lines += [
            f"#{db_id}{flag} {c.question_lt}",
            f"   {probs}",
            f"   📅 iki {c.resolve_by} · {c.category} · {c.duration_class}",
            "",
        ]
    lines.append(f"Report: {result.report_path}")
    return "\n".join(lines)


def _handle(token: str, chat_id: str, text: str) -> None:
    parts = text.split()
    cmd = parts[0].lower().split("@")[0]  # tolerate /markets@BotName

    if cmd == "/markets":
        count = config.DEFAULT_BATCH_SIZE
        if len(parts) > 1 and parts[1].isdigit():
            count = max(3, min(60, int(parts[1])))
        skip_verify = "fast" in [p.lower() for p in parts[1:]]
        _send(token, chat_id,
              f"Generuoju {count} rinkų kandidatų... ⏳ (kelios minutės)")
        result = pipeline.run_batch(
            count=count,
            skip_verify=skip_verify,
            progress=lambda msg: log.info("%s", msg),
        )
        _send(token, chat_id, _format_batch(result))
    elif cmd == "/feedback":
        note = text[len("/feedback"):].strip()
        if not note:
            _send(token, chat_id,
                  "Parašyk pastabą po komandos, pvz.:\n/feedback mažiau ekonomikos rinkų")
        else:
            line = feedback.append_feedback(note)
            _send(token, chat_id, f"✍️ Įrašyta, į tai atsižvelgsiu kitose partijose:\n{line}")
    elif cmd in ("/help", "/start"):
        _send(token, chat_id, HELP)
    # unknown commands are ignored silently


def run() -> int:
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
            msg = upd.get("message") or upd.get("channel_post") or {}
            text = (msg.get("text") or "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))
            if not text.startswith("/") or not chat_id:
                continue

            if text.split()[0].lower().startswith("/id"):
                _send(token, chat_id, f"Chat id: {chat_id}")
                continue
            if not allowed:
                _send(token, chat_id,
                      "TELEGRAM_CHAT_ID nesukonfigūruotas. Įrašyk šio pokalbio id "
                      f"({chat_id}) į .env ir perkrauk botą.")
                continue
            if chat_id != allowed:
                log.warning("ignoring command from unauthorized chat %s", chat_id)
                continue

            try:
                _handle(token, chat_id, text)
            except Exception:
                log.error("command failed:\n%s", traceback.format_exc())
                _send(token, chat_id, "❌ Klaida generuojant. Žiūrėk boto logą.")
