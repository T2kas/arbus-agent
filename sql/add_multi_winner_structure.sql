-- Arbus: new outcome structure "multi_winner" ("Nepriklausomos baigtys").
--
-- Each option is its own Taip/Ne market: its probability is priced on its own
-- (does not have to sum to 100 with the others), and ANY number of options can
-- resolve "Taip" — including none — each paying in full. No date ordering.
--
-- Why this is enough: markets with 3+ options already price every option on
-- its own book (_indep_apply). The "one winner" coupling happens only in
-- _rebalance_book for single_outcome (and ordering for cumulative_date /
-- survival); for any other structure it does nothing. Resolution
-- (_adm_resolve_market / _adm_resolve_option) already settles each option yes/no
-- in full and only applies extra rules to those three structures. So the new
-- structure only has to be ALLOWED — trading, resolving and pricing already
-- behave correctly for it.
--
-- Named "multi_winner" (not "independent") because _normalize_structure already
-- maps the legacy name 'independent' to single_outcome.
--
-- Run once in the Supabase SQL Editor (the whole file). Safe to re-run.

-- ── 1) allow the new value on markets.outcome_structure ─────────────────────
alter table public.markets drop constraint if exists markets_outcome_structure_check;
alter table public.markets add constraint markets_outcome_structure_check
  check (outcome_structure = any (array['single_outcome', 'cumulative_date', 'survival',
                                        'multi_winner']));


-- ── 2) the structure setter accepts it (only change: the allowed list) ──────
create or replace function public._adm_set_market_structure(p_market_id uuid, p_structure text)
 returns text
 language plpgsql
 security definer
 set search_path to 'public'
as $function$
declare
  v_structure text := public._normalize_structure(p_structure);
  v_status text;
  v_count integer;
begin
  if v_structure not in ('single_outcome', 'cumulative_date', 'survival', 'multi_winner') then
    raise exception 'structure must be single_outcome, cumulative_date, survival or multi_winner';
  end if;

  select status into v_status from public.markets where id = p_market_id for update;
  if v_status is null then
    raise exception 'market not found';
  end if;
  if v_status = 'resolved' then
    raise exception 'market already resolved';
  end if;

  select count(*) into v_count from public.market_options where market_id = p_market_id;
  if v_count < 3 and v_structure <> 'single_outcome' then
    raise exception 'only multi-outcome markets (3+ variants) take a structure';
  end if;

  if v_structure = 'single_outcome' and (
    select count(*) from public.market_options
      where market_id = p_market_id and resolved_outcome = 'yes') > 1 then
    raise exception 'more than one outcome already resolved YES';
  end if;
  if v_structure = 'cumulative_date' and exists (
    select 1 from public.market_options a
      join public.market_options b
        on b.market_id = a.market_id and (b.sort_order, b.id) > (a.sort_order, a.id)
      where a.market_id = p_market_id
        and a.resolved_outcome = 'yes' and b.resolved_outcome = 'no'
  ) then
    raise exception 'resolved deadlines are out of order';
  end if;
  if v_structure = 'survival' and exists (
    select 1 from public.market_options a
      join public.market_options b
        on b.market_id = a.market_id and (b.sort_order, b.id) > (a.sort_order, a.id)
      where a.market_id = p_market_id
        and a.resolved_outcome = 'no' and b.resolved_outcome = 'yes'
  ) then
    raise exception 'resolved deadlines are out of order';
  end if;

  update public.markets set outcome_structure = v_structure where id = p_market_id;

  if v_count >= 3 then
    perform public._rebalance_book(p_market_id, null);   -- no-op for multi_winner
    insert into public.option_price_history (market_id, option_id, probability)
      select market_id, id, probability
      from public.market_options
      where market_id = p_market_id;
  end if;

  return v_structure;
end;
$function$;


-- ── 3) early-elimination renormalise: one-winner markets only ───────────────
-- (the version from renormalize_after_elimination.sql rescaled ANY market,
-- which would wrongly push a multi_winner / date market's prices to sum 100)
create or replace function public._renormalize_open_options(p_market_id uuid)
returns void
language plpgsql security definer set search_path to 'public'
as $function$
declare
  v_sum   numeric;
  v_n     integer;
  v_resid numeric;
  v_top   uuid;
begin
  if (select public._normalize_structure(outcome_structure)
        from public.markets where id = p_market_id) is distinct from 'single_outcome' then
    return;
  end if;

  select coalesce(sum(probability), 0), count(*) into v_sum, v_n
    from public.market_options
   where market_id = p_market_id and resolved_outcome is null;

  if v_n = 0 or v_sum <= 0 then return; end if;
  if abs(v_sum - 100) < 0.005 then return; end if;

  update public.market_options
     set probability = least(greatest(round(probability * 100 / v_sum, 2), 0.50), 99.50)
   where market_id = p_market_id and resolved_outcome is null;

  if v_n > 1 then
    select 100 - sum(probability) into v_resid
      from public.market_options
     where market_id = p_market_id and resolved_outcome is null;
    if v_resid <> 0 then
      select id into v_top from public.market_options
       where market_id = p_market_id and resolved_outcome is null
       order by probability desc, sort_order
       limit 1;
      update public.market_options set probability = probability + v_resid
       where id = v_top;
    end if;
  end if;

  insert into public.option_price_history (market_id, option_id, probability)
  select market_id, id, probability
    from public.market_options
   where market_id = p_market_id and resolved_outcome is null;
end;
$function$;

revoke all on function public._renormalize_open_options(uuid) from public, anon, authenticated;


-- ── 4) check (read-only): should show the new list and 'multi_winner' ───────
select pg_get_constraintdef(oid) as allowed_structures
  from pg_constraint where conname = 'markets_outcome_structure_check';
select public._normalize_structure('multi_winner') as normalized;   -- expect: multi_winner
