# Arbus Market Agent — market creator

Standalone service that generates prediction-market candidates for **Arbus**,
a Lithuanian prediction app (virtual credits / Arbukai, audience 16-35).
Covers **Job 1 (market creation)** and **Job 2 (resolution monitoring)**.

## Design: the LLM is one stage, not the whole agent

Reliability comes from deterministic scaffolding around the model:

```
 1. HARVEST    feedparser pulls LT headlines (LRT, Delfi, 15min, Lrytas, VŽ
               + sports feeds) from the last 4 days           [pure code]
 1b. PULSE     real attention signal news RSS misses — what LT is SEARCHING
               (Google Trends LT), DISCUSSING (Reddit r/lietuva), LOOKING UP
               (Wikipedia LT), and TRENDING on TikTok (Creative Center),
               each with a checkable number; + optional YouTube Trending LT
               with a free key                                 [pure code]
 2. DRAFT      web-grounded LLM scouts trends off BOTH signals and drafts N
               quick-mode candidates                              [LLM]
 3. STRUCTURE  structured output -> validated Pydantic schema     [LLM]
 4. VALIDATE   gambling-language linter, Lithuanian check, date sanity,
               probability normalization, fuzzy dedupe vs SQLite  [pure code]
 5. VERIFY     "already decided?" live web check per candidate    [LLM]
 6. IMAGES     og:image pulled from each market's own source article
               (relevant by construction, no key, no generation) [pure code]
 7. OUTPUT     SQLite + reports/batch_*.md + exports/batch_*.json
               + optional Telegram ping                        [pure code]
 8. PUBLISH    `arbus publish <id>` POSTs approved markets to the app
               (manual, per-market, never automatic)           [pure code]
```

Drafting is **themed**: each chunk carries a mandatory theme (state &
geopolitics 30%, economy & finance 30%, sport 20%, culture 20%), so balance is
enforced in code rather than requested in a prompt — a chunk mandated to state
affairs cannot answer with a view-count market.

### Why the PULSE stage exists

A web-search LLM cannot actually "go into TikTok" — its index is news-article
biased, so telling it to find social trends just launders press coverage or
invents popularity. The pulse fixes this at the input: it feeds the drafter
**real, current, attention-weighted signal** from sources with public
structured endpoints (search volume, upvotes + comments, pageviews), harvested
the same resilient way as RSS. Those numbers double as hard evidence for the
attention gate. Every source is fail-safe — a dead or rate-limited source is
logged and skipped, and an empty pulse just means news-only for that run.
Sources live in a registry in `arbus/pulse.py`; add one (e.g. Spotify Top 50)
by writing a fetcher and appending it. Zero-auth sources need no keys; keyed
sources (YouTube) stay inert until their key is set. TikTok (Creative Center)
is best-effort — it fights automated access and may not cover Lithuania; if
`--dry-run` shows no TikTok lines, adjust `TIKTOK_COUNTRY` in `config.py` or
fall back to a paid scraper. It never breaks a batch when empty.

## Market images

Every market gets a picture from its own source article's `og:image` tag, so
the image is about the exact subject with no generation cost and no API key.
Markets whose sources expose no image simply carry none — a missing picture
never costs you a market. Turn the stage off with `IMAGES_ENABLED = False`.

> **Rights:** these are press photos owned by the outlet. Fine for internal
> review; before showing them to users, clear usage with the outlet or swap in
> your own artwork. `image_source` records the page each image came from.

## Publishing to the app

```sh
python -m arbus publish 237 242 --dry-run   # print the exact payload
python -m arbus publish 237 242             # POST to the app
```

Add the endpoint to `.env` when it exists:

```sh
ARBUS_API_URL=https://api.arbus.lt/markets
ARBUS_API_KEY=...
```

