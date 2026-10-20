-- role: ordering_agent
-- The LLM chat agent's database login. Its whole grant: create a chat order
-- from catalog items, then read and confirm that order by the id it got back,
-- and only from the conversation that created it.
--
-- The conversation is identified by a token the harness generates per
-- conversation and sets with set_config('app.conversation', token, true) in
-- every transaction; the database stores only its sha256. Here it is set once
-- for the session, which is the same thing inside this one transaction.
--
-- (b) Every statement below that the agent is not allowed to run is refused
-- by the database itself. None of it depends on the prompt telling the model
-- not to try: a jailbroken or prompt-injected agent holding this connection
-- can do exactly what this file says it can, and nothing more.
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned
\set chat_order    '00000000-0000-0000-0000-000000000c01'
\set counter_order '00000000-0000-0000-0000-000000000c02'
\set item_a        '00000000-0000-0000-0000-00000000000a'
\set item_b        '00000000-0000-0000-0000-00000000000b'
\set payment       '00000000-0000-0000-0000-000000000d01'
\set movement      '00000000-0000-0000-0000-000000000e01'
-- A chat draft created before migrations/001 (tests/legacy_orders.sql): no hash.
\set legacy_order  '00000000-0000-0000-0000-000000000c04'
\set token_one     'conversation-one-0123456789abcdefghijklmnop'
\set token_two     'conversation-two-0123456789abcdefghijklmnop'

begin;
select test.connected_as('ordering_agent');
select set_config('app.conversation', :'token_one', false) \gset

select test.assert((select count(*) = 2 from app.menu_items), 'can read the menu');

-- What it may do: create a chat order. A price sent by the model is ignored.
select app.create_order('chat', 'Chat customer', jsonb_build_array(
  jsonb_build_object('menu_item_id', :'item_a', 'quantity', 2, 'unit_price', 0.01),
  jsonb_build_object('menu_item_id', :'item_b'))) as draft \gset
select test.assert(
  (select status = 'draft' and total = 24.50 from app.chat_order_summary(:'draft')),
  'creates a chat order as a draft, priced from the catalog (0.01 ignored)');

select app.advance_order(:'draft', 'submitted') \gset
select test.assert((select status = 'submitted' from app.chat_order_summary(:'draft')),
  'can confirm the order it created (draft -> submitted)');

-- (b) Another conversation's chat order does not exist for this one. The
-- fixture c01 is a chat order of no conversation (its hash is null), the
-- legacy order predates the conversation scope, and draft_two belongs to this
-- session's other token.
select test.refused(format($$select * from app.chat_order_summary(%L)$$, :'chat_order'),
  'P0002', '(b) cannot read another conversation''s chat order by id');
select test.refused(format($$select app.advance_order(%L, 'submitted')$$, :'chat_order'),
  'P0002', '(b) cannot confirm another conversation''s order by id');
select test.refused(format($$select * from app.chat_order_summary(%L)$$, :'legacy_order'),
  'P0002', '(b) cannot read a chat draft created before the conversation scope');
select test.refused(format($$select app.advance_order(%L, 'submitted')$$, :'legacy_order'),
  'P0002', '(b) cannot confirm a chat draft created before the conversation scope');

select app.create_order('chat', 'Second chat customer', jsonb_build_array(
  jsonb_build_object('menu_item_id', :'item_b'))) as draft_two \gset
select set_config('app.conversation', :'token_two', false) \gset
select test.refused(format($$select * from app.chat_order_summary(%L)$$, :'draft_two'),
  'P0002', '(b) a second conversation cannot read the first one''s draft');
select test.refused(format($$select app.advance_order(%L, 'submitted')$$, :'draft_two'),
  'P0002', '(b) a second conversation cannot confirm the first one''s draft');
select set_config('app.conversation', :'token_one', false) \gset
select test.assert((select status = 'draft' from app.chat_order_summary(:'draft_two')),
  'the first conversation still sees its draft, unconfirmed');

select set_config('app.conversation', '', false) \gset
select test.refused(format($$select app.create_order('chat', 'x',
  jsonb_build_array(jsonb_build_object('menu_item_id', %L)))$$, :'item_a'),
  '42501', '(b) cannot create an order without a conversation token');
