-- Database-enforced permissions for a multi-station ordering system with an
-- LLM ordering agent. Every rule about who may read or write what lives here,
-- so the UI and the agent's prompt are never the only line of defense.
--
--   * One Postgres login per physical station, plus one for the chat agent.
--   * Row-level security on every table, forced.
--   * No direct write grant on any table: writes go through SECURITY DEFINER
--     functions that check the caller's station themselves.
--   * The agent may create a chat order from catalog items, then read and
--     confirm that order by the id it got back. Nothing else.
--   * Payments and stock movements are append-only; corrections are reversals.

begin;

-- 1. Roles ------------------------------------------------------------------
-- app_owner owns every object and is the identity the SECURITY DEFINER
-- functions run as. It is not a superuser and cannot log in; BYPASSRLS lets
-- the functions do their own station checks instead of the caller's policies.
create role app_owner nologin bypassrls;

-- Common group for everyone who uses the application.
create role app_user nologin;
create role floor_staff    login in role app_user;
create role kitchen        login in role app_user;
create role cashier        login in role app_user;
create role manager        login in role app_user;
create role ordering_agent login in role app_user;

create schema app authorization app_owner;
revoke all on schema public from public;
grant usage on schema app to app_user;

set role app_owner;

-- 2. Types and tables ---------------------------------------------------------
create type app.station as enum ('floor_staff', 'kitchen', 'cashier', 'manager', 'ordering_agent');
create type app.channel as enum ('chat', 'counter', 'phone');
create type app.order_status as enum
  ('draft', 'submitted', 'preparing', 'ready', 'completed', 'cancelled');

-- Maps a database login to the station it operates. Deactivating a row
-- revokes that login's access everywhere at once.
create table app.staff (
  db_user name primary key,
  station app.station not null,
  active  boolean not null default true
);

create table app.menu_items (
  id     uuid primary key default gen_random_uuid(),
  name   text not null,
  price  numeric(12,2) not null check (price >= 0 and price <> 'NaN'),
  active boolean not null default true
);

create table app.orders (
  id             uuid primary key default gen_random_uuid(),
  channel        app.channel not null,
  customer_label text not null,
  status         app.order_status not null,
  total          numeric(12,2) not null default 0 check (total <> 'NaN'),
  created_by     name not null default session_user,
  created_at     timestamptz not null default now()
);

create table app.order_items (
  id           uuid primary key default gen_random_uuid(),
  order_id     uuid not null references app.orders(id),
  menu_item_id uuid references app.menu_items(id),
  description  text not null,
  quantity     numeric(12,3) not null check (quantity > 0 and quantity <> 'NaN'),
  unit_price   numeric(12,2) not null check (unit_price >= 0 and unit_price <> 'NaN')
);

-- Append-only. A reversal is a new row with the opposite amount that points at
-- the row it cancels; the unique constraint makes a double reversal impossible.
-- numeric accepts 'NaN', which passes "<> 0" and "> 0"; one NaN row would
-- poison every sum forever, so every numeric column rejects it explicitly.
create table app.payments (
  id                  uuid primary key default gen_random_uuid(),
  order_id            uuid not null references app.orders(id),
  amount              numeric(12,2) not null check (amount <> 0 and amount <> 'NaN'),
  method              text not null check (method in ('cash', 'card', 'transfer')),
  reverses_payment_id uuid unique references app.payments(id),
  reason              text,
  recorded_by         name not null default session_user,
  recorded_at         timestamptz not null default now(),
  check ((amount > 0) = (reverses_payment_id is null))
);

create table app.stock_movements (
  id                   uuid primary key default gen_random_uuid(),
  menu_item_id         uuid not null references app.menu_items(id),
  quantity_change      numeric(12,3) not null check (quantity_change <> 0 and quantity_change <> 'NaN'),
  reason               text not null check (btrim(reason) <> ''),
  reverses_movement_id uuid unique references app.stock_movements(id),
  recorded_by          name not null default session_user,
  recorded_at          timestamptz not null default now()
);

-- 3. Immutability ---------------------------------------------------------------
-- Fires for everyone, including the table owner. Only a DDL change could get
-- around it, and the application never holds the owner's credentials.
create function app.reject_mutation() returns trigger
language plpgsql set search_path = '' as $$
begin
  raise exception '% is append-only; record a reversing entry instead', tg_table_name
    using errcode = '55000';
end $$;

create trigger payments_append_only before update or delete on app.payments
  for each row execute function app.reject_mutation();
create trigger payments_no_truncate before truncate on app.payments
  for each statement execute function app.reject_mutation();
create trigger stock_append_only before update or delete on app.stock_movements
  for each row execute function app.reject_mutation();
create trigger stock_no_truncate before truncate on app.stock_movements
  for each statement execute function app.reject_mutation();

-- 4. Who is calling ---------------------------------------------------------------
-- session_user, not current_user: inside a SECURITY DEFINER function
-- current_user is the owner, but session_user is still the station that
-- connected. This assumes one real login per station; behind a pooler that
-- shares one login, every check fails closed. SECURITY DEFINER because staff
-- is manager-only under its own policy.
create function app.current_station() returns app.station
language sql stable security definer set search_path = '' as $$
  select station from app.staff where db_user = session_user and active
