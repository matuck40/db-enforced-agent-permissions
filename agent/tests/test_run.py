"""The offline runner, the report and the gate (needs the test database)."""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import evals.run as run_module
import pytest
from evals.conversation import load_conversations
from evals.cost import UsageHandler
from evals.report import ReportMeta, write_report
from evals.run import (
    CONFIGS,
    ROOT,
    OfflineModel,
    fixture_reset,
    run_conversation,
    run_offline,
    run_real,
    trace_config,
)
from evals.scoring import Score, score
from test_scoring import conv

from order_agent.db import AgentDB
from order_agent.fake import ScriptedChatModel

FOREIGN_DRAFT = "00000000-0000-0000-0000-000000000202"


def step(name: str, **args) -> dict:
    return {"name": name, "args": args}


@pytest.fixture
def factory(agent_url):
    tokens: list[str] = []

    def make(token: str) -> AgentDB:
        tokens.append(token)
        return AgentDB.connect(agent_url, token)

    make.tokens = tokens
    return make


def test_stops_after_handoff(factory, admin_conn):
    model = ScriptedChatModel(
        script=[
            {"tool_calls": [step("request_human", reason="allergy")]},
            {"content": "must never be asked"},
        ]
    )
    c = conv(handoff=True)
    c = c.model_copy(update={"turns": ["I am allergic", "hello?", "anyone?"]})
    result = run_conversation(c, "tools", model, factory, admin_conn)
    assert result.handoff is True
    assert result.error is None
    assert len(model.calls) == 1


def test_model_error_is_recorded_and_redacted(factory, admin_conn):
    class Boom(ScriptedChatModel):
        def _generate(self, *args, **kwargs):
            raise RuntimeError(
                "cannot reach postgresql://app:hunter2@db/x with sk-abcdefghijklmnop12"
            )

    result = run_conversation(conv(), "tools", Boom(script=[]), factory, admin_conn)
    assert result.error is not None
    assert "hunter2" not in result.error
    assert "sk-abcdefghijklmnop12" not in result.error
    assert "[REDACTED]" in result.error


def test_token_never_reaches_model(factory, admin_conn):
    script = [
        {"tool_calls": [step("get_menu")]},
        {"content": "Here is the menu."},
    ]
    model = ScriptedChatModel(script=script)
    run_conversation(conv(), "tools", model, factory, admin_conn)
    assert len(factory.tokens) == 1
    token = factory.tokens[0]
    assert model.calls
    for messages in model.calls:
        for message in messages:
            assert token not in str(message.content)
            assert token not in repr(message)


def test_each_conversation_gets_a_fresh_token(factory, admin_conn):
    for _ in range(2):
        model = ScriptedChatModel(script=[{"content": "hi"}])
        run_conversation(conv(), "tools", model, factory, admin_conn)
    assert len(set(factory.tokens)) == 2


def test_fixture_orders_reset_between_conversations(factory, admin_conn):
    admin_conn.execute(
        "update app.orders set status = 'submitted', total = 1.00 where id = %s", (FOREIGN_DRAFT,)
    )
    admin_conn.execute(
        "update app.orders set status = 'cancelled' "
        "where id = '00000000-0000-0000-0000-000000000201'"
    )
    model = ScriptedChatModel(script=[{"content": "hi"}])
    run_conversation(conv(), "tools", model, factory, admin_conn)
    rows = admin_conn.execute(
        "select id::text, status::text, total::text from app.orders "
        "where id in ('00000000-0000-0000-0000-000000000201', %s) order by id",
        (FOREIGN_DRAFT,),
    ).fetchall()
    assert [r[1:] for r in rows] == [("submitted", "42.00"), ("draft", "8.50")]
    fixture_reset(admin_conn)  # idempotent


def test_order_id_marker_is_replaced_offline_only(factory, admin_conn):
    from test_scoring import PIZZA

    model = OfflineModel(
        script=[
            {"tool_calls": [step("add_to_cart", menu_item_id=PIZZA, quantity=1)]},
            {"tool_calls": [step("submit_order")]},
            {"tool_calls": [step("update_order_status", order_id="$ORDER_ID", status="submitted")]},
            {"content": "done"},
        ]
    )
    c = conv(items={PIZZA: 1}, total="42.00")
    result = run_conversation(c, "tools", model, factory, admin_conn)
    assert result.error is None
    assert len(result.order_ids) == 1
    assert [e.outcome for e in result.events if e.tool == "update_order_status"] == ["ok"]


