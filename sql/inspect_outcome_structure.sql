-- READ-ONLY. Changes nothing. Dumps everything needed to add a new
-- "independent" outcome structure safely: column types, enum values, check
-- constraints, triggers, and the full source of every function that touches
-- outcome_structure / resolution_kind / option prices / settlement.
--
-- Run in Supabase SQL Editor, then "Download CSV" and save the file as
--   C:\Users\HP\Desktop\arbus-agent\sql\schema_dump.csv

select 'column' as kind,
       table_name || '.' || column_name as name,
       data_type || ' / ' || udt_name || ' / default: ' || coalesce(column_default, '') as definition
  from information_schema.columns
 where table_schema = 'public'
   and column_name in ('outcome_structure', 'resolution_kind')

union all
select 'enum', t.typname, string_agg(e.enumlabel, ', ' order by e.enumsortorder)
  from pg_type t
  join pg_enum e on e.enumtypid = t.oid
 group by t.typname

union all
select 'constraint', conrelid::regclass::text || '.' || conname, pg_get_constraintdef(oid)
  from pg_constraint
 where contype = 'c'
   and conrelid in ('public.markets'::regclass, 'public.market_options'::regclass)

union all
select 'trigger', event_object_table || '.' || trigger_name,
       action_timing || ' ' || event_manipulation || ' ' || action_statement
  from information_schema.triggers
 where trigger_schema = 'public'
   and event_object_table in ('markets', 'market_options', 'option_price_history')

union all
select 'function',
       p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
       pg_get_functiondef(p.oid)
  from pg_proc p
  join pg_namespace n on n.oid = p.pronamespace
 where n.nspname = 'public'
   and p.prokind = 'f'
   and (p.prosrc ilike '%outcome_structure%'
        or p.prosrc ilike '%resolution_kind%'
        or p.prosrc ilike '%cumulative%'
        or p.prosrc ilike '%survival%'
        or p.prosrc ilike '%probability%'
        or p.prosrc ilike '%resolved_outcome%'
        or p.prosrc ilike '%winning_option%')
 order by 1, 2;
