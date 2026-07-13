# Arbus Market Agent — market creator

Standalone service that generates prediction-market candidates for **Arbus**,
a Lithuanian prediction app (virtual credits / Arbukai, audience 16-35).
This repo covers **Job 1 (market creation)** only; resolution monitoring
(Job 2) plugs in later as a separate module.

## Design: the LLM is one stage, not the whole agent

Reliability comes from deterministic scaffolding around the model:

```
 1. HARVEST    feedparser pulls LT headlines (LRT, Delfi, 15min, Lrytas,
               BasketNews, VŽ) from the last 4 days           [pure code]
 2. DRAFT      Claude (opus-4-8) + live web search, geolocated to Vilnius,
               scouts trends and drafts N quick-mode candidates   [LLM]
 3. STRUCTURE  Claude structured outputs -> validated Pydantic schema [LLM]
 4. VALIDATE   gambling-language linter, Lithuanian check, date sanity,
               probability normalization, fuzzy dedupe vs SQLite  [pure code]
 5. VERIFY     "already decided?" web check per candidate —
               Perplexity Sonar if key set, else Claude+search    [LLM]
 6. OUTPUT     SQLite + reports/batch_*.md + exports/batch_*.json
               + optional Telegram ping                        [pure code]
```

Hard guarantees enforced in code (not just prompt):

- **No gambling vocabulary** in any user-facing text (`lažyb-`, `statym-`,
  `koeficient-`, `odds`, `jackpot`... — bare Lithuanian "bet" = "but" is allowed).
- **Lithuanian-only** question text.
- **Future, parseable resolution dates**; duration class recomputed from the
  date, never trusted from the model.
- **Probabilities clamped** to [0.02, 0.98] and normalized to 1.0.
- **Near-duplicate rejection** against the last 60 days of markets
  (rapidfuzz token-set ratio ≥ 87).
- **Already-decided rejection** via a live web verification pass — the
  historical #1 failure mode of naive generators.

## Commands

```sh
pip install -r requirements.txt
cp .env.example .env            # fill in ANTHROPIC_API_KEY

python -m arbus generate                 # full batch (default 35)
python -m arbus generate --count 20      # smaller batch
python -m arbus generate --dry-run       # harvest only, zero API cost
python -m arbus generate --skip-verify   # skip stage 5 (cheaper, riskier)

python -m arbus list                     # browse stored candidates
python -m arbus list --status needs_review

python -m arbus promote 12 17 23         # full-mode specs for launch picks
```

`generate` produces quick-mode candidates only. When your team picks winners
from the report, run `promote` — it re-verifies each pick against the live
web and writes the full spec (trigger sentence, definitions, edge cases,
primary + independent backup source, early-resolution logic, freeze hint)
to `reports/specs_<batch>.md` and the DB.

## Scheduled runs (GitHub Actions)

`.github/workflows/generate.yml` runs Mon + Thu 08:00 Vilnius time and
commits `data/`, `reports/`, `exports/` back to the repo (same commit-back
pattern as the weather runner). Set repo secrets:

- `ANTHROPIC_API_KEY` (required)
- `PERPLEXITY_API_KEY` (optional — cheaper/cited verification)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (optional — batch-ready ping)

Manual run: Actions → generate-markets → Run workflow.

## Tuning

- Feeds, banned stems, batch size, dedupe threshold, duration boundaries:
  `arbus/config.py`.
- Editorial voice and category weighting: `prompts/system.md` and
  `prompts/draft.md` — these are the knobs to iterate on when batch quality
  drifts.
- Verify feed URLs occasionally (`python -m arbus generate --dry-run` shows
  what harvest sees); dead feeds are skipped, not fatal.

## Costs (rough)

One full batch ≈ 1 large research call + 1 structuring call + ~5 verification
calls on `claude-opus-4-8` ≈ $1–3 depending on search volume. Twice weekly
→ roughly $10–25/month. Perplexity for stage 5 lowers this.

## Tests

```sh
pip install pytest
python -m pytest tests/ -q
```

Offline only — validation gates, linter, dedupe. No API key needed.