Publishing is manual and per-market — markets go live only for ids you pass,
never automatically at the end of a batch. Rejected markets are refused, and
an already-published market is skipped unless you pass `--force`, so repeating
the command cannot double-post. `--dry-run` works without any endpoint
configured and prints the payload contract to hand to whoever builds the API:

```json
{
  "external_id": "arbus-237", "question": "...", "type": "binary",
  "options": ["Taip", "Ne"], "probabilities": [0.4, 0.6],
  "category": "ekonomika", "resolve_by": "2026-10-01",
  "duration_class": "long", "resolution_criteria": "...",
  "sources": ["https://..."], "image_url": "https://...",
  "language": "lt", "generated_at": "2026-07-25T13:53:00+00:00"
}
```

## Job 2 — the resolution system

Implements the Notion spec *"Resolution logika (supaprastintas draft)"*. Arbus
runs an AMM and takes the other side of user bets, so a market still trading
after its outcome is knowable leaks money to whoever knows. Freezing is
therefore instant; everything after it is deliberate.

```
OPEN ──┬─ circuit breaker ──┐
       └─ resolution request ┴──> PENDING ──> RESOLVING ──> RESOLVED / VOID
                                    (admin)    (undo window)      or back to OPEN
```

**Two entry paths, one frozen state.** The circuit breaker watches for a price
move of `CB_PRICE_MOVE` inside `CB_WINDOW_MINUTES` **made by at least
`CB_MIN_DISTINCT_USERS` distinct users** — one whale betting big is prediction,
not a leak, so both conditions are required. Separately, any eligible user
(≥20 predictions) can stake a bond and report the outcome. Either way trading
stops immediately.

**The AI decides nothing.** `arbus.aicheck` reads the cited source and reports
whether it says what the reporter claims, plus anything that should make the
admin hesitate (~€0.05 per check: one call, three searches at $0.01 each plus
the tokens the results add). That summary is admin input, not a verdict, and
`python -m arbus check` pushes it straight to the team's Telegram group with
the market, the claim, the source and the rules — so the decision happens on a
phone, not after someone remembers to open the dashboard.

**A market is never voided because an event was cancelled.** Every market's
rules state up front what non-occurrence means — for a Taip/Ne market the
default is "Ne" — the same way Polymarket and Kalshi write the edge case into
the rules instead of cancelling afterwards. The validator appends that clause
when the model forgets it, so no market can ship without one. `VOID` survives
only as a last resort for a market whose rules cannot be applied at all, and
`admin_decide` refuses it without an explicit `void_reason`.

**Settlement waits.** The admin decides in the app dashboard; the payout lands
`SETTLEMENT_DELAY_MINUTES` later, so a misclick can be cancelled. Once Arbucks
are paid there is no way back — which is why the delay exists.

```sh
python -m arbus resolve            # sweep markets past their date (dry run)
python -m arbus resolve --apply    # freeze the clearly-decided ones for review
python -m arbus check              # AI-check frozen markets + Telegram alert
python -m arbus settle             # pay out decisions whose undo window expired
```

`settle` must run on a short schedule (every minute or two) — until it does, a
decision is still reversible and nothing has been paid. It is pure local SQLite
(“is any settle_at in the past? then pay”): **no LLM, no web search, no API
cost** — running it every minute forever costs nothing but the process.

`check` is the only step that spends money, and only when something is actually
frozen: one call per waiting market, never on a schedule of its own.

### Economy (v1, from the spec)

| | Arbucks |
|---|---:|
| Proposal bond (standard / important) | 200 / 450 |
| Challenge bond | 450 |
| Reward, correct report | +30 |
| Reward, correct challenge | 50% of the proposal bond |

A correct reporter gets their bond back plus the reward; a wrong one forfeits
it. A challenge is correct exactly when the proposal it disputed was wrong. On
`VOID` or a return to `OPEN` **every bond is returned** — nobody was proven
wrong, and punishing good-faith reports on hard markets would stop reporting
altogether.

