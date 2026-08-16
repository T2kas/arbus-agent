# Kaip paleisti botą automatiškai (GitHub Actions)

Botas **nėra nuolat veikianti programa** — tai komandos, paleidžiamos pagal
tvarkaraštį. Nereikia serverio: viskas sukasi GitHub Actions'e. Trys darbai:

| Workflow | Ką daro | Tvarkaraštis | LLM? |
|---|---|---|---|
| `generate-markets` | kuria naujas rinkas | rankinis (mygtukas) | taip |
| `circuit-breaker` (`watch`) | stebi gyvą prekybą, įspėja, gali sustabdyti | kas 15 min | ne |
| `resolution-check` (`check`) | pataria baigtį užšaldytoms/pravėluotoms rinkoms | kas 6 val. | taip (pigiai) |

## 1. Vienkartinis paruošimas — Secrets

Settings → Secrets and variables → **Actions** → **Secrets** → New secret:

| Secret | Kam | Būtinas? |
|---|---|---|
| `ARBUS_API_URL` | app'o markets REST URL | taip |
| `ARBUS_API_KEY` | viešas anon raktas (skaitymui) | taip |
| `ANTHROPIC_API_KEY` | resolution check + generavimas | taip |
| `PERPLEXITY_API_KEY` | jei generuoji su Perplexity | pagal poreikį |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | žinutės komandai | taip |
| `ARBUS_WRITE_KEY` | **service_role** raktas rinkų stabdymui | tik jei nori auto-stop |

> `ARBUS_WRITE_KEY` — tą privilegijuotą raktą duosi tu. Be jo `watch` **tik
> įspėja** (nesustabdo). Anon raktas stabdyti negali (RLS neleidžia).

## 2. Įjungimas / išjungimas — Variables (be kodo keitimo)

Settings → Secrets and variables → Actions → **Variables**. Čia lengva toglinti,
kai testuoji ir nenori, kad kas sukiotųsi:

| Variable | Reikšmė | Ką daro |
|---|---|---|
| `WATCH_ENABLED` | `true` / (nieko) | circuit breaker pagal tvarkaraštį |
| `WATCH_FREEZE` | `true` / (nieko) | ar `watch` **sustabdo** įtartinas rinkas (reikia `ARBUS_WRITE_KEY`) |
| `CHECK_ENABLED` | `true` / (nieko) | resolution check pagal tvarkaraštį |
| `CHECK_DEEP` | `true` / (nieko) | ar leidžia brangią paiešką be-duomenų rinkoms |
| `CB_PRICE_MOVE` | pvz. `0.20` | circuit breaker riba (numatyta 0,20 = 20 %) |
| `CB_MIN_DISTINCT_USERS` | pvz. `3` | kiek skirtingų vartotojų reikia |
| `CB_WINDOW_MINUTES` | pvz. `10` | per kiek minučių |

**Testuojant:** ištrink (arba nustatyk ne `true`) `WATCH_ENABLED` / `CHECK_ENABLED`
— tvarkaraštis nustos veikti, o „Run workflow" mygtukas vis tiek veiks rankiniu
būdu. Įjungti atgal — nustatyk `true`.

## 3. Circuit breaker — nustatymai

Riba dabar **20 %** (`CB_PRICE_MOVE=0.20`): rinka pažymima įtartina, kai kaina
per langą pajuda ≥20 procentinių punktų **IR** tą pačia kryptimi stūmė ≥3
skirtingi vartotojai (kad vienas „banginis" neįjungtų aliarmo). `arbus watch`
kiekvieną kartą **atspausdina aktyvius nustatymus**, tad niekada nespėlioji:

```
⚙️  Circuit breaker: ≥20% kainos judesys IR ≥3 vartotojai per 10 min. | Auto-stabdymas: išjungtas (tik įspėja)
```

Keisti — per Variables (aukščiau) arba `.env` lokaliai.

## 4. Market generator — kaip paleisti

**Prijungtas prie Perplexity IR Anthropic** — raktai jau įvedami workflow'e;
provider'is pasirenkamas `.env`/secret'u:

- **su Anthropic** (dabartinis): nieko nekeisk.
- **su Perplexity**: nustatyk `LLM_PROVIDER_DRAFT=perplexity` (arba visam —
  `LLM_PROVIDER=perplexity`).

Paleidimas:
- **Lokaliai:** `python -m arbus generate --count 35`
  (peržiūra be LLM: `python -m arbus generate --dry-run`)
- **GitHub:** Actions → **generate-markets** → **Run workflow** → įrašyk kiekį.

## 5. Rankinės giluminės patikros (kai reikia)

Automatinis `check` praleidžia be-duomenų rinkas (kad būtų pigu). Kai nori, kad
AI **giliai** patikrintų konkrečią svarbią rinką:

```
python -m arbus check --match "Eurovizij" --deep
```

## Kaina ir dažnis — kiek galim be pinigų

Pats circuit breaker ir check **jokių LLM/API pinigų nekainuoja** watch'ui (nėra
LLM; Supabase skaitymai nemokami). Kainuoja tik **GitHub Actions minutės**, ir
tik jei repo **privatus** (nemokamai ~2000 min/mėn). Paleidimas lengvas
(`requirements.txt` – 5 bibliotekos), tad vienas paleidimas ~1 min.

