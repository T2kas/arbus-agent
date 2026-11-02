-- Arbus: let the bot move an open market's prices to a new forecast (repricing).
--
-- Why: weather (orai) markets were priced ONCE, ~40 h before they close, and
-- then only trades moved them. Every newer model run (ICON/ECMWF/UKMO publish
-- 4× a day) made the old price stale, and the best weather players simply bet
-- the bucket the newest run pointed at, at a price that still reflected the old
-- run. The bot now recomputes the price from the newest runs every pass and
-- calls this function, the way a bookmaker moves a line.
--
-- What it does: sets the probabilities of a market's OPEN options to the given
-- values (scaled so the open options sum to exactly 100 minus whatever the
-- settled options hold, each kept inside [0.5, 99.5]) and writes a price-history
-- row so the chart moves. Nothing else changes: no trades, no positions, no
-- volume, no house P&L — existing holders keep their shares; a reprice just
-- quotes new prices for the next trade, exactly as a trade's price move does.
--
-- Only exclusive (single_outcome) markets that are open. Callable by the bot's
-- service_role key or by an admin.
--
-- Run once in the Supabase SQL editor.

create or replace function public.admin_reprice_market(
  p_market_id uuid,
  p_probabilities jsonb          -- {"<option uuid>": 63.5, ...} on the 0-100 scale
)
returns integer                   -- number of options repriced
language plpgsql security definer set search_path to 'public'
as $function$
declare
  v_status    text;
  v_structure text;
  v_open      integer;
  v_given     integer;
  v_sum       numeric;
  v_target    numeric;
  v_resid     numeric;
  v_top       uuid;
begin
  if auth.role() <> 'service_role' and not coalesce(public.is_admin(), false) then
    raise exception 'not authorized';
  end if;

  select status, outcome_structure into v_status, v_structure
    from public.markets where id = p_market_id for update;
  if v_status is null then raise exception 'market not found'; end if;
  if v_status <> 'open' then raise exception 'market is not open'; end if;
  if v_structure is distinct from 'single_outcome' then
    raise exception 'only single_outcome markets can be repriced';
  end if;

  -- every open option must be given a positive number, and nothing else
  select count(*) into v_open from public.market_options
   where market_id = p_market_id and resolved_outcome is null;
  select count(*), coalesce(sum((value)::numeric), 0) into v_given, v_sum
    from jsonb_each_text(p_probabilities) e
    join public.market_options o
      on o.id = (e.key)::uuid and o.market_id = p_market_id and o.resolved_outcome is null
   where (value)::numeric > 0;
  if v_given <> v_open or v_given <> (select count(*) from jsonb_object_keys(p_probabilities)) then
    raise exception 'give a positive probability for every open option of this market (and only those)';
  end if;

  -- open options share what the settled ones do not hold (100 normally)
  select 100 - coalesce(sum(probability), 0) into v_target
    from public.market_options
   where market_id = p_market_id and resolved_outcome is not null;
  if v_target <= 0 then v_target := 100; end if;

  update public.market_options o
     set probability = least(greatest(round((p_probabilities ->> o.id::text)::numeric
                                            * v_target / v_sum, 4), 0.5), 99.5)
   where o.market_id = p_market_id and o.resolved_outcome is null;

  -- rounding/clamping residue to the largest open option: the book sums exactly
  select v_target - sum(probability) into v_resid
    from public.market_options
   where market_id = p_market_id and resolved_outcome is null;
  if v_resid <> 0 then
    select id into v_top from public.market_options
     where market_id = p_market_id and resolved_outcome is null
     order by probability desc, sort_order limit 1;
    update public.market_options set probability = probability + v_resid where id = v_top;
  end if;

  insert into public.option_price_history (market_id, option_id, probability)
  select market_id, id, probability
    from public.market_options
   where market_id = p_market_id and resolved_outcome is null;

  return v_open;
end;
$function$;

revoke all on function public.admin_reprice_market(uuid, jsonb) from public, anon;
grant execute on function public.admin_reprice_market(uuid, jsonb) to authenticated, service_role;

notify pgrst, 'reload schema';

-- Check (read-only) after a few bot passes: the newest price rows of open orai markets.
-- select m.title, o.label, o.probability
--   from public.markets m join public.market_options o on o.market_id = m.id
--  where m.category = 'orai' and m.status = 'open' order by m.closes_at, o.sort_order;
