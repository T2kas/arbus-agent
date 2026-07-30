# Kaip tikrinti rinkas — tikslu ir pigu (sprendimų sistema)

Rezoliucijos patikra (`arbus check`) turi vieną tikslą: **ar rinkos rezultatas
jau žinomas, ir koks**. Testuojant paaiškėjo, kad svarbiausias dalykas yra ne
modelis, o **paieškos variklis** — ar jis randa lietuviškus šaltinius (LRT,
Delfi, eurovision.tv, LKL, UEFA).

## Pelningumo taisyklė (2026-07-30): default'as PIGUS, brangi paieška tik svarbioms

Prie ~200 rinkų ir ~15 freeze'ų per dieną paieška po ~0,15–0,30 € kiekvienai
rinkai daro botą nuostolingą — ir tos rinkos vis tiek dažniausiai grąžina „dar
neaišku" (ateities įvykis). Todėl `arbus check` dabar dirba taip:

| Rinka turi… | Ką daro | Kaina |
|---|---|---|
| **feed'o faktą** (akcijos/oras/degalai) | atsako iš fakto, 0 paieškų | ~0,01–0,02 € |
| **pridėtą šaltinį** (URL taisyklėse/šaltinyje) | pats parsisiunčia, 1 paieška | ~0,06 € |
| **nei vieno** | **PRALEIDŽIAMA** su „patikrink rankiniu" žinute, LLM nekviečiamas | **0 €** |
| nei vieno, bet svarbi → `--deep` | pilna web paieška | ~0,15–0,30 € |

Taigi pilnas 9 rinkų runas kainuoja **~0,06 €** (tik fact rinkos), ne ~1 €.
Svarbias rinkas atpigini **pridėdamas šaltinį kuriant rinką** — tada jos
tikrinamos automatiškai ir pigiai. Vienkartinei gilesnei patikrai:
`arbus check --match "…" --deep`.

## Ką parodė testai (2026-07-28…30)