$$;

create function app.has_station(allowed app.station[]) returns boolean
language sql stable set search_path = '' as $$
  select coalesce(app.current_station() = any(allowed), false)
$$;

-- 5. Row-level security ---------------------------------------------------------------
alter table app.staff           enable row level security, force row level security;
alter table app.menu_items      enable row level security, force row level security;
alter table app.orders          enable row level security, force row level security;
alter table app.order_items     enable row level security, force row level security;
alter table app.payments        enable row level security, force row level security;
alter table app.stock_movements enable row level security, force row level security;

create policy staff_read on app.staff for select using (app.has_station('{manager}'));

create policy menu_read on app.menu_items for select using (app.current_station() is not null);

-- Every human station sees every order, whatever channel it came from. A
-- shipped bug once put the floor station behind a chat-only channel filter:
-- a counter order created by that very station vanished from its screen with
-- no error. Testing as an owner would never have caught it;
-- tests/10_floor_staff.sql guards it. The agent has no read grant here at all
-- (see chat_order_summary).
create policy orders_read on app.orders for select
  using (app.has_station('{floor_staff,kitchen,cashier,manager}'));

-- An item is visible exactly when its order is: the subquery runs under the
-- caller's own orders policy.
create policy order_items_read on app.order_items for select
  using (exists (select 1 from app.orders o where o.id = order_items.order_id));

create policy payments_read on app.payments for select using (app.has_station('{cashier,manager}'));

create policy stock_read on app.stock_movements for select
  using (app.has_station('{kitchen,manager}'));

-- Table grants are the first gate, policies the second. Nobody gets a direct
-- write grant.
grant select on app.menu_items to app_user;
grant select on app.orders, app.order_items to floor_staff, kitchen, cashier, manager;
grant select on app.payments to cashier, manager;
grant select on app.stock_movements to kitchen, manager;
grant select on app.staff to manager;

-- 6. Writes, one function per operation ------------------------------------------------
create function app.create_order(p_channel app.channel, p_customer text, p_lines jsonb)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  st app.station := app.current_station();
  v_order uuid; line jsonb; item app.menu_items%rowtype; v_desc text;
  v_price numeric(12,2); v_qty numeric(12,3); v_total numeric(12,2) := 0;
begin
  if st is null or st not in ('floor_staff', 'cashier', 'manager', 'ordering_agent') then
    raise exception 'station % may not create orders', st using errcode = '42501';
  end if;
  if st = 'ordering_agent' and p_channel <> 'chat' then
    raise exception 'the agent only records chat orders' using errcode = '42501';
  end if;
  if jsonb_typeof(p_lines) is distinct from 'array' or jsonb_array_length(p_lines) = 0 then
    raise exception 'an order needs at least one line' using errcode = '22023';
  end if;

  -- The agent's orders wait as drafts until the customer confirms.
  insert into app.orders (channel, customer_label, status)
  values (p_channel, p_customer,
          case when st = 'ordering_agent' then 'draft' else 'submitted' end::app.order_status)
  returning id into v_order;

  for line in select value from jsonb_array_elements(p_lines) loop
    v_qty := coalesce((line ->> 'quantity')::numeric, 1);
    if not v_qty > 0 or (st = 'ordering_agent' and (v_qty <> trunc(v_qty) or v_qty > 20)) then
      raise exception 'invalid quantity' using errcode = '22023';
    end if;
    if line ? 'menu_item_id' then
      select * into item from app.menu_items
      where id = (line ->> 'menu_item_id')::uuid and active;
      if not found then raise exception 'menu item unavailable' using errcode = '22023'; end if;
      -- The price always comes from the catalog; a price sent by the caller
      -- is ignored, so a manipulated prompt cannot discount an order.
      v_desc := item.name; v_price := item.price;
    elsif st = 'ordering_agent' then
      raise exception 'the agent may only order catalog items' using errcode = '42501';
    else
      v_desc := nullif(btrim(line ->> 'description'), '');
      v_price := (line ->> 'unit_price')::numeric;
      if v_desc is null or v_price is null or v_price < 0 then
        raise exception 'an open item needs a description and a price' using errcode = '22023';
      end if;
    end if;
    insert into app.order_items (order_id, menu_item_id, description, quantity, unit_price)
    values (v_order, (line ->> 'menu_item_id')::uuid, v_desc, v_qty, v_price);
    v_total := v_total + round(v_qty * v_price, 2);
  end loop;

  update app.orders set total = v_total where id = v_order;
  return v_order;
end $$;

-- The whole state machine, with the stations allowed to take each step.
create function app.advance_order(p_order uuid, p_to app.order_status)
returns void language plpgsql security definer set search_path = '' as $$
declare
  st app.station := app.current_station();
  o app.orders%rowtype;
  allowed app.station[];