For reference, Polymarket's equivalent is a **$750 proposer bond, a dispute
bond of the same size, and a 2-hour challenge window**; the proposer's reward
is a few dollars on that bond. Our window matches theirs; our reward is much
more generous relative to the bond (30 on 200 = 15 %, vs well under 1 % there)
because Arbucks are virtual and we need reporting to start at all.

Balances live in this repo's SQLite (`arbus/ledger.py`) so the numbers can be
tuned without app-backend work. The ledger is append-only: a balance is the sum
of its entries, so every Arbuck has a row explaining why — which is what makes
a disputed resolution auditable. Moving balances to the app later means
rewriting that one file, not the engine.

Reputation is accumulated and displayable only; per the spec it does **not**
change bond or reward sizes in v1.

## Backing up the database

`data/arbus.db` is **local state, not source**, and is deliberately untracked.
It holds every market ever generated and is what stops the bot repeating
itself, so it is worth keeping — but it is rewritten by every batch, and a
tracked binary file collides on every `git pull`.

```sh
copy data\arbus.db data\arbus.db.backup    # Windows
cp data/arbus.db data/arbus.db.backup      # macOS / Linux
```

Losing it costs the dedupe history (the bot may re-propose old markets), not
the reports — `reports/` and `exports/` are tracked as usual.

## Teaching the bot (feedback loop)

The bot improves from plain-language notes, no code required. `feedback.md` at
the repo root is read before **every** batch and its bullets are injected into
the draft prompt as hard rules that override the default category mix. Add a
note three ways:

```sh
python -m arbus feedback "mažiau ekonomikos rinkų"   # from the terminal
# or in Telegram:  /feedback daugiau TikTok temų
# or just edit feedback.md in any text editor
```

Say "less economics", "stop the pension markets", "more culture like Dirkstys"
— the next batch obeys. `#` heading lines and the `<!-- -->` instruction block
are ignored, so only your bullets reach the model.

## Providers

Auto-detected from which API key is set (force with `LLM_PROVIDER=`):

| Provider | Models | Notes |
|---|---|---|
| **Perplexity** (default) | `sonar-pro` research/verify, `sonar` structuring | Search-native, cheap (~$0.10-0.30/batch). |
| **Anthropic** | `claude-opus-4-8` + web search tool | Sharper editorial drafting and the strongest "is this already decided?" judgement; `LLM_PROVIDER=anthropic`. |
| **Z.AI (GLM)** | `glm-4.6` | `LLM_PROVIDER=zai` + `ZAI_API_KEY`. **Not search-native** — grounding relies on Z.AI's server-side `web_search` tool, and if that tool is rejected the call is retried without it and the model answers from memory. Watch the batch for stale facts before trusting it. |

Hard guarantees enforced in code (not just prompt):

- **No gambling vocabulary** in user-facing text (`lažyb-`, `statym-`,
  `koeficient-`, `odds`... — bare Lithuanian "bet" = "but" is allowed).
- **Lithuanian-only** question text; **future, parseable** resolution dates;
  duration class recomputed from the date, never trusted from the model.
- **Probabilities clamped** to [0.02, 0.98] and normalized to 1.0.
- **Near-duplicate rejection** vs the last 60 days (rapidfuzz ≥ 87).
- **Already-decided rejection** via live web verification — the #1 failure
  mode of naive generators.

## Cost control

A full Opus batch with adaptive thinking and 16 searches per chunk costs about
**$5**. The defaults now target a fraction of that, and the levers are:

| Lever | Where | Effect |
|---|---|---|
| Model | `ANTHROPIC_MODEL` (default `claude-sonnet-5`) | Sonnet instead of Opus is the single biggest saving |
| Per-stage provider | `LLM_PROVIDER_DRAFT` / `LLM_PROVIDER_VERIFY` | Draft cheap, verify sharp — see below |
| Searches | `SEARCH_MAX_USES_DRAFT` (6), `SEARCH_MAX_USES_VERIFY` (4) | Each search costs money *and* injects pages into context |
| Thinking | `ANTHROPIC_THINKING=off` | Thinking tokens are billed as output |
| Prompt caching | automatic | The system prompt is cached across chunks |
| Batch size | `--count 15` | Cost scales with candidates |

