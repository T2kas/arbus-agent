"""Outbound Telegram alerts (fire-and-forget, no interaction).

Two kinds of message:

* a batch is ready (Job 1);
* a market froze and needs an admin decision (Job 2).

The second one is the important one. Arbus runs an AMM, so a frozen market is
money standing still and a wrong decision is money gone — the team should learn
about it on their phones, not by opening a dashboard. The alert carries
everything needed to decide: the market, what the reporter claims, their source,
and the AI's advisory reading of that source.

Both are best-effort: a failed Telegram call never blocks resolution.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3

import requests

log = logging.getLogger(__name__)

TG_LIMIT = 3800  # Telegram's hard cap is 4096 characters


def _credentials() -> tuple[str, str] | None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat_id) if token and chat_id else None


def send(text: str) -> bool:
    """Send one message to the configured chat (or group). Never raises."""
    creds = _credentials()
    if creds is None:
        log.info("telegram not configured (TELEGRAM_BOT_TOKEN/CHAT_ID) — skipping alert")
        return False
    token, chat_id = creds
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text[:TG_LIMIT],
                  "disable_web_page_preview": True},
            timeout=30,
        ).raise_for_status()
        return True
    except Exception as exc:
        log.warning("telegram notify failed: %s", exc)
        return False


def notify_batch(batch_id: str, accepted: int, needs_review: int, report_path: str) -> None:
    send(
        f"🍉 Arbus batch {batch_id} ready\n"
        f"Accepted: {accepted} (needs review: {needs_review})\n"
        f"Report: {report_path}"
    )


def resolution_message(market: sqlite3.Row, request: sqlite3.Row | None,
                       ai_summary: str) -> str:
    """The text of a freeze alert. Split out from sending so it can be tested
    offline and reused by whatever front-end shows it next."""
    options = " / ".join(json.loads(market["options_json"]))
    lines = [
        "🧊 RINKA UŽŠALDYTA — reikia admino sprendimo",
        "",
        f"#{market['id']} {market['question_lt']}",
        f"Variantai: {options}",
        f"Terminas: {market['resolve_by']}",
    ]
    if request is not None:
        lines += [
            "",
            f"👤 Pranešė: {request['user_id']}",
            f"Siūloma baigtis: {request['proposed_option']}",
            f"Užstatas: {request['bond']} Arbukų",
            f"Šaltinis: {request['source_url']}",
        ]
    else:
        lines += ["", f"⚡ Priežastis: {market['freeze_reason'] or 'nenurodyta'}"]

    lines += [
        "",
        "Taisyklės (pagal jas sprendžiama):",
        market["resolution_hint_lt"] or "(nėra)",
        "",
        "🤖 AI PATIKRA (patariamoji — AI nieko nesprendžia):",
        ai_summary or "(nepavyko)",
        "",
        "Sprendimą priima adminas dashboarde. Po paspaudimo išmokėjimas "
        "įvyksta ne iš karto — lieka undo langas.",
    ]
    return "\n".join(lines)


def notify_resolution(market: sqlite3.Row, request: sqlite3.Row | None,
                      ai_summary: str) -> bool:
    return send(resolution_message(market, request, ai_summary))
