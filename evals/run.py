"""Eval runner: runs conversations through the agent, scores them, writes the report.

    uv run --project agent python -m evals.run --mode offline --config all \\
        --schema schema.sql --out evals/reports/offline-latest.md

Offline, the model is a scripted fake (see ``evals/conversation.py``), so the run
measures the harness and the database, not model behaviour. The offline gate fails
(exit 1) on a breach in ``baseline`` or ``tools``, a score that differs from the one the
script is expected to produce, a script that is not fully consumed (or runs out), or an
attack attempt that no layer refused and that did not breach (a harness warning).
Breaches in ``tools_unguarded`` do not fail the gate: that is what a later task measures.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, get_args
from urllib.parse import urlsplit, urlunsplit

import psycopg
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from evals.conversation import Conversation, load_conversations
from evals.report import ReportMeta, write_report
from evals.scoring import Config, RunResult, Score, score
from order_agent.baseline import BASELINE_PROMPT_VERSION, BaselineAgent
from order_agent.db import AgentDB
from order_agent.events import Event
from order_agent.fake import ScriptedChatModel
from order_agent.graph import RUN_CONFIG, build_graph
from order_agent.prompt import PROMPT_VERSION
from order_agent.redact import redact

ROOT = Path(__file__).resolve().parents[1]
CONFIGS: tuple[Config, ...] = get_args(Config)  # ("baseline", "tools", "tools_unguarded")
MARKER = "$ORDER_ID"

COUNTER_ORDER = "00000000-0000-0000-0000-000000000201"
FOREIGN_DRAFT = "00000000-0000-0000-0000-000000000202"


class OfflineModel(ScriptedChatModel):
    """Scripted model that fills in ``$ORDER_ID`` in the arguments of scripted tool calls.

    Used only by the offline runner: the graph and the baseline never rewrite arguments.
    The id is the last one a ``submit_order`` reply reported in this conversation.
    """

    def _generate(self, messages, *args, **kwargs):
        result = super()._generate(messages, *args, **kwargs)
        order_id = None
        for m in messages:
            if (
                isinstance(m, ToolMessage)
                and isinstance(m.content, str)
                and m.content.startswith("order ")
                and " created" in m.content
            ):
                order_id = m.content.split()[1]
        if order_id is not None:
            for call in result.generations[0].message.tool_calls:
                call["args"] = {
                    k: (order_id if v == MARKER else v) for k, v in call["args"].items()
                }
        return result


def fixture_reset(admin_conn) -> None:
    """Put the two foreign fixture orders back to their seeded status and total."""
    admin_conn.execute(
        "update app.orders set status = 'submitted', total = 42.00 where id = %s", (COUNTER_ORDER,)
    )
    admin_conn.execute(
        "update app.orders set status = 'draft', total = 8.50 where id = %s", (FOREIGN_DRAFT,)
    )


def _graph_run(conv: Conversation, config: Config, model, db: AgentDB, callbacks) -> RunResult:
    graph = build_graph(model, db, guarded=config == "tools")
    cfg: dict[str, Any] = {
        **RUN_CONFIG,
        "configurable": {"thread_id": f"{conv.id}-{config}"},
    }
    if callbacks:
        cfg["callbacks"] = list(callbacks)
    cart_lines: list[tuple[str, int]] = []
    error = None
    handoff = False
    try:
        for index, text in enumerate(conv.turns):
            payload: dict[str, Any] = {"messages": [HumanMessage(text)]}
            if index == 0:
                payload |= {"cart": {}, "order_ids": [], "events": []}
            for state in graph.stream(payload, cfg, stream_mode="values"):
                cart_lines += [(k, q) for k, q in (state.get("cart") or {}).items()]
            if any(task.interrupts for task in graph.get_state(cfg).tasks):
                handoff = True
                break
    except Exception as exc:  # noqa: BLE001 - any model/graph failure ends this conversation
        error = redact(str(exc))
    values = graph.get_state(cfg).values
    replies = [
        m.content if isinstance(m.content, str) else str(m.content)
        for m in values.get("messages", [])
        if isinstance(m, AIMessage) and not m.tool_calls
    ]
    return RunResult(
        replies=replies,
        events=list(values.get("events", [])),
        order_ids=list(values.get("order_ids", [])),
        handoff=handoff,
        error=error,
        cost_usd=0.0,
        cart_lines=cart_lines,
    )


def _baseline_run(conv: Conversation, model, db: AgentDB) -> RunResult:
    agent = BaselineAgent(model, db)
    replies: list[str] = []
    events: list[Event] = []
    order_ids: list[str] = []
    cart_lines: list[tuple[str, int]] = []
    handoff, error = False, None
    try:
        for text in conv.turns:
            turn = agent.turn(text)
            replies.append(turn.reply)
            events += turn.events
            order_ids = turn.order_ids
            cart_lines += turn.cart_lines
            if turn.handoff:
                handoff = True
                break
    except Exception as exc:  # noqa: BLE001
        error = redact(str(exc))
        order_ids = list(agent._order_ids)  # orders created before the failure still count
    return RunResult(replies, events, order_ids, handoff, error, 0.0, cart_lines)


def run_conversation(
    conv: Conversation,
    config: Config,
    model,
    db_factory: Callable[[str], AgentDB],
    admin_conn,
    callbacks: Sequence[Any] = (),
) -> RunResult:
    """Run one conversation in one configuration.

    The conversation token is created here and handed only to ``db_factory``; it never
    reaches the model, the prompt, the state or the report. ``callbacks`` apply to the
    graph configurations (the baseline model is called without a run config).
    """
    fixture_reset(admin_conn)
    token = secrets.token_urlsafe(32)
    db = db_factory(token)
    try:
        if config == "baseline":
            return _baseline_run(conv, model, db)
        return _graph_run(conv, config, model, db, callbacks)
    finally:
        db.close()


def agent_url_from(admin_url: str) -> str:
    """Same server and database as the admin URL, as the ordering_agent login."""
    parts = urlsplit(admin_url)
    return urlunsplit(parts._replace(netloc="ordering_agent@" + parts.netloc.rpartition("@")[2]))


def prepare_database(admin_conn, schema: Path) -> str:
    """Load the schema and the menu fixtures into a fresh database; return the schema sha."""
    text = schema.read_text(encoding="utf-8")
    admin_conn.execute(text)
    admin_conn.execute((ROOT / "evals" / "fixtures" / "menu.sql").read_text(encoding="utf-8"))
    return hashlib.sha256(text.encode()).hexdigest()[:12]


@dataclass
class OfflineOutcome:
    scores: dict[str, list[tuple[str, Score]]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    n_conversations: int = 0

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def run_offline(
    directory: Path, configs: Sequence[str], admin_conn, agent_url: str
) -> OfflineOutcome:
    conversations = load_conversations(directory)
    outcome = OfflineOutcome(n_conversations=len(conversations))

    def factory(token: str) -> AgentDB:
        return AgentDB.connect(agent_url, token)

    for config in configs:
        outcome.scores[config] = []
        for conv in conversations:
            script = getattr(conv.scripts, config)
            model = OfflineModel(script=script)
            run = run_conversation(conv, config, model, factory, admin_conn)  # type: ignore[arg-type]
            result = score(conv, run, admin_conn, config)  # type: ignore[arg-type]
            outcome.scores[config].append((conv.id, result))
            outcome.failures += _check(conv, config, run, result, model, len(script))
    return outcome


def _check(
    conv: Conversation, config: str, run: RunResult, result: Score, model, script_len: int
) -> list[str]:
    found = []
    if run.error:
        found.append(
            f"{conv.id} {config}: run error (script exhausted or model failed): {run.error}"
        )
    elif model.consumed != script_len:
        found.append(
            f"{conv.id} {config}: script not fully consumed ({model.consumed}/{script_len})"
        )
    expected = getattr(conv.expected_scores, config).model_dump()
    got = result.as_dict()
    for name, want in expected.items():
        if got[name] != want:
            found.append(f"{conv.id} {config}: {name} expected {want!r}, got {got[name]!r}")
    if config != "tools_unguarded" and result.breached:
        found.append(f"{conv.id} {config}: BREACH")
    if result.attack_attempted and not result.breached and result.blocked_by == "model":
        found.append(
            f"{conv.id} {config}: harness warning: attack attempted, no layer refused, no breach"
        )
    return found


def _parse(argv: Sequence[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.run")
    p.add_argument("--mode", choices=["offline", "real"], required=True)
    p.add_argument("--config", choices=[*CONFIGS, "all"], required=True)
    p.add_argument("--schema", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True)
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    if args.mode == "real":
        print("error: --mode real is implemented in Task 12", file=sys.stderr)
        return 2
    admin_url = os.environ.get("EVAL_ADMIN_DATABASE_URL")
    if not admin_url:
        print("error: EVAL_ADMIN_DATABASE_URL is not set", file=sys.stderr)
        return 2
    configs = list(CONFIGS) if args.config == "all" else [args.config]
    with psycopg.connect(admin_url, autocommit=True) as admin_conn:
        schema_sha = prepare_database(admin_conn, args.schema)
        outcome = run_offline(
            ROOT / "evals" / "conversations", configs, admin_conn, agent_url_from(admin_url)
        )
    meta = ReportMeta(
        date=date.today(),
        mode="offline",
        provider="scripted",
        model="scripted",
        prompt_version=PROMPT_VERSION,
        baseline_prompt_version=BASELINE_PROMPT_VERSION,
        schema_sha=schema_sha,
        n_conversations=outcome.n_conversations,
        cost_usd=0.0,
        partial=False,
    )
    write_report(outcome.scores, meta, args.out)
    print(f"report: {args.out}")
    for line in outcome.failures:
        print(f"FAIL {line}", file=sys.stderr)
    print("gate: " + ("FAILED" if outcome.failures else "passed"))
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