| Repo | Kas 2 min | Kas 15 min | Kas 30 min | Kas 6 val. (check) |
|---|---|---|---|---|
| **Privatus** | ~€150/mėn ❌ | šiek tiek viršija | **nemokama ✅** | nemokama ✅ |
| **Viešas** | **nemokama ✅** | nemokama ✅ | nemokama ✅ | nemokama ✅ |

**Išvada:** privačiam repo laikykis **~kas 30 min** (numatyta) — nemokama. Jei
nori **kas 2 min** be pinigų — padaryk repo **viešą** (žr. žemiau) arba naudok
atskirą always-on procesą.

### Ar saugu padaryti repo viešą?

- **Actions tampa nemokamos ir neribotos** → kas 2 min OK.
- **Secrets LIEKA slapti** — GitHub jų niekada nerodo viešuose repo (užšifruoti,
  užmaskuoti loguose). API raktai saugūs.
- **Bet** visas kodas (logika, prompt'ai, rezoliucijos taisyklės) tampa viešai
  matomas. Prediction market'ui tai reiškia, kad vartotojai galėtų matyti, kaip
  botas sprendžia — apsvarstyk, ar tai OK. Slaptų raktų istorijoje nėra (`.env`
  git'e niekada nebuvo).

## Kaip testuoti (ir ar jau veikia)

**Taip, jau veikia** — kai pridėjai `ARBUS_API_KEY`, botas skaito gyvus duomenis
(patikrinta: aptinka realius kainų judesius). Kaip pačiam pasitikrinti:

1. **Rankiniu būdu iš karto** (nelaukiant tvarkaraščio): Actions → **circuit-breaker**
   → **Run workflow**. Atsidaryk log'ą — pamatysi „⚙️ Circuit breaker: ≥20%…" ir
   arba „No price movement", arba 🚨 aptiktas judesys.
2. **Kad pamatytum, kaip atrodo aptikimas:** laikinai Variables nustatyk jautrius
   parametrus (`CB_PRICE_MOVE=0.02`, `CB_MIN_DISTINCT_USERS=1`, `CB_WINDOW_MINUTES=1440`),
   paleisk rankiniu būdu — pamatysi 🚨. Po to grąžink į `0.20 / 3 / 10`.
3. **Lokaliai** (jei nori): `python -m arbus watch --no-telegram`.

## Circuit breaker parametrai — keisk per Variables (be kodo)

Settings → Secrets and variables → Actions → **Variables**:

| Variable | Ką reiškia | Numatyta |
|---|---|---|
| `CB_PRICE_MOVE` | kiek kaina turi pajudėti (0.20 = 20 %) | 0.20 |
| `CB_MIN_DISTINCT_USERS` | kiek skirtingų vartotojų ta pačia kryptimi | 3 |
| `CB_WINDOW_MINUTES` | per kiek minučių tai turi įvykti | 10 |

Pakeitei → įsigalioja nuo kito paleidimo. Dažnį (kas kiek min) keiti
`watch.yml` `cron` eilutėje (žr. komentarą ten).

## Santrauka — kas ką sprendžia

- **Circuit breaker** = automatinis **sargas**: pastebi įtartiną prekybą,
  (nebūtinai) sustabdo. Nesprendžia baigties.
- **Resolution check** = **patarėjas**: surenka įrodymus, pasiūlo baigtį.
  **Adminas patvirtina** dashboard'e. Botas niekada neišmoka pats.
