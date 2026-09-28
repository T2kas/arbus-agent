-- Arbus: keep a market's open outcomes summing to 100% after early elimination.
--
-- Problem: admin_eliminate_options settles each eliminated option to "Ne"
-- (probability -> 0) but leaves the remaining open options at their old
-- probability, so during the day the open outcomes sum to < 100%.
-- Fix: after eliminating, rescale the remaining OPEN options proportionally so
-- they sum to exactly 100 (the dead bucket's probability mass moves to the live
-- ones, like on Polymarket). Runs inside the same transaction as the
-- elimination, so it also covers eliminations made from the dashboard.
--
-- Run steps 1 and 2 once in the Supabase SQL editor. Step 3 is an optional preview
-- plus a one-off repair of markets that are already short of 100 today.

-- ── 1) helper: renormalise one market's open options to 100 ─────────────────
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
  select coalesce(sum(probability), 0), count(*) into v_sum, v_n
    from public.market_options
   where market_id = p_market_id and resolved_outcome is null;

  if v_n = 0 or v_sum <= 0 then return; end if;          -- nothing open
  if abs(v_sum - 100) < 0.005 then return; end if;      -- already 100

  -- proportional rescale; keep every open option strictly inside (0, 100)
  update public.market_options
     set probability = least(greatest(round(probability * 100 / v_sum, 2), 0.50), 99.50)
   where market_id = p_market_id and resolved_outcome is null;

  -- rounding residue goes to the largest open option so the sum is exactly 100
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

  -- record the new prices so the chart moves too
  insert into public.option_price_history (market_id, option_id, probability)
  select market_id, id, probability
    from public.market_options
   where market_id = p_market_id and resolved_outcome is null;
end;
$function$;

-- internal helper: not callable by app users directly
revoke all on function public._renormalize_open_options(uuid) from public, anon, authenticated;


-- ── 2) elimination now renormalises before finalising ───────────────────────
-- Same as the existing _adm_eliminate_options, with ONE added line (marked).
create or replace function public._adm_eliminate_options(p_market_id uuid, p_option_ids uuid[])
 returns table(eliminated integer, paid_out bigint)
 language plpgsql security definer set search_path to 'public'
as $function$
declare
  v_status text; v_ids uuid[]; v_n integer; v_open integer;
  v_elim integer := 0; v_paid bigint := 0; v_w integer; v_p bigint; r record;
begin
  if auth.uid() is null and auth.role() <> 'service_role' then
    raise exception 'not authenticated';
  end if;
  select coalesce(array_agg(distinct x), '{}'::uuid[]) into v_ids
    from unnest(coalesce(p_option_ids, '{}'::uuid[])) x where x is not null;
  v_n := coalesce(array_length(v_ids, 1), 0);
  if v_n = 0 then raise exception 'no options to eliminate'; end if;
  select status into v_status from public.markets where id = p_market_id for update;
  if v_status is null then raise exception 'market not found'; end if;
  if v_status = 'resolved' then raise exception 'market already resolved'; end if;
  perform 1 from public.market_options
    where market_id = p_market_id and id = any(v_ids) and resolved_outcome is null
    having count(*) = v_n;
  if not found then
    raise exception 'option does not belong to this market or is already resolved';
  end if;
  select count(*) into v_open from public.market_options
    where market_id = p_market_id and resolved_outcome is null;
  if v_open <= v_n then
    raise exception 'cannot eliminate all remaining options; resolve a winner instead';
  end if;
  for r in
    select id from public.market_options
    where market_id = p_market_id and id = any(v_ids) and resolved_outcome is null
  loop
    select s.winners, s.paid_out into v_w, v_p
      from public._settle_option(r.id, 'no') s;
    v_elim := v_elim + 1;
    v_paid := v_paid + coalesce(v_p, 0);
  end loop;
  perform public._renormalize_open_options(p_market_id);   -- ← ADDED: open options back to 100%
  perform public._finalize_market_if_settled(p_market_id);
  return query select v_elim, v_paid;
end;$function$;


-- ── 3) optional: preview + repair markets that are short of 100 right now ───
-- Preview (read-only): open markets with an eliminated option and their open sum.
select m.id, m.title,
       round(sum(o.probability) filter (where o.resolved_outcome is null), 2) as open_sum
  from public.markets m
  join public.market_options o on o.market_id = m.id
 where m.status <> 'resolved'
 group by m.id, m.title
having bool_or(o.resolved_outcome = 'no')
   and bool_or(o.resolved_outcome is null);

-- Repair (writes): renormalise each of those markets once.
select public._renormalize_open_options(m.id)
  from public.markets m
 where m.status <> 'resolved'
   and exists (select 1 from public.market_options o
                where o.market_id = m.id and o.resolved_outcome = 'no')
   and exists (select 1 from public.market_options o
                where o.market_id = m.id and o.resolved_outcome is null);
