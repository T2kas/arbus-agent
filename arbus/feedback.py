"""Team feedback loop — turn "what's wrong with this batch" into a hard rule.

The people running Arbus review each batch and know things the model does not:
"too many economics markets", "stop the pension stories", "more TikTok stuff
like Dirkstys". Without a home, that feedback evaporates and the only way to
steer the bot is editing code.

This module gives it a home: a plain-text ``feedback.md`` at the repo root that
non-coders edit in any language. The pipeline reads it before every batch and
injects it into the draft prompt as high-priority guidance. Notes can also be
appended from Telegram (``/feedback ...``) or the CLI
(``python -m arbus feedback "..."``), so the loop closes right after a batch is
reviewed. Instruction comments (``<!-- ... -->``) and ``#`` heading lines are
stripped so only the actual notes reach the model.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

FEEDBACK_PATH = Path(__file__).resolve().parent.parent / "feedback.md"

_STARTER = """\
<!--
Arbus market agent — TEAM FEEDBACK.
Write what you liked or disliked about past batches, in plain Lithuanian or
English, one bullet per line. The bot reads this file before EVERY batch and
treats your notes as hard rules. Add notes here directly, from Telegram with
`/feedback <your note>`, or from the terminal: python -m arbus feedback "..."
This comment block and any line starting with # are ignored — only bullets count.

Examples (delete or keep):
  - mažiau ekonomikos ir pensijų rinkų
  - daugiau TikTok / influencerių temų (pvz. Dirkstys)
  - venk klausimų, kurių niekas dar negali patikrinti
-->
"""


def load_feedback(path: Path = FEEDBACK_PATH) -> str:
    """Return the team's notes as clean text, or '' if there are none."""
    if not path.exists():
        return ""
    text = re.sub(r"<!--.*?-->", "", path.read_text(encoding="utf-8"), flags=re.S)
    kept = [ln.rstrip() for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    return "\n".join(kept).strip()


def feedback_block(text: str) -> str:
    """Render feedback for the draft prompt (graceful when empty)."""
    return text if text else "(no team feedback recorded yet)"


def append_feedback(note: str, path: Path = FEEDBACK_PATH) -> str:
    """Append a dated bullet. Creates the file with a starter header if missing.

    Returns the line written (for confirmation messages).
    """
    note = " ".join(note.split()).strip()
    if not note:
        return ""
    if not path.exists():
        path.write_text(_STARTER, encoding="utf-8")
    line = f"- ({date.today().isoformat()}) {note}"
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")
    return line