begin
  if st is null then raise exception 'unknown station' using errcode = '42501'; end if;
  select * into o from app.orders where id = p_order for update;
  -- An order the caller cannot read does not exist for the caller.
  if not found or (st = 'ordering_agent' and o.channel <> 'chat') then
    raise exception 'order not found' using errcode = 'P0002';
  end if;

  allowed := case
    when o.status = 'draft'          and p_to = 'submitted'      then '{ordering_agent,floor_staff,manager}'
    when o.status = 'submitted'      and p_to = 'preparing' then '{kitchen,manager}'
    when o.status = 'preparing' and p_to = 'ready'          then '{kitchen,manager}'
    when o.status = 'ready'          and p_to = 'completed'      then '{floor_staff,cashier,manager}'
    when o.status not in ('completed', 'cancelled')
                                     and p_to = 'cancelled'      then '{cashier,manager}'
  end::app.station[];

  -- Invalid moves are reported before permission, so callers can learn the
  -- status of an order they can already read, and nothing more.
  if allowed is null then
    raise exception 'invalid transition % -> %', o.status, p_to using errcode = '23514';
  end if;
  if not st = any(allowed) then
    raise exception 'station % may not move an order from % to %', st, o.status, p_to
      using errcode = '42501';
  end if;
  update app.orders set status = p_to where id = p_order;
end $$;

-- The agent's only read path into orders. The id returned by create_order is
-- the capability: the agent cannot list chat orders, so it cannot read or
-- confirm another conversation's order.
create function app.chat_order_summary(p_order uuid)
returns table (status app.order_status, total numeric) language plpgsql
security definer set search_path = '' as $$
begin
  if not app.has_station('{ordering_agent}') then
    raise exception 'for the ordering agent only' using errcode = '42501';
  end if;
  return query select o.status, o.total from app.orders o
    where o.id = p_order and o.channel = 'chat';
  if not found then raise exception 'order not found' using errcode = 'P0002'; end if;
end $$;

create function app.record_payment(p_order uuid, p_amount numeric, p_method text)
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_id uuid;
begin
  if not app.has_station('{cashier,manager}') then
    raise exception 'only the cashier records payments' using errcode = '42501';
  end if;
  if not exists (select 1 from app.orders
                 where id = p_order and status not in ('draft', 'cancelled')) then
    raise exception 'order is not payable' using errcode = '23514';
  end if;
  insert into app.payments (order_id, amount, method)
  values (p_order, round(p_amount, 2), p_method) returning id into v_id;
  return v_id;
end $$;

create function app.reverse_payment(p_payment uuid, p_reason text)
returns uuid language plpgsql security definer set search_path = '' as $$
declare p app.payments%rowtype; v_id uuid;
begin
  if not app.has_station('{cashier,manager}') then
    raise exception 'only the cashier reverses payments' using errcode = '42501';
  end if;
  if nullif(btrim(p_reason), '') is null then
    raise exception 'a reversal needs a reason' using errcode = '22023';
  end if;
  select * into p from app.payments where id = p_payment and reverses_payment_id is null;
  if not found then raise exception 'payment not found' using errcode = 'P0002'; end if;
  insert into app.payments (order_id, amount, method, reverses_payment_id, reason)
  values (p.order_id, -p.amount, p.method, p.id, p_reason) returning id into v_id;
  return v_id;
end $$;

create function app.record_stock_movement(p_item uuid, p_delta numeric, p_reason text)
returns uuid language plpgsql security definer set search_path = '' as $$
declare v_id uuid;
begin
  if not app.has_station('{kitchen,manager}') then
    raise exception 'only the kitchen records stock' using errcode = '42501';
  end if;
  insert into app.stock_movements (menu_item_id, quantity_change, reason)
  values (p_item, p_delta, p_reason) returning id into v_id;
  return v_id;
end $$;

create function app.reverse_stock_movement(p_movement uuid, p_reason text)
returns uuid language plpgsql security definer set search_path = '' as $$
declare m app.stock_movements%rowtype; v_id uuid;
begin
  if not app.has_station('{kitchen,manager}') then
    raise exception 'only the kitchen reverses stock' using errcode = '42501';
  end if;
  select * into m from app.stock_movements where id = p_movement and reverses_movement_id is null;
  if not found then raise exception 'movement not found' using errcode = 'P0002'; end if;
  insert into app.stock_movements (menu_item_id, quantity_change, reason, reverses_movement_id)
  values (m.menu_item_id, -m.quantity_change, p_reason, m.id) returning id into v_id;
  return v_id;
end $$;

-- 7. Function grants: a function nobody is granted does not exist for them -------------
revoke all on all functions in schema app from public;
grant execute on function app.current_station(), app.has_station(app.station[]) to app_user;
grant execute on function app.create_order(app.channel, text, jsonb)
  to floor_staff, cashier, manager, ordering_agent;
grant execute on function app.advance_order(uuid, app.order_status)
  to floor_staff, kitchen, cashier, manager, ordering_agent;
grant execute on function app.chat_order_summary(uuid) to ordering_agent;
grant execute on function app.record_payment(uuid, numeric, text),
                          app.reverse_payment(uuid, text) to cashier, manager;
grant execute on function app.record_stock_movement(uuid, numeric, text),
                          app.reverse_stock_movement(uuid, text) to kitchen, manager;

reset role;
commit;