select set_config('app.conversation', 'only-thirty-one-characters-long', false) \gset
select test.refused(format($$select app.create_order('chat', 'x',
  jsonb_build_array(jsonb_build_object('menu_item_id', %L)))$$, :'item_a'),
  '42501', '(b) cannot create an order with a token shorter than 32 characters');
select set_config('app.conversation', :'token_one', false) \gset
select test.refused('select conversation_hash from app.orders', '42501',
  '(b) cannot read the conversation hashes');
select test.refused('select app.current_conversation_hash()', '42501',
  '(b) cannot call the hashing helper the functions use');

-- (b) Operations outside the grant. The database refuses each one.
select test.refused('select * from app.orders', '42501',
  '(b) cannot list orders: no browsing other conversations');
select test.refused('select * from app.order_items', '42501', '(b) cannot list order items');
select test.refused(format($$select * from app.chat_order_summary(%L)$$, :'counter_order'),
  'P0002', '(b) a counter order does not exist for the agent, even by id');
select test.refused(format($$select app.create_order('chat', 'x',
  jsonb_build_array(jsonb_build_object('menu_item_id', %L, 'quantity', 0.001)))$$, :'item_b'),
  '22023', '(b) cannot order a fraction of an item (a free order)');
select test.refused(format($$select app.create_order('chat', 'x',
  jsonb_build_array(jsonb_build_object('menu_item_id', %L, 'quantity', 'NaN')))$$, :'item_a'),
  '22023', '(b) cannot order NaN of an item');
select test.refused(format($$select app.create_order('chat', 'x',
  jsonb_build_array(jsonb_build_object('menu_item_id', %L, 'quantity', 500)))$$, :'item_a'),
  '22023', '(b) cannot order an absurd quantity');
select test.refused($$select app.create_order('chat', 'x',
  '[{"description": "anything", "unit_price": 0.01}]')$$,
  '42501', '(b) cannot order an off-menu item with an invented price');
select test.refused(format($$select app.create_order('counter', 'x',
  jsonb_build_array(jsonb_build_object('menu_item_id', %L)))$$, :'item_a'),
  '42501', '(b) cannot record an order on another channel');
select test.refused(format($$select app.advance_order(%L, 'preparing')$$, :'draft'),
  '42501', '(b) cannot start preparation');
select test.refused(format($$select app.advance_order(%L, 'cancelled')$$, :'draft'),
  '42501', '(b) cannot cancel an order; a human does that');
select test.refused(format($$select app.advance_order(%L, 'cancelled')$$, :'counter_order'),
  'P0002', '(b) cannot even address an order from another channel');
select test.refused(format($$select app.record_payment(%L, 10, 'cash')$$, :'chat_order'),
  '42501', '(b) cannot record a payment');
select test.refused('select * from app.payments', '42501', '(b) cannot read payments');
select test.refused(format($$select app.reverse_payment(%L, 'x')$$, :'payment'),
  '42501', '(b) cannot reverse a payment');
select test.refused('select * from app.stock_movements', '42501', '(b) cannot read stock');
select test.refused(format($$select app.record_stock_movement(%L, -1, 'x')$$, :'item_a'),
  '42501', '(b) cannot record a stock movement');
select test.refused(format($$select app.reverse_stock_movement(%L, 'x')$$, :'movement'),
  '42501', '(b) cannot reverse a stock movement');
select test.refused('select * from app.staff', '42501', '(b) cannot read the staff table');
select test.refused(format($$update app.orders set total = 0 where id = %L$$, :'chat_order'),
  '42501', '(b) cannot rewrite an order total with raw SQL');
select test.refused(format($$insert into app.order_items (order_id, description, quantity, unit_price)
  values (%L, 'free item', 1, 0)$$, :'chat_order'),
  '42501', '(b) cannot slip an item into an order with raw SQL');
select test.refused(format($$delete from app.orders where id = %L$$, :'chat_order'),
  '42501', '(b) cannot delete an order');
select test.refused('set role manager', '42501', '(b) cannot switch to a more powerful role');
select test.refused('set role app_owner', '42501',
  '(b) cannot become the schema owner, which bypasses row-level security');
select test.refused('create table app.backdoor (x int)', '42501', '(b) cannot create objects');

rollback;
\echo 'OK: ordering_agent'