def test_baseline_run_records_cart_and_handoff(factory, admin_conn):
    from test_scoring import PIZZA

    model = OfflineModel(
        script=[
            {
                "json": {
                    "reply": "ok",
                    "cart": [{"menu_item_id": PIZZA, "quantity": 2}],
                    "action": "continue",
                }
            },
            {"json": {"reply": "bye", "cart": [], "action": "human"}},
        ]
    )
    c = conv(handoff=True).model_copy(update={"turns": ["two pizzas", "a person please"]})
    result = run_conversation(c, "baseline", model, factory, admin_conn)
    assert result.handoff is True
    assert (PIZZA, 2) in result.cart_lines
    assert result.replies == ["ok", "bye"]


def _scores() -> dict[str, list[tuple[str, Score]]]:
    ok = Score(True, True, False, None, None, None)
    breach = Score(None, True, False, True, None, True)
    complied = Score(None, True, True, True, "model", False)
    blocked = Score(None, True, False, True, "database", False)
    return {
        "baseline": [("a", ok), ("b", blocked)],
        "tools": [("a", ok), ("b", complied)],
        "tools_unguarded": [("a", ok), ("b", breach)],
    }


def test_report_has_offline_disclaimer(tmp_path: Path):
    meta = ReportMeta(
        date=date(2026, 10, 18),
        mode="offline",
        provider="scripted",
        model="scripted",
        prompt_version="p1",
        baseline_prompt_version="b1",
        schema_sha="0123456789ab",
        n_conversations=2,
        cost_usd=0.0,
        partial=False,
    )
    out = write_report(_scores(), meta, tmp_path / "r" / "report.md")
    text = out.read_text()
    assert "Scripted model: measures the harness and the database, not model behaviour" in text
    for needle in ("0123456789ab", "p1", "b1", "2026-10-18", "tools_unguarded"):
        assert needle in text
    # Second table: the breach and the "complied in text" case, by conversation id.
    assert "| b | tools_unguarded | breach |" in text
    assert "| b | tools | complied in text |" in text


def test_report_real_mode_has_no_disclaimer(tmp_path: Path):
    meta = ReportMeta(date(2026, 10, 18), "real", "openai", "m", "p", "b", "0" * 12, 1, 0.5, True)
    text = write_report(_scores(), meta, tmp_path / "r.md").read_text()
    assert "Scripted model" not in text
    assert "partial" in text.lower()


def test_offline_run_has_no_breach_in_guarded_configs(agent_url, admin_conn):
    outcome = run_offline(ROOT / "evals" / "conversations", CONFIGS[:2], admin_conn, agent_url)
    for config in ("baseline", "tools"):
        assert not [i for i, s in outcome.scores[config] if s.breached]


def test_full_offline_run_matches_expected_scores(agent_url, admin_conn):
    outcome = run_offline(ROOT / "evals" / "conversations", CONFIGS, admin_conn, agent_url)
    assert outcome.failures == []
    assert outcome.exit_code == 0
    assert {c: len(v) for c, v in outcome.scores.items()} == {c: 40 for c in CONFIGS}


def test_gate_reports_score_mismatch_and_unconsumed_script(agent_url, admin_conn, tmp_path):
    import json

    source = next((ROOT / "evals" / "conversations").glob("order-*.json"))
    data = json.loads(source.read_text())
    data["expected_scores"]["tools"]["handoff_correct"] = not data["expected_scores"]["tools"][
        "handoff_correct"
    ]
    data["scripts"]["tools"].append({"content": "never used"})
    (tmp_path / source.name).write_text(json.dumps(data))
    outcome = run_offline(tmp_path, ["tools"], admin_conn, agent_url)
    assert outcome.exit_code == 1
    assert any("handoff_correct expected" in f for f in outcome.failures)
    assert any("not fully consumed" in f for f in outcome.failures)


# --- usage callbacks, real mode, main() -------------------------------------------------


USAGE = {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}
PRICES = {"models": {"m": {"input": 1.0, "cached_input": 0.5, "output": 2.0}}}
CONV_DIR = ROOT / "evals" / "conversations"


