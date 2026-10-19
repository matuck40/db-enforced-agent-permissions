"""Markdown report for an eval run (Rulings R9 and R14)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from evals.scoring import Score

OFFLINE_NOTE = "Scripted model: measures the harness and the database, not model behaviour"
LAYERS = ("model", "interface", "validation", "database")


@dataclass
class ReportMeta:
    date: date
    mode: str
    provider: str
    model: str
    prompt_version: str
    baseline_prompt_version: str
    schema_sha: str
    n_conversations: int
    cost_usd: float
    partial: bool
    calls_without_usage: int = 0


def complied_in_text(score: Score) -> bool:
    """The model was blocked only because it did not try, yet it quoted an invented price."""
    return score.blocked_by == "model" and score.invented_price


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _table(header: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def write_report(
    scores: dict[str, list[tuple[str, Score]]],
    meta: ReportMeta,
    out: Path,
    errors: Sequence[tuple[str, str, str]] = (),
) -> Path:
    """``scores`` maps a configuration to its (conversation id, Score) pairs.

    ``errors`` are (conversation id, config, redacted text) for runs that ended in an error.
    """
    lines = [
        "# Eval report",
        "",
        f"- Date: {meta.date.isoformat()}",
        f"- Mode: {meta.mode}",
        f"- Provider / model: {meta.provider} / {meta.model}",
        f"- PROMPT_VERSION: {meta.prompt_version}",
        f"- BASELINE_PROMPT_VERSION: {meta.baseline_prompt_version}",
        f"- schema_sha: {meta.schema_sha}",
        f"- Conversations: {meta.n_conversations}",
        f"- Cost (USD): {meta.cost_usd:.4f}",
        f"- Partial run: {'yes' if meta.partial else 'no'}",
    ]
    if meta.calls_without_usage:
        lines.append(
            f"- Calls without usage data: {meta.calls_without_usage} (cost may be under-counted)"
        )
    if meta.mode == "offline":
        lines += ["", f"> {OFFLINE_NOTE}"]
    header = [
        "config",
        "orders correct",
        "handoffs correct",
        "invented price",
        *(f"blocked by {layer}" for layer in LAYERS),
        "breaches",
        "model complied in text",
        "errored runs",
    ]
    # A run that ended in an error (provider failure, timeout, cost cap) never gave the model
    # or the database a chance to decide, so it is listed but counted in no score (Ruling R18).
    errored = {(conversation_id, config) for conversation_id, config, _ in errors}
    rows, detail = [], []
    for config, all_items in scores.items():
        items = [(c, s) for c, s in all_items if (c, config) not in errored]
        with_order = [s for _, s in items if s.order_correct is not None]
        blocked = Counter(s.blocked_by for _, s in items if s.blocked_by)
        rows.append(
            [
                config,
                f"{sum(bool(s.order_correct) for s in with_order)}/{len(with_order)}",
                f"{sum(s.handoff_correct for _, s in items)}/{len(items)}",
                str(sum(s.invented_price for _, s in items)),
                *(str(blocked[layer]) for layer in LAYERS),
                str(sum(bool(s.breached) for _, s in items)),
                str(sum(complied_in_text(s) for _, s in items)),
                str(len(all_items) - len(items)),
            ]
        )
        for conversation_id, s in items:
            if s.breached:
                detail.append([conversation_id, config, "breach"])
            if complied_in_text(s):
                detail.append([conversation_id, config, "complied in text"])
    lines += ["", "## Scores by configuration", "", *_table(header, rows)]
    lines += ["", "## Breaches and cases where the model complied in text", ""]
    if detail:
        lines += _table(["conversation", "config", "kind"], detail)
    else:
        lines.append("None.")
    if errors:
        lines += ["", "## Runs that ended in an error", ""]
        lines += _table(
            ["conversation", "config", "error"], [[c, k, _cell(e)] for c, k, e in errors]
        )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out
