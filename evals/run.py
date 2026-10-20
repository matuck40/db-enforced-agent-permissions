"""Eval runner: runs conversations through the agent, scores them, writes the report.

    uv run --project agent python -m evals.run --mode offline --config all \\
        --schema schema.sql --schema migrations/001_conversation_scope.sql \\
        --out evals/reports/offline-latest.md

``--schema`` is repeatable: the files are loaded in the order given, and the report's
``schema_sha`` is taken over their concatenation. The current schema is ``schema.sql``
plus every ``migrations/*.sql`` in name order (pass one ``--schema`` per file); ``--schema schema.sql`` alone reproduces the
run from before the conversation-scope fix (its gate fails: the expected scores are
those of the current schema).

Offline, the model is a scripted fake (see ``evals/conversation.py``), so the run
measures the harness and the database, not model behaviour. The offline gate fails
(exit 1) on a breach in any configuration, a score that differs from the one the
script is expected to produce, a script that is not fully consumed (or runs out), or an
attack attempt that no layer refused and that did not breach (a harness warning).

Real mode (``--mode real``) uses the provider model of ``AGENT_MODEL`` for every
configuration, ignores the scripts and has no gate: the report is the product. It needs
``AGENT_MODEL``, the key of ``AGENT_PROVIDER`` and ``EVAL_MAX_USD`` (a positive number), and
a filled-in ``evals/prices.toml``; anything missing is an error (exit 2) before any model call.
After every model call the running cost is compared to the cap. Once it is over, the model
call in flight finishes, the current conversation stops, no later conversation starts, and the
report is written with ``Partial run: yes``.
"""

from __future__ import annotations

import argparse
import hashlib
import math
import os
import secrets
import sys
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, get_args
from urllib.parse import urlsplit, urlunsplit

import psycopg
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from evals.conversation import Conversation, load_conversations
from evals.cost import PRICES_PATH, PriceError, UsageHandler, load_prices, model_prices
from evals.report import ReportMeta, write_report
from evals.scoring import Config, RunResult, Score, score
from order_agent.baseline import BASELINE_PROMPT_VERSION, BaselineAgent
from order_agent.config import PROVIDERS, Settings
from order_agent.db import AgentDB
from order_agent.events import Event
from order_agent.fake import ScriptedChatModel
from order_agent.graph import RUN_CONFIG, build_graph
from order_agent.models import make_model
from order_agent.prompt import PROMPT_VERSION
from order_agent.redact import redact
from order_agent.tracing import flush, make_callbacks

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


def _error_text(exc: Exception) -> str:
    """The redacted message of a failure; a blank message (a bare timeout) names the type."""
    return redact(str(exc)) or type(exc).__name__


def _graph_run(
    conv: Conversation, config: Config, model, db: AgentDB, tracing: dict[str, Any] | None = None
) -> RunResult:
    graph = build_graph(model, db, guarded=config == "tools")
    cfg: dict[str, Any] = {
        **RUN_CONFIG,
        "configurable": {"thread_id": f"{conv.id}-{config}"},
        **(tracing or {}),
    }
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
        error = _error_text(exc)
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


def _baseline_run(
    conv: Conversation, model, db: AgentDB, tracing: dict[str, Any] | None = None
) -> RunResult:
    agent = BaselineAgent(model, db, tracing)
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
        error = _error_text(exc)
        order_ids = list(agent._order_ids)  # orders created before the failure still count
    return RunResult(replies, events, order_ids, handoff, error, 0.0, cart_lines)


def run_conversation(
    conv: Conversation,
    config: Config,
    model,
    db_factory: Callable[[str], AgentDB],
    admin_conn,
    callbacks: Sequence[Any] = (),
    tracing: dict[str, Any] | None = None,
) -> RunResult:
    """Run one conversation in one configuration.

    The conversation token is created here and handed only to ``db_factory``; it never
    reaches the model, the prompt, the state or the report. ``callbacks`` are attached to
    the model itself, so they see every chat-model call of all three configurations (the
    graph's and the baseline's structured-output call alike), once each. ``tracing`` is an
    optional run config (``callbacks``, ``run_name``, ``metadata``) for one trace per
    conversation; it carries no token either, and it is never set offline.
    """
    if callbacks:
        model.callbacks = [*(model.callbacks or []), *callbacks]
    fixture_reset(admin_conn)
    token = secrets.token_urlsafe(32)
    db = db_factory(token)
    try:
        if config == "baseline":
            return _baseline_run(conv, model, db, tracing)
        return _graph_run(conv, config, model, db, tracing)
    finally:
        db.close()


def agent_url_from(admin_url: str) -> str:
    """Same server and database as the admin URL, as the ordering_agent login."""
    parts = urlsplit(admin_url)
    return urlunsplit(parts._replace(netloc="ordering_agent@" + parts.netloc.rpartition("@")[2]))


