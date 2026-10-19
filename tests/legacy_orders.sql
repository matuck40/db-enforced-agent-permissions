-- Data that exists before the migrations run: loaded as the bootstrap
-- superuser right after schema.sql and before migrations/*.sql, so the suite
-- covers what a migration does to rows that are already there.
--
-- One chat draft created by the agent under schema.sql alone. Migration 001
-- leaves its conversation_hash null, so tests/40_ordering_agent.sql can prove
-- the agent can no longer read or confirm it.

\set ON_ERROR_STOP on
begin;

insert into app.orders (id, channel, customer_label, status, total, created_by) values
  ('00000000-0000-0000-0000-000000000c04', 'chat', 'Legacy chat customer', 'draft', 0, 'ordering_agent');

commit;
