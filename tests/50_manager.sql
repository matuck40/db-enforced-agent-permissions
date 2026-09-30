-- role: manager
-- The most powerful operational station. Still no direct writes, and still no
-- way to rewrite the money or stock ledgers.
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned
\set counter_order '00000000-0000-0000-0000-000000000c02'
\set payment       '00000000-0000-0000-0000-000000000d01'
\set movement      '00000000-0000-0000-0000-000000000e01'

begin;
select test.connected_as('manager');

select test.assert((select count(*) = 3 from app.orders), 'sees every order');
select test.assert((select count(*) = 1 from app.payments), 'reads payments');
select test.assert((select count(*) = 1 from app.stock_movements), 'reads stock');
select test.assert((select count(*) = 5 from app.staff), 'reads the staff table');

select app.create_order('phone', 'Phone customer',
  '[{"description": "Special request", "unit_price": 7.25}]') as open_order \gset
select test.assert((select total = 7.25 from app.orders where id = :'open_order'),
  'a human station may add an open item with its own price');

select test.refused(format($$update app.payments set amount = 0 where id = %L$$, :'payment'),
  '42501', 'cannot update a payment, even as manager');
select test.refused(format($$delete from app.stock_movements where id = %L$$, :'movement'),
  '42501', 'cannot delete a stock movement, even as manager');
select test.refused(format($$update app.orders set status = 'completed' where id = %L$$, :'counter_order'),
  '42501', 'cannot bypass the state machine with raw SQL');
select test.refused(format($$select app.advance_order(%L, 'completed')$$, :'counter_order'),
  '23514', 'cannot skip a step of the state machine');
select test.refused('update app.staff set active = false', '42501',
  'staff changes are not an application operation');

rollback;
\echo 'OK: manager'