| Sistema | Rado LT šaltinius? | Kaina/patikra | Rezultatas |
|---|---|---|---|
| **Anthropic web_search + Opus** | ✅ taip | ~0,20–0,30 € | Tikslus LRT straipsnis, rezultatas, data, net pastebėjo Vilnius/Kaunas painiavą |
| Anthropic web_search + Sonnet | ✅ taip | ~0,12–0,15 € | Ta pati paieška, kiek silpnesnis protavimas |
| **OpenAI GPT-5 + web search** | dalinai | **~1,00 €** ❗ | Euroviziją atsakė NETEISINGAI („nepateko"), brangu |
| Perplexity sonar / reasoning-pro | ❌ ne | ~0,05 € | „nerasta" net kai LRT turi atsakymą |

**Kaštų realybė (sąžiningai, 2026-07-30).** Ankstesnis „~0,05 €" Sonnet įvertis
buvo per mažas. Tikras kaštas Sonnet + 4 paieškos: **~0,12–0,15 €/rinka**, o su
rate-limit retry (2 pilnos eigos) — **iki ~0,25–0,30 €**. Pagrindinis kaštas NE
modelio protavimas, o **paieškų rezultatų tokenai**: kiekviena web_search įterpia
visą puslapį (~5–15k tokenų) kaip input'ą po $3/M. Botas KOL KAS nespausdina
kaštų per patikrą, todėl juos reikia vertinti, ne matuoti — tai būtų vertas
sekantis patobulinimas (Anthropic atsako `usage` laukus su input/output/search
skaičiais). Greitas 9 rinkų runas ≈ **~1,2–1,5 €**, jei nė viena nerate-limit'inama.

**Aiški išvada: Anthropic yra tiksliausias IR protingos kainos.** GPT-5 pasirodė
ir brangesnis, ir mažiau tikslus (Eurovizija). Perplexita nemato LT šaltinių.
Modelio keitimas (GPT-5.6 Terra ir pan.) šito neišspręs, nes problema yra
**paieška**, ne protavimas.

### Pigiausias variantas: duok botui šaltinį (2026-07-30)

Kadangi kaštas — tai **paieškų rezultatų tokenai** (kiekviena web_search įterpia
visą puslapį), didžiausia ekonomija yra **neieškoti**. Jei rinka turi nurodytą
šaltinį (jį pridedi kurdamas rinką), botas jį **pats parsisiunčia (nemokamas GET)**
ir įterpia tekstą kaip faktą — modelis skaito šaltinį tiesiogiai, o ne moka už
paiešką jo ieškodamas. Tada paieškų biudžetas nukrenta iki
`AICHECK_MAX_SEARCHES_WITH_SOURCE` (numatyta 1 — patvirtinamoji paieška; nustatyk
`0`, kai šaltiniu visiškai pasitiki).

| Scenarijus | Paieškos | ~input tokenai | ~kaina |
|---|---|---|---|
| Be šaltinio (ieško pats) | 4 | ~40k | ~0,12–0,15 € |
| **Su šaltiniu + 1 patvirtinimas** | 1 | ~13k | **~0,06 €** |
| Su šaltiniu, 0 paieškų (`=0`) | 0 | ~3k | **~0,02 €** |

Praktinė rekomendacija: **kurdami rinką pridėkite 1–2 tiksliausius šaltinius**
(kur bus skelbiama naujiena apie tą įvykį — LRT/Delfi straipsnis, UEFA/eurovision
puslapis, LEA biuletenis). URL'ai imami ir iš „šaltinio", ir iš „taisyklių" laukų.
Jei parsisiuntimas nepavyksta (paywall, 404) — botas tyliai grįžta prie paieškos,
tikslumas nenukenčia. Išjungti: `AICHECK_SOURCE_FETCH=off`.

**Kaštai dabar matomi.** Po kiekvienos patikros spausdinama eilutė:
`💶 kaina ~0,0X € (N paieškos, Xk in / Yk out)` — iš tikrų Anthropic `usage`
laukų. Apytikslė (kainos iš config), bet parodo „3 ar 15 centų" skirtumą, kurio
anksčiau nesimatė.

### Bet svarbiausia: nepriklausyk nuo modelio paieškos ten, kur nereikia

Akcijos ir oras **neturi** būti ieškomi modelio — jie ateina iš `resolvers.py`
(Yahoo, meteo.lt) kaip faktas. Jei patikra sako „neradau Ignitis kainos / Vilniaus
temperatūros" — tai reiškia, kad **feed'as nesuveikė**, ne modelis kaltas.
Patikrink tai tiesiogiai:

```
python -m arbus facts "Ar Ignitis grupė akcija pakils virš 23 Eur?"
python -m arbus facts "Kokia bus aukščiausia temperatūra Vilniuje liepos 25, 2026?"
```

- **✅** = kaina/temperatūra gauta ir bus paduota patikrai (modeliui nereikia ieškoti).
- **❌ NEPAVYKO — <klaida>** = feed'as neprieinamas iš tavo tinklo (pvz. Yahoo blokuoja
  IP). Tada matysi tikslią klaidą ir galėsim pataisyti šaltinį.

Tai atskiria „modelis blogai ieško" nuo „duomenų feed'as neveikia" — dažniausiai
tai antra, ir sprendžiama be jokio modelio.

## Modelis ≠ paieška

