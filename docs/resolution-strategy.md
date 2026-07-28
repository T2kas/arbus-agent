# Kaip tikrinti rinkas — tikslu ir pigu (sprendimų sistema)

Rezoliucijos patikra (`arbus check`) turi vieną tikslą: **ar rinkos rezultatas
jau žinomas, ir koks**. Testuojant paaiškėjo, kad svarbiausias dalykas yra ne
modelis, o **paieškos variklis** — ar jis randa lietuviškus šaltinius (LRT,
Delfi, eurovision.tv, LKL, UEFA).

## Ką parodė testai (2026-07-28)

| Sistema | Rado LT šaltinius? | Rezultatas |
|---|---|---|
| **Anthropic web_search + Opus/Sonnet** | ✅ taip | Rado tikslų LRT straipsnį, rezultatą, datą |
| Perplexity sonar / sonar-reasoning-pro | ❌ ne | „ŠALTINIS: nerasta" net kai LRT turi atsakymą |

Išvada: **Perplexity paieška nemato lietuviškų šaltinių** — netinka rezoliucijai.
Anthropic paieška (lokalizuota į Vilnių) juos randa.

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

Botas dabar palaiko **4 tiekėjus** ir kiekvieną gali pajungti tik rezoliucijai,
per `.env`, be kodo keitimo. Testuok šia tvarka:

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
