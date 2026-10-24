-- Arbus: keep price charts honest when an outcome is eliminated.
--
-- Problem: after an elimination the remaining options are renormalised and a
-- new price point is written at that moment, but the previous point can be
-- hours old. Charts interpolate linearly between the two, so e.g. Klaipėda
-- 2026-10-24 "≥13" seemed to climb slowly 3.8% -> 99.5% over ~3 h while
-- "11–12.9" still showed ~96% -> visible sums of 130–190%. Stored prices
-- always summed to 100; only the drawing was wrong.
--
-- Fix: just before an option gets settled, store a snapshot of the market's
-- current open prices (1 ms earlier). The chart then stays flat until the
-- elimination and jumps exactly at it.
--
-- This is a trigger, so it does not replace any existing function and needs
-- no checks. Safe to run more than once. Paste the whole file into the
-- Supabase SQL editor and press Run.

create or replace function public._snapshot_prices_before_settle()
returns trigger
language plpgsql security definer set search_path to 'public'
as $function$
begin
  -- one snapshot per market per transaction (several options may be settled)
  if not exists (
    select 1 from public.option_price_history
     where market_id = new.market_id
       and created_at = now() - interval '1 millisecond'
  ) then
    insert into public.option_price_history (market_id, option_id, probability, created_at)
    select market_id, id, probability, now() - interval '1 millisecond'
      from public.market_options
     where market_id = new.market_id and resolved_outcome is null;
  end if;
  return new;
end;
$function$;

revoke all on function public._snapshot_prices_before_settle() from public, anon, authenticated;

drop trigger if exists trg_snapshot_prices_before_settle on public.market_options;
create trigger trg_snapshot_prices_before_settle
  before update of resolved_outcome on public.market_options
  for each row
  when (old.resolved_outcome is null and new.resolved_outcome is not null)
  execute function public._snapshot_prices_before_settle();

-- For a perfect chart the app should also draw step lines (Recharts
-- type="stepAfter", Chart.js stepped: 'before') and end an eliminated
-- option's line at 0 at its elimination time instead of holding it flat.
