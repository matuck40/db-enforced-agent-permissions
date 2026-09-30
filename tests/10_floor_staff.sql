-- role: floor_staff
-- The front-of-house station: takes counter and phone orders, hands them out.
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned
\set chat_order    '00000000-0000-0000-0000-000000000c01'
\set counter_order '00000000-0000-0000-0000-000000000c02'
\set phone_order   '00000000-0000-0000-0000-000000000c03'
\set item_a        '00000000-0000-0000-0000-00000000000a'

begin;
select test.connected_as('floor_staff');

-- (a) Regression test for a bug that shipped. The read policy on orders once
-- put this station behind a channel filter meant for the chat channel only,
-- so every order that did not come from chat silently disappeared from the
-- floor's screen. No error, just an empty queue. It was caught only by
-- testing as the station itself; an owner account passes every check.
select test.assert(
  exists (select 1 from app.orders where id = :'counter_order'),
  '(a) a counter order is visible to the floor station');
select test.assert(
  exists (select 1 from app.orders where id = :'phone_order'),
  '(a) a phone order is visible to the floor station');
select test.assert(
  (select count(*) from app.order_items where order_id = :'counter_order') = 2,
  '(a) the items of a counter order are visible to the floor station');

-- The exact shape of the original failure: an order this station creates
-- itself, on a non-chat channel, must show up on its own screen.
select app.create_order('counter', 'Walk-in',
  jsonb_build_array(jsonb_build_object('menu_item_id', :'item_a', 'quantity', 2))) as new_order \gset
select test.assert(
  (select status = 'submitted' and total = 20.00 from app.orders where id = :'new_order'),
  '(a) a counter order the floor station just created is on its own screen');

select test.assert(
  exists (select 1 from app.orders where id = :'chat_order'),
  'chat orders are visible too: the floor hands them out');

-- What it may not read.
select test.refused('select * from app.payments', '42501', 'cannot read payments');
select test.refused('select * from app.stock_movements', '42501', 'cannot read stock movements');
select test.refused('select * from app.staff', '42501', 'cannot read the staff table');

-- What it may not write.
select test.refused($$insert into app.orders (channel, customer_label, status)
  values ('counter', 'x', 'completed')$$, '42501', 'cannot insert an order directly');
select test.refused(format($$update app.orders set total = 0 where id = %L$$, :'counter_order'),
  '42501', 'cannot update an order directly');
select test.refused(format($$select app.advance_order(%L, 'preparing')$$, :'counter_order'),
  '42501', 'cannot start preparation (kitchen step)');
select test.refused(format($$select app.advance_order(%L, 'cancelled')$$, :'counter_order'),
  '42501', 'cannot cancel an order');
select test.refused(format($$select app.record_payment(%L, 14.50, 'cash')$$, :'counter_order'),
  '42501', 'cannot record a payment');

-- What it may do.
select app.advance_order(:'phone_order', 'completed') \gset
select test.assert(
  (select status = 'completed' from app.orders where id = :'phone_order'),
  'can hand out a ready order (ready -> completed)');

rollback;
\echo 'OK: floor_staff'
