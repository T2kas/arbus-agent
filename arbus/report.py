"""Markdown batch report + JSON export for the team."""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from . import config
from .schemas import Candidate, FullSpec


def write_batch_report(
    batch_id: str,
    accepted: list[tuple[int, Candidate, str, str]],  # (db_id, cand, verdict, note)
    rejected: list[tuple[Candidate, str]],            # (cand, reason)
    theme_stats: dict | None = None,   # {label: (target, drafted, accepted)}
    reject_reasons: dict | None = None,
) -> Path:
    lines = [f"# Arbus batch {batch_id}", ""]

    # Yield per theme makes an unbalanced batch self-explaining: if a theme was
    # asked for 5, drafted 9 and landed 1, the problem is the gates for that
    # theme, not the quota.
    if theme_stats:
        lines += ["## Yield per theme", "",
                  "| Tema | Užsakyta | Sukurta | Priimta |", "|---|---:|---:|---:|"]
        for label, (target, drafted, acc) in theme_stats.items():
            lines.append(f"| {label} | {target} | {drafted} | {acc} |")
        lines.append("")
    if reject_reasons:
        top = sorted(reject_reasons.items(), key=lambda kv: -kv[1])[:8]
        lines += ["**Dažniausios atmetimo priežastys:** "
                  + ", ".join(f"{k} ×{v}" for k, v in top), ""]

    durations = Counter(c.duration_class for _, c, _, _ in accepted)
    cats = Counter(c.category for _, c, _, _ in accepted)
    lines += [
        f"**Accepted:** {len(accepted)}  |  **Rejected:** {len(rejected)}",
        f"**Durations:** " + ", ".join(f"{k}: {v}" for k, v in sorted(durations.items())),
        f"**Categories:** " + ", ".join(f"{k}: {v}" for k, v in cats.most_common()),
        "",
        "## Candidates",
        "",
    ]

    illustrated = sum(1 for _, c, _, _ in accepted if getattr(c, "image_url", ""))
    if illustrated:
        lines.insert(4, f"**Images:** {illustrated}/{len(accepted)}")

    for db_id, c, verdict, note in accepted:
        flag = " ⚠️ NEEDS REVIEW" if verdict == "UNCLEAR" else ""
        probs = ", ".join(f"{o} {p:.0%}" for o, p in zip(c.options_lt, c.probabilities))
        lines += [
            f"### #{db_id} — {c.question_lt}{flag}",]
        if getattr(c, "image_url", ""):
            lines.append(f"![]({c.image_url})")
        lines += [
            f"- **Type:** {c.market_type} | **Category:** {c.category} | "
            f"**Resolves by:** {c.resolve_by} ({c.duration_class})",
            f"- **Estimates:** {probs}",
            f"- **Resolution:** {c.resolution_hint_lt}",
            f"- **Verification:** {verdict} — {note}" if note else f"- **Verification:** {verdict}",
            f"- **Why now:** {c.rationale_en}",
            f"- **Sources:** " + " | ".join(c.sources),
            "",
        ]

    if rejected:
        lines += ["## Rejected", ""]
        for c, reason in rejected:
            lines.append(f"- {c.question_lt} — _{reason}_")
        lines.append("")

    path = Path(config.REPORT_DIR) / f"batch_{batch_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def write_batch_export(batch_id: str, accepted: list[tuple[int, Candidate, str, str]]) -> Path:
    payload = [
        {"id": db_id, "verify_verdict": verdict, "verify_note": note, **cand.model_dump()}
        for db_id, cand, verdict, note in accepted
    ]
    path = Path(config.EXPORT_DIR) / f"batch_{batch_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def append_full_spec(batch_id: str, market_id: int, spec: FullSpec) -> Path:
    path = Path(config.REPORT_DIR) / f"specs_{batch_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    block = [
        f"## Market #{market_id} — {spec.question_lt}",
        f"**Trigger:** {spec.trigger_sentence_lt}",
        "**Definitions:**",
        *[f"- {d}" for d in spec.definitions_lt],
        "**Edge cases:**",
        *[f"- {e}" for e in spec.edge_cases_lt],
        f"**Primary source:** {spec.primary_source}",
        f"**Backup source:** {spec.backup_source}",
        f"**Early resolution:** {spec.early_resolution_logic_lt}",
        f"**Freeze:** {spec.freeze_time_hint}",
        "",
    ]
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(block) + "\n")
    return path
