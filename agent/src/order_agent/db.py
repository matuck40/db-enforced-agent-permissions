"""AgentDB: the agent's only path to the database.

The connection runs as the ``ordering_agent`` login, so every permission check
happens in Postgres (roles, RLS, SECURITY DEFINER functions). This module only
refuses to start under a more powerful role and translates the database's
refusals into a typed exception.

The conversation token scopes the agent's orders to its conversation (see
``migrations/001_conversation_scope.sql``). It is set with
``set_config('app.conversation', token, true)`` as the first statement of every
transaction, so it lives only as long as that transaction and can never be left
on a pooled connection for another conversation. It is never logged and never
put in an exception, the prompt, the graph state or a report.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Self

import psycopg
from psycopg.types.json import Jsonb

# SQLSTATEs the schema uses to refuse a request; everything else is a real error.
REFUSAL_SQLSTATES = frozenset({"42501", "22023", "22P02", "P0002", "23514"})


class UnsafeRole(Exception):
    """The connection is not the plain ordering_agent login."""


class DbRefusal(Exception):
    """The database refused the request (permission, validation or state)."""

    def __init__(self, sqlstate: str, message: str):
        super().__init__(f"{sqlstate}: {message}")
        self.sqlstate = sqlstate
        self.message = message


@dataclass(frozen=True)
class MenuItem:
    id: str
    name: str
    price: Decimal


@dataclass(frozen=True)
class OrderSummary:
    status: str
    total: Decimal


class AgentDB:
    def __init__(self, conn: psycopg.Connection, conversation_token: str):
        self._conn = conn
        self._token = conversation_token

    def __repr__(self) -> str:
        return "AgentDB(<connection>, <conversation token>)"

    @classmethod
    def connect(cls, url: str, conversation_token: str) -> AgentDB:
        conn = psycopg.connect(url)
        try:
            row = conn.execute(
                "select session_user, r.rolsuper, r.rolbypassrls "
                "from pg_roles r where r.rolname = session_user"
            ).fetchone()
            conn.rollback()
            if row is None or row[0] != "ordering_agent" or row[1] or row[2]:
                raise UnsafeRole(
                    "connection must be the ordering_agent login, without superuser or BYPASSRLS"
                )
        except BaseException:
            conn.close()
            raise
        return cls(conn, conversation_token)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple]:
        """One transaction per call, scoped to the conversation; roll back on any error."""
        try:
            self._conn.execute("select set_config('app.conversation', %s, true)", (self._token,))
            rows = self._conn.execute(sql, params).fetchall()
            self._conn.commit()
            return rows
        except psycopg.Error as err:
            self._conn.rollback()
            if err.sqlstate in REFUSAL_SQLSTATES:
                raise DbRefusal(err.sqlstate, err.diag.message_primary or str(err)) from err
            raise
        except BaseException:
            self._conn.rollback()
            raise

    def menu(self) -> list[MenuItem]:
        rows = self._run(
            "select id::text, name, price from app.menu_items where active order by name"
        )
        return [MenuItem(*r) for r in rows]

    def create_order(self, customer_label: str, lines: list[dict]) -> str:
        rows = self._run(
            "select app.create_order('chat', %s, %s)::text", (customer_label, Jsonb(lines))
        )
        return rows[0][0]

    def order_summary(self, order_id: str) -> OrderSummary:
        rows = self._run(
            "select status::text, total from app.chat_order_summary(%s::uuid)", (order_id,)
        )
        return OrderSummary(*rows[0])

    def update_order_status(self, order_id: str, status: str) -> None:
        self._run("select app.advance_order(%s::uuid, %s::app.order_status)", (order_id, status))
