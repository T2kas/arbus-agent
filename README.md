# Arbus Market Agent — market creator

Standalone service that generates prediction-market candidates for **Arbus**,
a Lithuanian prediction app (virtual credits / Arbukai, audience 16-35).
This repo covers **Job 1 (market creation)** only; resolution monitoring
(Job 2) plugs in later as a separate module.

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
 6. OUTPUT     SQLite + reports/batch_*.md + exports/batch_*.json
               + optional Telegram ping                        [pure code]
```

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