Opus ir Sonnet naudoja **tą patį** Anthropic `web_search`. Modelis tik protauja
(pvz. pastebi „Vilniaus vs Kauno Žalgiris" painiavą, ar dvikova dar nebaigta).
Todėl pigesnis modelis su ta pačia gera paieška randa tuos pačius šaltinius.

## Kainos (2026-07)

**Paieška (už 1 iškvietimą):**

| Variklis | Kaina | Randa LT? |
|---|---|---|
| OpenRouter + Parallel | $0.001 | ? (testuoti) |
| OpenRouter + Exa | $0.005 | ? (testuoti) |
| Anthropic web_search | $0.01 | ✅ patikrinta |
| OpenAI web_search | $0.01 + tokenai | tikėtina taip |

**Modelis (už 1M tokenų in/out):**

| Modelis | Kaina | Protavimas |
|---|---|---|
| DeepSeek R1 (OpenRouter) | ~$0.5 / $2 | geras, pigiausias |
| OpenAI o4-mini | $1.1 / $4.4 | geras |
| OpenAI GPT-5 | $1.25 / $10 | labai geras |
| Claude Sonnet 5 | $3 / $15 | labai geras |
| Claude Opus 5 | $5 / $25 | geriausias (patikrinta) |

## Rekomenduojama sistema (nuo pigiausio prie tiksliausio)

Botas dabar palaiko **5 tiekėjus** (Anthropic, OpenAI, OpenRouter, Perplexity,
Z.AI) ir kiekvieną gali pajungti tik rezoliucijai, per `.env`, be kodo keitimo.

### Greita atmintinė — kaip pajungti kiekvieną (tik patikrai)

| Tiekėjas | `.env` eilutės |
|---|---|
| **OpenAI** (savo raktas) | `OPENAI_API_KEY=sk-...`<br>`LLM_PROVIDER_AICHECK=openai`<br>`OPENAI_AICHECK_MODEL=gpt-5` (arba `gpt-5-mini`, `o4-mini`) |
| **OpenRouter** (vienas raktas, daug modelių) | `OPENROUTER_API_KEY=sk-or-...`<br>`LLM_PROVIDER_AICHECK=openrouter`<br>`OPENROUTER_AICHECK_MODEL=openai/gpt-5` |
| **Anthropic** (patikrinta) | `LLM_PROVIDER_AICHECK=anthropic`<br>`ANTHROPIC_AICHECK_MODEL=claude-sonnet-5` (arba `claude-opus-5`) |
| **Perplexity** | `LLM_PROVIDER_AICHECK=perplexity`<br>`PERPLEXITY_AICHECK_MODEL=sonar-reasoning-pro` |

Keiti tik tas eilutes, kitko neliesk. Po kiekvieno keitimo — `python -m arbus check --no-telegram`.

Testuok šia tvarka:

1. **Pigiausias, ką verta bandyti — GPT-5 + Exa (OpenRouter).**
   ```
   OPENROUTER_API_KEY=sk-or-...
   LLM_PROVIDER_AICHECK=openrouter
   OPENROUTER_AICHECK_MODEL=openai/gpt-5
   OPENROUTER_SEARCH_ENGINE=exa
   ```
   ~$0.02–0.05 už patikrą. Jei Exa randa LT šaltinius taip pat gerai kaip
   Anthropic — tai geriausias kainos/tikslumo santykis.

2. **Dar pigiau — DeepSeek R1 + Parallel:** `OPENROUTER_AICHECK_MODEL=deepseek/deepseek-r1`,
   `OPENROUTER_SEARCH_ENGINE=parallel`. ~$0.01. Tik jei R1 protavimo užtenka.

3. **Patikrintas etalonas — Sonnet + Anthropic paieška.**
   ```
   LLM_PROVIDER_AICHECK=anthropic
   ANTHROPIC_AICHECK_MODEL=claude-sonnet-5
   ```
   ~$0.05–0.08. Paieška tikrai randa LT šaltinius; Sonnet protavimo užtenka
   daugumai.

4. **Maksimalus tikslumas — Opus + Anthropic paieška.**
   `ANTHROPIC_AICHECK_MODEL=claude-opus-5`. ~$0.15. Sudėtingiems, dviprasmiškiems
   atvejams.

## Kaip testuoti tiksliai

Paimk 5 rinkas, kurių atsakymus žinai (pvz. Eurovizija 2026 — pateko; Žalgirio
dvikova — dar nebaigta). Kiekvienam variantui iš viršaus:

```
python -m arbus check --no-telegram
```

Žiūrėk į antraštę: **✅** (rado + nuoroda veikia), **❌** (nežino), **⚠️**
(haliucinacija). Geriausia sistema — ta, kuri tavo žinomoms rinkoms duoda **✅
teisingą baigtį su veikiančia nuoroda** ir **❌** toms, kurios tikrai dar
neįvyko. Pigiausias variantas, kuris tai daro patikimai, ir yra optimaliausias.

## Ką botas jau daro nepriklausomai nuo modelio

Šie deterministiniai sluoksniai veikia su bet kuriuo tiekėju ir mažina kaštus:

- **Faktų feed'ai** (`resolvers.py`): akcijų kaina (Yahoo), oras (meteo.lt),
  degalai (LEA) paduodami kaip faktas — modeliui nereikia jų ieškoti, tad
  užtenka mažiau paieškų.
- **Nuorodų tikrinimas**: kiekviena cituota nuoroda parsisiunčiama; 404 = pažymima
  kaip haliucinacija.
- **Aiški antraštė** ✅/❌/⚠️ ir „ne tie metai / ne tas žmogus" apsaugos prompte.

## Realiai patikrinta ir sutvarkyta (2026-07-29, gyvai su tikrais raktais)

Šie trys buvo tikri bug'ai, ne modelio kaltė — visi patvirtinti gyvu testavimu
prieš tikrą ena.lt/app API, ne spėjimu:

- **Degalų feed'as niekada neveikė.** `FUEL_LEA_URL` (ena.lt „įrankio" puslapis)
  yra Power BI iframe — jame apskritai nėra jokio „dyzelin"/„benzin" teksto,
  regex neturėjo ko rasti. Realus veikiantis šaltinis: ena.lt kasdien publikuoja
  paprasto teksto biuletenį kaip naujienos įrašą, bet **ne vienu nuspėjamu URL**
  — kartais `ndk-YYYYMMDD`, kartais aprašomasis slug'as, ir aprašomasis kartais
  būna VIENA DIENA ŠVIEŽESNIS už datinį (patikrinta gyvai: `ndk-20260727` turėjo
  pirmadienio kainą, kai jau buvo paskelbtas antradienio įrašas su kitu slug'u —
  ir būtent antradienio kaina peršoko rinkos ribą). Sprendimas: `sitemap.xml`
  turi `<lastmod>` kiekvienam URL — rikiuojam pagal datą ir bandome, kol vienas
  atsakymas atitinka biuletenio sakinio formą. Dabar visada randa tikrai
  naujausią įrašą, nepriklausomai nuo slug'o stiliaus.
- **Nukirstas atsakymas buvo skaitomas kaip „nežino".** Anthropic tyliai
  grąžina dalinį tekstą, kai `max_tokens` pasibaigia viduryje atsakymo (tik
  perspėjimas loge, ne klaida) — pastebėta gyvai ant sudėtingos paieškos rinkos.
  1200 tokenų buvo per mažai su išplėstiniu mąstymu ir keliomis paieškomis.
  Pakelta iki 4000 (`AICHECK_MAX_TOKENS`), ir atsakymas be `SIŪLOMA BAIGTIS`
  eilutės dabar aptinkamas kaip nukirstas ir bandomas iš naujo, o ne priimamas
  kaip apgalvotas „nežinau".
- **App'o tikimybės skalė — 0-100, ne 0-1.** Tikra rinka gyvai turėjo
  `probability: 55.0` / `45.0` (suma = 100). `arbus calibration` dėl to rodė
  „5500%", o circuit breaker'io 0,15 riba (15 procentinių punktų) būtų
  suveikusi beveik nuo bet kokio realaus kainos judesio, nes net 1 punkto
  judesys šioje skalėje jau yra „1.0", kas viršija 0.15. Pridėtas `_as_fraction()`
  normalizavimas: bet kuri reikšmė >1 laikoma 0-100 skale ir dalinama iš 100.

## Greitis — 5 min → 2 min vienai rinkai (2026-07-30, gyvai)

Eurovizijos patikra su Sonnet užtruko **5 min 20 s** — ir tai buvo švaistymas, ne
tikras darbas. Logas parodė: modelis norėjo daugiau paieškų nei leido biudžetas
(3), pasiekė **savo eilės (per-turn) paieškos limitą**, ir pasakė, kad negalėjo
baigti. `_research_with_search_retry` tai palaikė rate-limit'u: palaukė 20 s ir
**pakartojo visą paieškų seką iš naujo su tuo pačiu biudžetu 3** — dar ~2,5 min
už nieką.

Tas biudžeto sumažinimas 5→3 (dėl greičio/kainos) atsuko atgal: sukėlė limito
pataikymą, o pilnas retry yra LĖTESNIS nei viena 4 paieškų eiga. Pataisyta:

- **OPEN biudžetas 3→4** (`AICHECK_MAX_SEARCHES_OPEN`) — paieškai imli rinka
  baigia per vieną eigą.
- **Limito pataikymas ≠ rate-limit.** Kai modelis pasiekia savo eilės paieškos
  cap'ą (norėjo daugiau), retry vyksta **iš karto** (be 20 s) ir su **didesniu**
  biudžetu (+2, `AICHECK_SEARCH_CAP_BUMP`). 20 s backoff paliktas tik tikram
  rate-limit'ui.

Rezultatas gyvai: ta pati Eurovizijos rinka, tas pats teisingas ✅ atsakymas
(LRT + eurodiena.lt, 8 vieta, teisingi metai), bet **2 min 8 s** — vienas API
kvietimas, jokio retry. ~60 % greičiau ir pigiau (perpus mažiau paieškų).
