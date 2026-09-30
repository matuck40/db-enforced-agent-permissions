-- Test setup, run once as the bootstrap superuser before the role tests.
-- It only installs assertion helpers and fixtures; every assertion in the
-- numbered files runs as the station it is about, never as a superuser.

\set ON_ERROR_STOP on
begin;

create schema test;
grant usage on schema test to app_user, app_owner;

-- Both helpers are SECURITY INVOKER, so the statement under test runs with the
-- privileges of whoever connected.
create function test.assert(cond boolean, msg text) returns text
language plpgsql as $$
begin
  if cond is not true then
    raise exception 'ASSERTION FAILED: %', msg;
  end if;
  return 'ok       ' || msg;
end $$;

create function test.refused(stmt text, expected_sqlstate text, msg text) returns text
language plpgsql as $$
begin
  begin
    execute stmt;
  exception when others then
    if sqlstate = expected_sqlstate then
      return 'refused  ' || msg || '  [' || sqlstate || ': ' || sqlerrm || ']';
    end if;
    raise exception 'ASSERTION FAILED: % -- expected SQLSTATE %, got % (%)',
      msg, expected_sqlstate, sqlstate, sqlerrm;
  end;
  raise exception 'ASSERTION FAILED: % -- the database allowed: %', msg, stmt;
end $$;

-- Every test file starts here: prove the connection is the station it claims
-- to be, and that it is not a superuser.
create function test.connected_as(expected name) returns text
language sql as $$
  select test.assert(
    session_user = expected
      and not (select rolsuper from pg_catalog.pg_roles where rolname = session_user),
    'connected as ' || expected || ', not a superuser')
$$;

grant execute on all functions in schema test to app_user, app_owner;

-- The schema creates app_owner without LOGIN: in a deployment nobody connects
-- as it. The suite turns login on only so tests/60 can prove that even the
-- owner cannot rewrite the ledgers.
alter role app_owner login;

-- Fixtures. Fixed ids so the role tests can refer to them.
insert into app.staff (db_user, station) values
  ('floor_staff', 'floor_staff'),
  ('kitchen', 'kitchen'),
  ('cashier', 'cashier'),
  ('manager', 'manager'),
  ('ordering_agent', 'ordering_agent');

insert into app.menu_items (id, name, price) values
  ('00000000-0000-0000-0000-00000000000a', 'Menu item A', 10.00),
  ('00000000-0000-0000-0000-00000000000b', 'Menu item B', 4.50);

insert into app.orders (id, channel, customer_label, status, total, created_by) values
  ('00000000-0000-0000-0000-000000000c01', 'chat',    'Customer 1', 'submitted', 10.00, 'ordering_agent'),
  ('00000000-0000-0000-0000-000000000c02', 'counter', 'Customer 2', 'submitted', 14.50, 'cashier'),
  ('00000000-0000-0000-0000-000000000c03', 'phone',   'Customer 3', 'ready',      4.50, 'manager');

insert into app.order_items (order_id, menu_item_id, description, quantity, unit_price) values
  ('00000000-0000-0000-0000-000000000c01', '00000000-0000-0000-0000-00000000000a', 'Menu item A', 1, 10.00),
  ('00000000-0000-0000-0000-000000000c02', '00000000-0000-0000-0000-00000000000a', 'Menu item A', 1, 10.00),
  ('00000000-0000-0000-0000-000000000c02', '00000000-0000-0000-0000-00000000000b', 'Menu item B', 1, 4.50),
  ('00000000-0000-0000-0000-000000000c03', '00000000-0000-0000-0000-00000000000b', 'Menu item B', 1, 4.50);

insert into app.payments (id, order_id, amount, method) values
  ('00000000-0000-0000-0000-000000000d01', '00000000-0000-0000-0000-000000000c02', 14.50, 'cash');

insert into app.stock_movements (id, menu_item_id, quantity_change, reason) values
  ('00000000-0000-0000-0000-000000000e01', '00000000-0000-0000-0000-00000000000a', 20, 'delivery received');

commit;
