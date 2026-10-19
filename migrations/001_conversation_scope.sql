-- Migration 001: scope the ordering agent's orders to the conversation that
-- created them. Load as the bootstrap superuser, after schema.sql. The
-- current schema is schema.sql plus every file in migrations/ in name order.
--
-- The gap it closes: under schema.sql alone, chat_order_summary and
-- advance_order accept any chat order id, so an id that leaks into another
-- conversation (pasted from a receipt, injected through data) lets the agent
-- read that order or confirm that draft.
--
-- After it, the harness generates a token per conversation and sets it in
-- every transaction with set_config('app.conversation', token, true). The
-- database stores only its sha256, in app.orders.conversation_hash, and for the
-- ordering_agent:
--   * create_order requires a token of at least 32 characters (else 42501)
--     and records its hash on the order;
--   * chat_order_summary and advance_order see a chat order only when its
--     hash equals the hash of the current token (else P0002, exactly like an
--     order that does not exist). A null hash never matches.
-- Other stations and every function signature are unchanged.
--
-- It expects no pre-existing `extensions` schema: if one exists, the migration
-- fails instead of reusing a schema it does not own.
--
-- Existing chat orders keep a null conversation_hash and are therefore
-- unreachable by the agent from now on, on purpose: nothing records which
-- conversation created them, and guessing would reopen the gap. A human
-- station still sees and handles them as before.

begin;

-- pgcrypto in a schema of its own, called schema-qualified: every function
-- in app runs with search_path = ''. Only the schema owner needs it, inside
-- the SECURITY DEFINER functions; app_user gets no usage on this schema.
create schema extensions;
create extension pgcrypto with schema extensions;
revoke all on schema extensions from public;
grant usage on schema extensions to app_owner;

set role app_owner;

-- Null for every order the agent did not create. The agent has no select on
-- app.orders, so it cannot read this column either.
alter table app.orders add column conversation_hash bytea;

-- The sha256 of the caller's conversation token, or null when the setting is
-- missing or shorter than 32 characters. Not granted to anyone: only the
-- functions below, which run as its owner, call it.
create function app.current_conversation_hash() returns bytea
language sql stable set search_path = '' as $$
  select case when length(t) >= 32
              then extensions.digest(pg_catalog.convert_to(t, 'UTF8'), 'sha256') end
  from (select pg_catalog.current_setting('app.conversation', true) as t) s
$$;
revoke all on function app.current_conversation_hash() from public;

create or replace function app.create_order(p_channel app.channel, p_customer text, p_lines jsonb)
returns uuid language plpgsql security definer set search_path = '' as $$
declare
  st app.station := app.current_station();
  v_conversation bytea;
  v_order uuid; line jsonb; item app.menu_items%rowtype; v_desc text;
  v_price numeric(12,2); v_qty numeric(12,3); v_total numeric(12,2) := 0;
begin
  if st is null or st not in ('floor_staff', 'cashier', 'manager', 'ordering_agent') then
    raise exception 'station % may not create orders', st using errcode = '42501';
  end if;
  if st = 'ordering_agent' and p_channel <> 'chat' then
    raise exception 'the agent only records chat orders' using errcode = '42501';
  end if;
  -- An agent order belongs to the conversation that created it, and to no
  -- other. Without a token there is no conversation to give it to.
  if st = 'ordering_agent' then
    v_conversation := app.current_conversation_hash();
    if v_conversation is null then
      raise exception 'the agent needs a conversation token' using errcode = '42501';
    end if;
  end if;
  if jsonb_typeof(p_lines) is distinct from 'array' or jsonb_array_length(p_lines) = 0 then
    raise exception 'an order needs at least one line' using errcode = '22023';
  end if;

  -- The agent's orders wait as drafts until the customer confirms.
  insert into app.orders (channel, customer_label, status, conversation_hash)
  values (p_channel, p_customer,
          case when st = 'ordering_agent' then 'draft' else 'submitted' end::app.order_status,
          v_conversation)
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
create or replace function app.advance_order(p_order uuid, p_to app.order_status)
returns void language plpgsql security definer set search_path = '' as $$
declare
  st app.station := app.current_station();
  o app.orders%rowtype;
  allowed app.station[];
begin
  if st is null then raise exception 'unknown station' using errcode = '42501'; end if;
  -- An order the caller cannot read does not exist for the caller. For the
  -- agent that is every order but the chat orders of this conversation; a
  -- null hash on either side never matches. The scope is in the WHERE, so the
  -- agent never locks (or waits on) a row it cannot address.
  if st = 'ordering_agent' then
    select * into o from app.orders
    where id = p_order and channel = 'chat'
      and conversation_hash = app.current_conversation_hash()
    for update;
  else
    select * into o from app.orders where id = p_order for update;
  end if;
  if not found then
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

-- The agent's only read path into orders: status and total of a chat order of
-- this conversation. Another conversation's order, an order with no hash and
-- an order that does not exist are the same P0002.
create or replace function app.chat_order_summary(p_order uuid)
returns table (status app.order_status, total numeric) language plpgsql
security definer set search_path = '' as $$
begin
  if not app.has_station('{ordering_agent}') then
    raise exception 'for the ordering agent only' using errcode = '42501';
  end if;
  return query select o.status, o.total from app.orders o
    where o.id = p_order and o.channel = 'chat'
      and o.conversation_hash = app.current_conversation_hash();
  if not found then raise exception 'order not found' using errcode = 'P0002'; end if;
end $$;

reset role;
commit;
