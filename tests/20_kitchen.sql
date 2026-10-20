-- role: kitchen
-- Moves orders through preparation and keeps the stock ledger.
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned
\set chat_order    '00000000-0000-0000-0000-000000000c01'
\set counter_order '00000000-0000-0000-0000-000000000c02'
\set item_a        '00000000-0000-0000-0000-00000000000a'
\set movement      '00000000-0000-0000-0000-000000000e01'

begin;
select test.connected_as('kitchen');

-- Four: the three fixtures of tests/setup.sql and tests/legacy_orders.sql.
select test.assert((select count(*) from app.orders) = 4,
  'sees every order, from every channel');

-- The preparation steps belong to this station.
select app.advance_order(:'counter_order', 'preparing') \gset
select app.advance_order(:'counter_order', 'ready') \gset
select test.assert((select status = 'ready' from app.orders where id = :'counter_order'),
  'can move an order submitted -> preparing -> ready');

select test.refused(format($$select app.advance_order(%L, 'completed')$$, :'counter_order'),
  '42501', 'cannot hand out an order (floor step)');
select test.refused(format($$select app.advance_order(%L, 'cancelled')$$, :'chat_order'),
  '42501', 'cannot cancel an order');
select test.refused(format($$select app.advance_order(%L, 'draft')$$, :'chat_order'),
  '23514', 'cannot skip backwards in the state machine');

-- Stock: append-only, corrected by a reversing entry.
select test.assert(
  (select quantity_change = 20 from app.stock_movements where id = :'movement'),
  'can read stock movements');
select app.record_stock_movement(:'item_a', -3, 'used for service') as used \gset
select app.reverse_stock_movement(:'used', 'miscounted') as reversal \gset
select test.assert(
  (select quantity_change = 3 and reverses_movement_id = :'used'
   from app.stock_movements where id = :'reversal'),
  'a mistake is corrected with a reversing movement, not an edit');
select test.assert(
  (select sum(quantity_change) = 20 from app.stock_movements where menu_item_id = :'item_a'),
  'the balance is the sum of the ledger');
select test.refused(format($$select app.reverse_stock_movement(%L, 'again')$$, :'used'),
  '23505', 'the same movement cannot be reversed twice');
select test.refused(format($$select app.record_stock_movement(%L, 'NaN', 'x')$$, :'item_a'),
  '23514', 'NaN cannot enter the stock ledger');
select test.refused(format($$select app.reverse_stock_movement(%L, ' ')$$, :'movement'),
  '23514', 'a stock reversal needs a reason');
select test.refused(format($$update app.stock_movements set quantity_change = 1 where id = %L$$, :'movement'),
  '42501', 'cannot update a stock movement');
select test.refused(format($$delete from app.stock_movements where id = %L$$, :'movement'),
  '42501', 'cannot delete a stock movement');

select test.refused('select * from app.payments', '42501', 'cannot read payments');
select test.refused(format($$select app.create_order('counter', 'x',
  jsonb_build_array(jsonb_build_object('menu_item_id', %L)))$$, :'item_a'),
  '42501', 'cannot create orders');

rollback;
\echo 'OK: kitchen'
