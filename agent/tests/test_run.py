"""The offline runner, the report and the gate (needs the test database)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from evals.report import ReportMeta, write_report
from evals.run import (
    CONFIGS,
    ROOT,
    OfflineModel,
    fixture_reset,
    run_conversation,
    run_offline,
)
from evals.scoring import Score
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
            {"tool_calls": [step("submit_order", customer_label="Ana")]},
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
