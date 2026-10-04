-- Arbus: lower the liquidity of every unresolved market.
--
-- Anchors (team direction 2026-10-04): 150k -> 20k, 75k -> 10k, 5k -> 3k.
-- Values in between follow a smooth curve through those points (log scale),
-- rounded to 500. Examples:
--   1k -> 500 · 2.5k -> 1.5k · 10k -> 4k · 20k -> 5.5k · 25k -> 6k · 30k -> 6.5k
--   50k -> 8.5k · 100k -> 13.5k · 200k -> 26.5k · 300k -> 40k
--
-- What it changes: only markets.liquidity (the LMSR "b"). Prices, positions
-- and balances are untouched. From now on a bet moves the price more (7.5x
-- more on a 150k market). Selling a position also gets more slippage: a big
-- holder selling now gets back less than they would have under the old liquidity.
--
-- Run in the Supabase SQL Editor ONE STEP AT A TIME:
--   step 1 (preview, read-only) -> check the list -> step 2 (writes).
-- Step 3 undoes it from the backup.


-- ── helper: old liquidity -> new liquidity ─────────────────────────────────
create or replace function public._liquidity_reduced(p_old numeric)
returns numeric language sql immutable as $$
  select case
    when v < 500 then greatest(100, round(v))
    else greatest(100, round(v / 500) * 500)
  end
  from (select case
          when p_old <= 5000  then p_old * 0.6
          when p_old <= 75000 then 3000 * power(10.0 / 3, ln(p_old / 5000) / ln(15))
          else p_old * 20000 / 150000
        end as v) s
$$;
revoke all on function public._liquidity_reduced(numeric) from public, anon, authenticated;


-- ── STEP 1 — preview (read-only) ───────────────────────────────────────────
select status, liquidity as old_liquidity,
       public._liquidity_reduced(liquidity) as new_liquidity,
       count(*) as markets
  from public.markets
 where status <> 'resolved' and liquidity is not null
 group by status, liquidity
 order by liquidity desc, status;


-- ── STEP 2 — apply (writes; keeps a backup for undo) ──────────────────────
create table if not exists public.liquidity_backup_20261004 (
  market_id uuid primary key,
  old_liquidity numeric not null,
  new_liquidity numeric not null,
  changed_at timestamptz not null default now()
);
alter table public.liquidity_backup_20261004 enable row level security;   -- no app access

insert into public.liquidity_backup_20261004 (market_id, old_liquidity, new_liquidity)
select id, liquidity, public._liquidity_reduced(liquidity)
  from public.markets
 where status <> 'resolved' and liquidity is not null
on conflict (market_id) do nothing;            -- re-running never overwrites the original

update public.markets m
   set liquidity = b.new_liquidity
  from public.liquidity_backup_20261004 b
 where b.market_id = m.id
   and m.liquidity = b.old_liquidity;          -- re-running is a no-op

select count(*) as changed from public.liquidity_backup_20261004;


-- ── STEP 3 — undo (only if needed) ────────────────────────────────────────
-- update public.markets m
--    set liquidity = b.old_liquidity
--   from public.liquidity_backup_20261004 b
--  where b.market_id = m.id;