**Recommended setup — cheap drafting, sharp verification.** Drafting is many
long, search-heavy calls; verification is the short judgement call that decides
whether a market is already dead. Put each where it belongs:

```sh
LLM_PROVIDER=perplexity          # drafting + structuring (cents)
LLM_PROVIDER_VERIFY=anthropic    # only the fact-checking runs on Claude
```

That keeps the accuracy where it matters at a fraction of an all-Claude batch.
Start small (`--count 15`) and check your Anthropic usage page after one run
before scaling up.

## Setup

```sh
pip install -r requirements.txt
cp .env.example .env     # add PERPLEXITY_API_KEY (or ANTHROPIC_API_KEY)
```

## Commands

```sh
python -m arbus generate                 # full batch (default 35)
python -m arbus generate --count 20      # smaller batch
python -m arbus generate --dry-run       # harvest + pulse only, zero API cost
python -m arbus generate --skip-verify   # skip stage 5 (cheaper, riskier)

python -m arbus list                     # browse stored candidates
python -m arbus list --status needs_review

python -m arbus promote 12 17 23         # full-mode specs for launch picks

python -m arbus feedback "mažiau ekonomikos rinkų"   # teach every future batch

python -m arbus bot                      # Telegram bot (see below)
```

`generate` produces quick-mode candidates only. When your team picks winners
from the report, run `promote` — it re-verifies each pick against the live
web and writes the full spec (trigger sentence, definitions, edge cases,
primary + independent backup source, early-resolution logic, freeze hint)
to `reports/specs_<batch>.md` and the DB.

## Telegram bot

Generate batches by typing in Telegram:

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`), copy
   the token into `.env` as `TELEGRAM_BOT_TOKEN`.
2. Run `python -m arbus bot`, message your bot `/id`, put the returned chat
   id into `.env` as `TELEGRAM_CHAT_ID`, restart the bot.
3. Commands: `/markets`, `/markets 15`, `/markets 15 fast` (skips
   verification), `/feedback <pastaba>` (teach the bot), `/help`. The bot only
   obeys the configured chat.

The bot is long-polling — it runs wherever you start it (your PC is fine)
and needs no server or webhook.

For a **team group** that receives the freeze alerts (`python -m arbus check`),
follow [docs/telegram-grupe.md](docs/telegram-grupe.md) — create a group, add
the bot, `/id`, and put the group's (negative) id into `TELEGRAM_CHAT_ID`.

## GitHub Actions (manual only)

`.github/workflows/generate.yml` has **no schedule** — trigger it from the
Actions tab → generate-markets → Run workflow. It commits `data/`,
`reports/`, `exports/` back to the repo. Repo secrets used:
`PERPLEXITY_API_KEY` (or `ANTHROPIC_API_KEY`), optional `TELEGRAM_BOT_TOKEN`
+ `TELEGRAM_CHAT_ID` for the batch-ready ping.

## Tuning

- Feeds, banned stems, batch size, dedupe threshold, models: `arbus/config.py`.
- Pulse sources, per-source caps, Reddit subs, optional keys: `arbus/config.py`
  (`PULSE_*`, `REDDIT_SUBS`, `GOOGLE_TRENDS_GEO`); set `PULSE_ENABLED=False` to
  run news-only.
- Editorial voice and category weighting: `prompts/system.md` and
  `prompts/draft.md` — the knobs to iterate on when batch quality drifts.
- `python -m arbus generate --dry-run` shows what harvest AND the pulse see;
  dead feeds/sources are skipped, not fatal.

## Tests

```sh
pip install pytest
python -m pytest tests/ -q     # offline — no API key needed
```
