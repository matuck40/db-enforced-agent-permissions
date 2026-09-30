-- role: cashier
-- Takes payments and is the only operational station that can cancel.
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned
\set chat_order    '00000000-0000-0000-0000-000000000c01'
\set counter_order '00000000-0000-0000-0000-000000000c02'
\set payment       '00000000-0000-0000-0000-000000000d01'

begin;
select test.connected_as('cashier');

select test.assert((select amount = 14.50 from app.payments where id = :'payment'),
  'can read payments');

select app.record_payment(:'chat_order', 10.00, 'card') as paid \gset
select test.assert(exists (select 1 from app.payments where id = :'paid'),
  'can record a payment');

-- A payment taken by mistake is never edited or deleted: it gets a reversal.
select test.refused(format($$select app.record_payment(%L, 'NaN', 'cash')$$, :'chat_order'),
  '23514', 'NaN cannot enter the payment ledger');
select test.refused(format($$update app.payments set amount = 1 where id = %L$$, :'payment'),
  '42501', 'cannot update a payment');
select test.refused(format($$delete from app.payments where id = %L$$, :'payment'),
  '42501', 'cannot delete a payment');
select app.reverse_payment(:'payment', 'charged twice') as reversal \gset
select test.assert(
  (select amount = -14.50 and reverses_payment_id = :'payment' and reason = 'charged twice'
   from app.payments where id = :'reversal'),
  'a wrong payment is corrected with a reversing entry');
select test.assert(
  (select sum(amount) = 0 from app.payments where order_id = :'counter_order'),
  'the original and its reversal net to zero, and both stay on record');
select test.refused(format($$select app.reverse_payment(%L, 'again')$$, :'payment'),
  '23505', 'the same payment cannot be reversed twice');
select test.refused(format($$select app.reverse_payment(%L, '')$$, :'paid'),
  '22023', 'a reversal needs a reason');

select app.advance_order(:'counter_order', 'cancelled') \gset
select test.assert((select status = 'cancelled' from app.orders where id = :'counter_order'),
  'can cancel an order');
select test.refused(format($$select app.record_payment(%L, 1, 'cash')$$, :'counter_order'),
  '23514', 'cannot take a payment on a cancelled order');
select test.refused(format($$select app.advance_order(%L, 'preparing')$$, :'chat_order'),
  '42501', 'cannot start preparation');

select test.refused('select * from app.stock_movements', '42501', 'cannot read stock movements');

rollback;
\echo 'OK: cashier'