def prepare_database(admin_conn, schemas: Sequence[Path]) -> str:
    """Load the schema files in order, then the menu fixtures, into a fresh database.

    Returns the sha of the concatenated schema files, so one file alone gives the same
    sha as before ``--schema`` became repeatable.
    """
    texts = [path.read_text(encoding="utf-8") for path in schemas]
    for text in texts:
        admin_conn.execute(text)
    admin_conn.execute((ROOT / "evals" / "fixtures" / "menu.sql").read_text(encoding="utf-8"))
    return hashlib.sha256("".join(texts).encode()).hexdigest()[:12]


@dataclass
class OfflineOutcome:
    scores: dict[str, list[tuple[str, Score]]] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    n_conversations: int = 0

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0


def run_offline(
    directory: Path, configs: Sequence[str], admin_conn, agent_url: str, only: str | None = None
) -> OfflineOutcome:
    conversations = load_conversations(directory, only)
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


@dataclass
class RealOutcome:
    scores: dict[str, list[tuple[str, Score]]] = field(default_factory=dict)
    errors: list[tuple[str, str, str]] = field(default_factory=list)  # conversation, config, text
    cost_usd: float = 0.0
    partial: bool = False
    n_conversations: int = 0
    calls_without_usage: int = 0
    # In every trace seed and, as eval_run_id, in trace metadata; None: no tracing.
    trace_run_id: str | None = None


def trace_config(
    conv_id: str,
    session: str,
    prompt_version: str,
    schema_sha: str,
    run_id: str | None,
    handlers: Sequence[Any],
) -> dict[str, Any]:
    """The run config that makes one conversation one trace (callbacks, name, metadata)."""
    return {
        "callbacks": list(handlers),
        "run_name": conv_id,
        "metadata": {
            "langfuse_session_id": session,
            "prompt_version": prompt_version,
            "schema_sha": schema_sha,
            # Not "run_id": LangGraph reads that key from the config and the graph stops
            # calling the model after the first turn.
            "eval_run_id": run_id,
        },
    }


def run_real(
    directory: Path,
    configs: Sequence[str],
    admin_conn,
    agent_url: str,
    make_model_fn: Callable[[], Any],
    model_name: str,
    prices: dict[str, Any],
    max_usd: float,
    settings: Settings | None = None,
    config_date: date | None = None,
    prompt_version: str = PROMPT_VERSION,
    schema_sha: str = "",
    run_nonce: str | None = None,
    only: str | None = None,
) -> RealOutcome:
    """Run every conversation in every configuration with a real model, under a cost cap.

    ``make_model_fn`` builds a fresh model per run (the same model for all configurations).
    Customer turns are fixed; the scripts are not used. A failure in one run (a provider
    error, a timeout) is recorded as that run's error and the run goes on.

    With ``settings.langfuse`` and the ``tracing`` extra installed, every conversation in every
    configuration is one Langfuse trace (all its turns share a trace id), named after the
    conversation, in session ``<date>-<config>``. Cost is known only after the run, so it stays
    in the report, not on the trace.
    """
    model_prices(model_name, prices)  # fails before any model exists
    conversations = load_conversations(directory, only)
    outcome = RealOutcome(scores={config: [] for config in configs})
    handler = UsageHandler(model_name, prices, max_usd)
    tracers: list[Any] = []  # every handler built, to flush at the end
    if settings is not None and settings.langfuse:
        # One nonce per call, so two runs on the same day never share a trace id. Random, not
        # derived from anything of the conversation. A probe handler is built before any model
        # call, so a broken extra fails here and not after money was spent.
        outcome.trace_run_id = run_nonce or uuid.uuid4().hex[:8]
        make_callbacks(settings, "real", trace_seed=f"probe-{outcome.trace_run_id}")
    day = (config_date or date.today()).isoformat()

    def factory(token: str) -> AgentDB:
        return AgentDB.connect(agent_url, token)

    try:
        for conv in conversations:
            if handler.over_cap:
                break
            outcome.n_conversations += 1
            for config in configs:
                if handler.over_cap:
                    break
                before = handler.total_usd
                session = f"{day}-{config}"
                # One handler per conversation and configuration, pinned to one trace id.
                handlers = (
                    make_callbacks(
                        settings, "real", trace_seed=f"{session}-{outcome.trace_run_id}-{conv.id}"
                    )
                    if settings is not None
                    else []
                )
                tracers += handlers
                tracing = (
                    trace_config(
                        conv.id, session, prompt_version, schema_sha, outcome.trace_run_id, handlers
                    )
                    if handlers
                    else None
                )
                run = run_conversation(
                    conv,
                    config,
                    make_model_fn(),
                    factory,
                    admin_conn,  # type: ignore[arg-type]
                    callbacks=[handler],
                    tracing=tracing,
                )
                run = replace(run, cost_usd=handler.total_usd - before)
                outcome.scores[config].append((conv.id, score(conv, run, admin_conn, config)))  # type: ignore[arg-type]
                if run.error:
                    outcome.errors.append((conv.id, config, run.error))
    finally:
        flush(tracers)
    outcome.cost_usd = handler.total_usd
    outcome.calls_without_usage = handler.calls_without_usage
    outcome.partial = handler.over_cap
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
    if result.breached:
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
    p.add_argument(
        "--schema",
        type=Path,
        action="append",
        required=True,
        help="schema file to load; repeat it to load several, in the order given",
    )
    p.add_argument("--out", type=Path, help="report path (default for real: evals/reports/)")
    p.add_argument("--only", help="glob on conversation ids to run a subset (e.g. 'attack-*')")
    return p.parse_args(argv)


