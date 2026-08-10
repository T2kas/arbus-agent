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

## Santrauka — kas ką sprendžia

- **Circuit breaker** = automatinis **sargas**: pastebi įtartiną prekybą,
  (nebūtinai) sustabdo. Nesprendžia baigties.
- **Resolution check** = **patarėjas**: surenka įrodymus, pasiūlo baigtį.
  **Adminas patvirtina** dashboard'e. Botas niekada neišmoka pats.
