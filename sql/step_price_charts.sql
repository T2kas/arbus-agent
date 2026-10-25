-- Arbus: price charts as steps (flat until a trade, then a jump).
--
-- Problem: option_price_history only gets a row when a price changes, and the
-- chart draws a straight line between consecutive rows. A market that sat at
-- 52% for 19 days and then got one big buy looked like a slow, steady climb
-- (Žalgiris–Partizan 2026-10-25), and after eliminations the open outcomes
-- seemed to sum to 130–190% for hours (Klaipėda 2026-10-24).
--
-- Fix, entirely in the database (no app change needed):
--   1) a trigger: whenever a new price row is written, first write the option's
--      PREVIOUS price 1 ms earlier. The chart then stays flat until the trade and
--      jumps straight up/down at it.
--   2) a one-off backfill that does the same for all existing history, so old
--      charts are fixed too.
--
-- Safe to run more than once. Paste the whole file into the Supabase SQL editor
-- and press Run. It does not replace any existing function.

-- The earlier, narrower version of this fix (snapshot only on elimination) is
-- superseded by the general trigger below.
drop trigger if exists trg_snapshot_prices_before_settle on public.market_options;
drop function if exists public._snapshot_prices_before_settle();


-- the trigger looks up an option's latest price on every insert
create index if not exists option_price_history_option_created_idx
  on public.option_price_history (option_id, created_at desc);


-- ── 1) trigger: hold the previous price until the moment of change ──────────
create or replace function public._price_history_step()
returns trigger
language plpgsql security definer set search_path to 'public'
as $function$
declare
  v_prev_p  numeric;
  v_prev_at timestamptz;
begin
  if pg_trigger_depth() > 1 then          -- our own step row: nothing more to do
    return new;
  end if;
  if new.created_at is null then
    new.created_at := now();
  end if;

  select probability, created_at into v_prev_p, v_prev_at
    from public.option_price_history
   where option_id = new.option_id and created_at < new.created_at
   order by created_at desc
   limit 1;

  if v_prev_at is not null
     and v_prev_p is distinct from new.probability
     and new.created_at - v_prev_at > interval '1 second' then
    insert into public.option_price_history (market_id, option_id, probability, created_at)
    values (new.market_id, new.option_id, v_prev_p, new.created_at - interval '1 millisecond');
  end if;
  return new;
end;
$function$;

revoke all on function public._price_history_step() from public, anon, authenticated;


-- ── 2) backfill existing history (before the trigger exists) ────────────────
drop trigger if exists trg_price_history_step on public.option_price_history;

insert into public.option_price_history (market_id, option_id, probability, created_at)
select h.market_id, h.option_id, h.prev_p, h.created_at - interval '1 millisecond'
  from (
    select market_id, option_id, probability, created_at,
           lag(probability) over w as prev_p,
           lag(created_at)  over w as prev_at
      from public.option_price_history
    window w as (partition by option_id order by created_at)
  ) h
 where h.prev_at is not null
   and h.prev_p is distinct from h.probability
   and h.created_at - h.prev_at > interval '1 second'
   and not exists (
     select 1 from public.option_price_history x
      where x.option_id = h.option_id
        and x.created_at = h.created_at - interval '1 millisecond'
   );

create trigger trg_price_history_step
  before insert on public.option_price_history
  for each row
  execute function public._price_history_step();


-- ── check (read-only): Žalgiris–Partizan should now show flat 52% → jump ────
select h.created_at, o.label, h.probability
  from public.option_price_history h
  join public.market_options o on o.id = h.option_id
  join public.markets m on m.id = h.market_id
 where m.title ilike '%Partizan%' and m.status <> 'resolved'
 order by h.created_at, o.sort_order;