def default_real_out(today: date) -> Path:
    return ROOT / "evals" / "reports" / f"{today.isoformat()}-real.md"


class UsageError(Exception):
    """A missing or invalid setting: reported as ``error: ...`` with exit code 2."""


def _real_settings(admin_url: str | None) -> tuple[Settings, float]:
    """Check everything real mode needs, before any database or model work."""
    env = os.environ
    if not admin_url:
        raise UsageError("EVAL_ADMIN_DATABASE_URL is not set")
    if not env.get("AGENT_MODEL"):
        raise UsageError("AGENT_MODEL is required in real mode")
    provider = env.get("AGENT_PROVIDER") or "openai"
    if provider not in PROVIDERS:
        raise UsageError(f"AGENT_PROVIDER must be one of {PROVIDERS}")
    key = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}[provider]
    if not env.get(key):
        raise UsageError(f"{key} is required for AGENT_PROVIDER={provider}")
    raw = env.get("EVAL_MAX_USD")
    try:
        cap = float(raw) if raw else math.nan
    except ValueError:
        cap = math.nan
    if not math.isfinite(cap) or cap <= 0:
        raise UsageError("EVAL_MAX_USD must be a number greater than 0 (the cost cap in USD)")
    # The agent URL is built the way offline mode builds it, so AGENT_DATABASE_URL is not needed.
    settings = Settings.from_env({**env, "AGENT_DATABASE_URL": agent_url_from(admin_url)})
    return settings, cap


def _main_real(args: argparse.Namespace, configs: list[str]) -> int:
    settings, cap = _real_settings(os.environ.get("EVAL_ADMIN_DATABASE_URL"))
    model_name = settings.model or ""
    try:
        prices = load_prices(PRICES_PATH)
        model_prices(model_name, prices)
    except (PriceError, OSError, ValueError) as exc:
        raise UsageError(str(exc)) from exc
    admin_url = os.environ["EVAL_ADMIN_DATABASE_URL"]
    with psycopg.connect(admin_url, autocommit=True) as admin_conn:
        schema_sha = prepare_database(admin_conn, args.schema)
        outcome = run_real(
            ROOT / "evals" / "conversations",
            configs,
            admin_conn,
            agent_url_from(admin_url),
            lambda: make_model(settings),
            model_name,
            prices,
            cap,
            settings=settings,
            schema_sha=schema_sha,
            only=args.only,
        )
    meta = ReportMeta(
        date=date.today(),
        mode="real",
        provider=settings.provider,
        model=model_name,
        prompt_version=PROMPT_VERSION,
        baseline_prompt_version=BASELINE_PROMPT_VERSION,
        schema_sha=schema_sha,
        n_conversations=outcome.n_conversations,
        cost_usd=outcome.cost_usd,
        partial=outcome.partial,
        calls_without_usage=outcome.calls_without_usage,
        trace_run_id=outcome.trace_run_id,
        only=args.only,
    )
    out = args.out or default_real_out(meta.date)
    write_report(outcome.scores, meta, out, errors=outcome.errors)
    print(f"report: {out}")
    print(
        f"cost: {outcome.cost_usd:.4f} USD of {cap} USD"
        + (" (partial run)" if outcome.partial else "")
    )
    return 0


def _main_offline(args: argparse.Namespace, configs: list[str]) -> int:
    admin_url = os.environ.get("EVAL_ADMIN_DATABASE_URL")
    if not admin_url:
        raise UsageError("EVAL_ADMIN_DATABASE_URL is not set")
    if args.out is None:
        raise UsageError("--out is required in offline mode")
    with psycopg.connect(admin_url, autocommit=True) as admin_conn:
        schema_sha = prepare_database(admin_conn, args.schema)
        outcome = run_offline(
            ROOT / "evals" / "conversations",
            configs,
            admin_conn,
            agent_url_from(admin_url),
            only=args.only,
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
        only=args.only,
    )
    write_report(outcome.scores, meta, args.out)
    print(f"report: {args.out}")
    for line in outcome.failures:
        print(f"FAIL {line}", file=sys.stderr)
    print("gate: " + ("FAILED" if outcome.failures else "passed"))
    return outcome.exit_code


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse(argv)
    configs = list(CONFIGS) if args.config == "all" else [args.config]
    try:
        if args.mode == "real":
            return _main_real(args, configs)
        return _main_offline(args, configs)
    except UsageError as exc:
        print(f"error: {redact(str(exc))}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - last line of defence: no raw traceback or secret
        print(f"error: {_error_text(exc)}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
