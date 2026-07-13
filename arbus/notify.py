"""Optional outbound Telegram alert (single webhook call, no interaction)."""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger(__name__)


def notify_batch(batch_id: str, accepted: int, needs_review: int, report_path: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    text = (
        f"🍉 Arbus batch {batch_id} ready\n"
        f"Accepted: {accepted} (needs review: {needs_review})\n"
        f"Report: {report_path}"
    )
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=30,
        ).raise_for_status()
    except Exception as exc:
        log.warning("telegram notify failed: %s", exc)