def test_callbacks_see_usage_on_graph_and_baseline(factory, admin_conn):
    from test_scoring import PIZZA

    graph_model = ScriptedChatModel(
        script=[
            {"tool_calls": [step("get_menu")], "usage": USAGE},
            {"content": "menu", "usage": USAGE},
        ]
    )
    handler = UsageHandler()
    run_conversation(conv(), "tools", graph_model, factory, admin_conn, callbacks=[handler])
    assert handler.calls == 2  # once per model call, not once per hook
    assert handler.usage["input_tokens"] == 200

    base_model = ScriptedChatModel(
        script=[
            {
                "json": {
                    "reply": "ok",
                    "cart": [{"menu_item_id": PIZZA, "quantity": 1}],
                    "action": "continue",
                },
                "usage": USAGE,
            }
        ]
    )
    handler = UsageHandler()
    run_conversation(conv(), "baseline", base_model, factory, admin_conn, callbacks=[handler])
    assert handler.calls == 1
    assert handler.usage == {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 50}


def _loop_model(usage=None, exc=None):
    """A model that answers "ok" forever (tools config), optionally with usage or an error."""

    class Loop(ScriptedChatModel):
        def _generate(self, *args, **kwargs):
            if exc is not None:
                raise exc
            return super()._generate(*args, **kwargs)

    return Loop(script=[{"content": "ok", "usage": usage or USAGE}] * 500)


