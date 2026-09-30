-- role: app_owner
-- The identity that owns the tables and runs the SECURITY DEFINER functions.
-- It is not a superuser, and it bypasses row-level security by design, so this
-- file shows the last gate: the append-only triggers hold even for the owner.
-- The owner could still drop a trigger with DDL; that is why the schema
-- creates it without LOGIN and only this suite enables it.
\set ON_ERROR_STOP on
\set QUIET on
\pset tuples_only on
\pset format unaligned
\set payment  '00000000-0000-0000-0000-000000000d01'
\set movement '00000000-0000-0000-0000-000000000e01'

begin;
select test.connected_as('app_owner');

select test.refused(format($$update app.payments set amount = 0 where id = %L$$, :'payment'),
  '55000', 'the owner cannot update a payment');
select test.refused(format($$delete from app.payments where id = %L$$, :'payment'),
  '55000', 'the owner cannot delete a payment');
select test.refused('truncate app.payments', '55000', 'the owner cannot truncate payments');
select test.refused(format($$update app.stock_movements set quantity_change = 1 where id = %L$$, :'movement'),
  '55000', 'the owner cannot update a stock movement');
select test.refused('truncate app.stock_movements', '55000', 'the owner cannot truncate stock');

-- Not a staff member, so no station: the functions refuse it too.
select test.assert(app.current_station() is null, 'the owner has no station');
select test.refused($$select app.record_payment(gen_random_uuid(), 1, 'cash')$$,
  '42501', 'the owner cannot take a payment through the application');

rollback;
\echo 'OK: app_owner'
