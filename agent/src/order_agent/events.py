"""Audit events: one per call that was decided somewhere."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class Event:
    """A decision about one tool call.

    ``detail`` is the SQLSTATE when ``decided_by`` is ``'database'`` and a refusal.
    """

    tool: str
    args: dict = field(default_factory=dict)
    decided_by: Literal["validation", "tool", "database"] = "database"
    outcome: Literal["ok", "refused"] = "ok"
    detail: str = ""