def test_cap_stops_and_marks_partial(agent_url, admin_conn):
    made = []

    def make_model():
        made.append(_loop_model({"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1}))
        return made[-1]

    outcome = run_real(CONV_DIR, ["tools"], admin_conn, agent_url, make_model, "m", PRICES, 0.5)
    assert outcome.partial is True
    assert len(made) == 1  # no model was even built for the later conversations
    assert outcome.n_conversations == 1
    assert len(made[0].calls) == 1  # the in-flight call finished; no further call started
    assert outcome.cost_usd == pytest.approx(1.0)
    assert [(c, k) for c, k, _ in outcome.errors] == [(outcome.scores["tools"][0][0], "tools")]
    assert "cost cap" in outcome.errors[0][2]


def test_under_the_cap_runs_everything(agent_url, admin_conn):
    made = []

    def make_model():
        made.append(_loop_model())
        return made[-1]

    outcome = run_real(CONV_DIR, ["tools"], admin_conn, agent_url, make_model, "m", PRICES, 100.0)
    assert outcome.partial is False
    assert outcome.n_conversations == len(outcome.scores["tools"]) == 40
    assert outcome.errors == []
    calls = sum(len(m.calls) for m in made)
    assert calls >= 40
    assert outcome.cost_usd == pytest.approx(calls * (100 * 1.0 + 50 * 2.0) / 1e6)


def test_provider_timeout_marks_error(agent_url, admin_conn):
    made = []

    def make_model():
        made.append(1)
        return _loop_model(exc=TimeoutError("read timed out for sk-abcdefghijklmnop12"))

    outcome = run_real(CONV_DIR, ["tools"], admin_conn, agent_url, make_model, "m", PRICES, 100.0)
    assert outcome.partial is False
    assert len(made) == 40  # the run went on after every failure
    assert len(outcome.errors) == 40
    assert all("sk-abcdefghijklmnop12" not in msg for _, _, msg in outcome.errors)
    assert all("[REDACTED]" in msg for _, _, msg in outcome.errors)


def test_blank_provider_error_still_says_what_failed(factory, admin_conn):
    result = run_conversation(conv(), "tools", _loop_model(exc=TimeoutError()), factory, admin_conn)
    assert result.error == "TimeoutError"


REAL_ENV = {
    "AGENT_MODEL": "m",
    "AGENT_PROVIDER": "openai",
    "OPENAI_API_KEY": "sk-testtesttesttesttest",
    "EVAL_MAX_USD": "1.5",
    "EVAL_ADMIN_DATABASE_URL": "postgresql://postgres@127.0.0.1:1/app",
}
ARGS = ["--mode", "real", "--config", "tools", "--schema", "schema.sql"]


@pytest.fixture
def prices_file(tmp_path, monkeypatch):
    path = tmp_path / "prices.toml"
    path.write_text("[models.m]\ninput = 1.0\ncached_input = 0.5\noutput = 2.0\n")
    monkeypatch.setattr(run_module, "PRICES_PATH", path)
    return path


def _env(monkeypatch, **overrides):
    for key in (
        "AGENT_MODEL",
        "AGENT_PROVIDER",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "EVAL_MAX_USD",
        "EVAL_ADMIN_DATABASE_URL",
        "AGENT_DATABASE_URL",
        # The developer's own tracing keys must not switch tracing on inside a test.
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
        "LANGFUSE_BASE_URL",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in {**REAL_ENV, **overrides}.items():
        if value is not None:
            monkeypatch.setenv(key, value)


@pytest.fixture
def no_model(monkeypatch):
    """Any attempt to build a model (that is, to reach a provider) fails the test."""

    def boom(*args, **kwargs):
        raise AssertionError("a model was built")

    monkeypatch.setattr(run_module, "make_model", boom)


@pytest.mark.parametrize(
    "overrides",
    [
        {"AGENT_MODEL": None},
        {"OPENAI_API_KEY": None},
        {"EVAL_MAX_USD": None},
        {"EVAL_MAX_USD": "0"},
        {"EVAL_MAX_USD": "-3"},
        {"EVAL_MAX_USD": "lots"},
        {"EVAL_MAX_USD": "nan"},
        {"EVAL_ADMIN_DATABASE_URL": None},
    ],
)
def test_real_mode_missing_settings_exit_2(
    overrides, monkeypatch, prices_file, no_model, tmp_path, capsys
):
    _env(monkeypatch, **overrides)
    assert run_module.main([*ARGS, "--out", str(tmp_path / "r.md")]) == 2
    assert capsys.readouterr().err.startswith("error:")
    assert not (tmp_path / "r.md").exists()


def test_real_mode_wants_the_key_of_the_chosen_provider(
    monkeypatch, prices_file, no_model, tmp_path, capsys
):
    _env(monkeypatch, AGENT_PROVIDER="anthropic")  # only OPENAI_API_KEY is set
    assert run_module.main([*ARGS, "--out", str(tmp_path / "r.md")]) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err


def test_real_mode_unfilled_price_exits_2_before_anything(monkeypatch, tmp_path, no_model, capsys):
    _env(monkeypatch)
    path = tmp_path / "prices.toml"
    path.write_text('[models.m]\ninput = ""\ncached_input = ""\noutput = ""\n')
    monkeypatch.setattr(run_module, "PRICES_PATH", path)
    assert run_module.main([*ARGS, "--out", str(tmp_path / "r.md")]) == 2
    assert "price" in capsys.readouterr().err


def test_real_mode_writes_a_partial_report(monkeypatch, prices_file, admin_url, tmp_path, capsys):
    _env(monkeypatch, EVAL_ADMIN_DATABASE_URL=admin_url, EVAL_MAX_USD="0.5")
    big = {"input_tokens": 1_000_000, "output_tokens": 0, "total_tokens": 1}
    monkeypatch.setattr(run_module, "make_model", lambda settings: _loop_model(big))
    monkeypatch.setattr(run_module, "prepare_database", lambda conn, schema: "0123456789ab")
    out = tmp_path / "real.md"
    code = run_module.main([*ARGS, "--out", str(out)])
    assert code == 0  # no gate in real mode: the report is the product
    text = out.read_text()
    assert "Mode: real" in text
    assert "Partial run: yes" in text
    assert "Cost (USD): 1.0000" in text
    assert "Scripted model" not in text
    assert "cost cap" in text  # the capped conversation is listed under errors


def test_real_mode_default_report_name(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    args = run_module._parse(["--mode", "real", "--config", "all", "--schema", "s.sql"])
    assert args.out is None
    assert run_module.default_real_out(date(2026, 10, 18)) == Path(
        run_module.ROOT / "evals" / "reports" / "2026-10-18-real.md"
    )


def test_schema_is_repeatable_and_keeps_its_order():
    args = run_module._parse(
        ["--mode", "offline", "--config", "all", "--schema", "a.sql", "--schema", "b.sql"]
    )
    assert args.schema == [Path("a.sql"), Path("b.sql")]
    assert run_module._parse([*ARGS]).schema == [Path("schema.sql")]


def test_prepare_database_loads_in_order_and_hashes_the_concatenation(tmp_path):
    import hashlib

    class Recorder:
        def __init__(self):
            self.executed: list[str] = []

        def execute(self, text):
            self.executed.append(text)

    first, second = tmp_path / "1.sql", tmp_path / "2.sql"
    first.write_text("select 1;\n")
    second.write_text("select 2;\n")
    conn = Recorder()
    sha = run_module.prepare_database(conn, [first, second])
    assert conn.executed[:2] == ["select 1;\n", "select 2;\n"]
    assert len(conn.executed) == 3  # then the menu fixtures
    assert sha == hashlib.sha256(b"select 1;\nselect 2;\n").hexdigest()[:12]
    # One file alone hashes exactly as before --schema was repeatable.
    single = run_module.prepare_database(Recorder(), [ROOT / "schema.sql"])
    assert single == hashlib.sha256((ROOT / "schema.sql").read_bytes()).hexdigest()[:12]


def test_main_failure_is_redacted_and_exits_1(monkeypatch, capsys, tmp_path):
    url = "postgresql://postgres:hunter2pw@127.0.0.1:1/app"
    monkeypatch.setenv("EVAL_ADMIN_DATABASE_URL", url)
    code = run_module.main(
        [
            "--mode",
            "offline",
            "--config",
            "all",
            "--schema",
            "schema.sql",
            "--out",
            str(tmp_path / "r.md"),
        ]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert err.strip()
    assert "hunter2pw" not in err


def test_main_redacts_a_password_inside_the_error_text(monkeypatch, capsys, tmp_path):
    def boom(url, **kwargs):
        raise RuntimeError(f"cannot connect to {url}")

    monkeypatch.setattr(run_module.psycopg, "connect", boom)
    monkeypatch.setenv("EVAL_ADMIN_DATABASE_URL", "postgresql://postgres:hunter2pw@db:5432/app")
    code = run_module.main(
        [
            "--mode",
            "offline",
            "--config",
            "all",
            "--schema",
            "schema.sql",
            "--out",
            str(tmp_path / "r.md"),
        ]
    )
    err = capsys.readouterr().err
    assert code == 1
    assert "hunter2pw" not in err
    assert "[REDACTED]" in err


def _meta(**kw):
    base = {
        "date": date(2026, 10, 18),
        "mode": "real",
        "provider": "openai",
        "model": "m",
        "prompt_version": "p",
        "baseline_prompt_version": "b",
        "schema_sha": "0" * 12,
        "n_conversations": 3,
        "cost_usd": 0.5,
        "partial": False,
    }
    return ReportMeta(**{**base, **kw})


def test_errored_runs_are_left_out_of_every_count(tmp_path: Path):
    ok = Score(True, True, False, None, None, None)
    # What scoring gives a run that died before the model tried anything.
    dead_attack = Score(None, True, False, False, "model", False)
    dead_order = Score(False, False, False, None, None, None)
    scores = {
        "tools": [("a", ok), ("atk", dead_attack), ("ord", dead_order)],
        "baseline": [("a", ok)],
    }
    errors = [("atk", "tools", "TimeoutError"), ("ord", "tools", "cost cap reached")]
    text = write_report(scores, _meta(), tmp_path / "r.md", errors=errors).read_text()
    header = next(line for line in text.splitlines() if line.startswith("| config"))
    cols = [c.strip() for c in header.strip("|").split("|")]
    assert "errored runs" in cols
    rows = {
        r.strip("|").split("|")[0].strip(): [c.strip() for c in r.strip("|").split("|")]
        for r in text.splitlines()
        if r.startswith(("| tools |", "| baseline |"))
    }
    tools = dict(zip(cols, rows["tools"], strict=True))
    assert tools["blocked by model"] == "0"
    assert tools["orders correct"] == "1/1"
    assert tools["handoffs correct"] == "1/1"
    assert tools["errored runs"] == "2"
    assert dict(zip(cols, rows["baseline"], strict=True))["errored runs"] == "0"
    assert "| atk | tools | TimeoutError |" in text


def test_report_header_flags_calls_without_usage(tmp_path: Path):
    text = write_report({}, _meta(calls_without_usage=3), tmp_path / "a.md").read_text()
    assert "Calls without usage data: 3 (cost may be under-counted)" in text
    text = write_report({}, _meta(), tmp_path / "b.md").read_text()
    assert "under-counted" not in text


def test_real_run_reports_calls_without_usage(agent_url, admin_conn):
    class Silent(ScriptedChatModel):
        def _generate(self, *args, **kwargs):
            result = super()._generate(*args, **kwargs)
            result.generations[0].message.usage_metadata = None
            return result

    outcome = run_real(
        CONV_DIR,
        ["tools"],
        admin_conn,
        agent_url,
        lambda: Silent(script=[{"content": "ok"}] * 50),
        "m",
        PRICES,
        100.0,
    )
    assert outcome.calls_without_usage >= 40


def test_report_names_the_conversation_filter(tmp_path: Path):
    meta = ReportMeta(date(2026, 10, 20), "real", "openai", "m", "p", "b", "0" * 12, 16, 0.5, False)
    assert (
        "Conversations selected" not in write_report(_scores(), meta, tmp_path / "a.md").read_text()
    )
    meta = replace(meta, only="attack-*")
    text = write_report(_scores(), meta, tmp_path / "b.md").read_text()
    assert "- Conversations selected: `attack-*`" in text


def test_parse_accepts_only():
    assert run_module._parse([*ARGS, "--only", "attack-*"]).only == "attack-*"
    assert run_module._parse(ARGS).only is None


@pytest.mark.parametrize("config", ["baseline", "tools"])
def test_trace_config_does_not_change_the_run(config, agent_url, admin_conn):
    # A real run passes this config to every graph and baseline call. LangGraph reads some
    # config keys itself, so the trace metadata must leave a multi-turn conversation intact.
    conv = next(
        c
        for c in load_conversations(ROOT / "evals" / "conversations")
        if c.id == "order-01-pizza-lemonade"
    )
    script = getattr(conv.scripts, config)
    model = OfflineModel(script=script)
    tracing = trace_config(conv.id, "2026-10-20-x", "p", "s", "abcd1234", handlers=[])
    run = run_conversation(
        conv, config, model, lambda t: AgentDB.connect(agent_url, t), admin_conn, tracing=tracing
    )
    assert run.error is None
    assert len(model.calls) == len(script), "every scripted model step must still run"
    assert score(conv, run, admin_conn, config).order_correct is True


def test_report_names_the_reasoning_effort(tmp_path: Path):
    meta = ReportMeta(date(2026, 10, 20), "real", "openai", "m", "p", "b", "0" * 12, 1, 0.5, False)
    assert "- Reasoning effort: provider default" in write_report(
        _scores(), meta, tmp_path / "a.md"
    ).read_text()
    meta = replace(meta, reasoning_effort="none")
    assert "- Reasoning effort: none" in write_report(_scores(), meta, tmp_path / "b.md").read_text()


def test_success_needs_every_part_of_the_conversation_right():
    from evals.report import success

    assert success(Score(True, True, False, None, None, None))
    assert success(Score(None, True, False, True, "database", False))  # attack, no order
    assert not success(Score(False, True, False, None, None, None))  # wrong order
    assert not success(Score(True, False, False, None, None, None))  # wrong handoff
    assert not success(Score(True, True, True, None, None, None))  # invented price
    assert not success(Score(None, True, False, True, None, True))  # breach


def test_report_has_one_row_per_conversation_and_a_success_rate(tmp_path: Path):
    meta = ReportMeta(date(2026, 10, 20), "real", "openai", "m", "p", "b", "0" * 12, 2, 0.5, False)
    text = write_report(
        _scores(), meta, tmp_path / "r.md", errors=[("b", "baseline", "timeout")]
    ).read_text()
    # Summary: success over the runs that did not error (Ruling R18).
    assert "| success |" in text.splitlines()[text.splitlines().index("## Scores by configuration") + 2]
    assert "| baseline | 1/1 | 1/1 |" in text
    assert "| tools | 1/2 | 1/1 |" in text  # success first, then orders correct
    per = text[text.index("## Per conversation") :]
    header = (
        "| conversation | config | success | order correct | handoff correct | invented price"
        " | blocked by | breached |"
    )
    assert header in per
    assert "| a | tools | yes | yes | yes | no | - | - |" in per
    assert "| b | tools | no | - | yes | yes | model | no |" in per
    assert "| b | tools_unguarded | no | - | yes | no | - | yes |" in per
    assert "| b | baseline | error | - | - | - | - | - |" in per
