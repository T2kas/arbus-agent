# Telegram grupė komandai (žingsnis po žingsnio)

Kam to reikia: kai rinka užšaldoma (vartotojas praneša baigtį arba suveikia
circuit breaker), botas atsiunčia į **vieną grupę** santrauką — rinka, siūloma
baigtis, šaltinis, taisyklės ir AI patikros išvada su rekomendacija. Sprendimą
priima adminas dashboarde; žinutė tik atneša ją į telefoną iš karto.

Taip, grupėje gali būti keli žmonės — botas rašo į grupę, o ne asmeniškai.

## 1. Sukurk botą (jei dar neturi)

1. Telegrame atsidaryk [@BotFather](https://t.me/BotFather).
2. `/newbot` → pavadinimas → username (turi baigtis `bot`, pvz. `arbus_alerts_bot`).
3. BotFather atsiųs **tokeną** (ilga eilutė su `:`). Įrašyk į `.env`:

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
   ```

## 2. Sukurk grupę ir įdėk botą

1. Telegram → naujas **Group** (ne kanalas), pavadink pvz. „Arbus resolve".
2. Įdėk komandos narius.
3. Įdėk botą: grupės pavadinimas → *Add members* → įrašyk boto username.

## 3. Leisk botui matyti komandas

Botai grupėse pagal nutylėjimą **nemato** paprastų žinučių (privacy mode).
Pasirink vieną:

* **Paprasčiau:** padaryk botą grupės **adminu** (grupė → Administrators → Add
  admin → botas). Jokių papildomų teisių nereikia.
* **Arba:** @BotFather → `/setprivacy` → pasirink botą → **Disable**.

Vien pranešimams (alertams) to net nereikia — bot'ui užtenka būti grupėje.
Reikia tik jei norėsi grupėje rašyti komandas (`/markets`, `/feedback`).

## 4. Sužinok grupės ID

1. Paleisk botą: `python -m arbus bot`
2. Grupėje parašyk `/id`
3. Botas atsakys, pvz. `Chat id: -1001234567890`
   **Grupės ID visada su minusu** — nukopijuok su juo.
4. Įrašyk į `.env`:

   ```
   TELEGRAM_CHAT_ID=-1001234567890
   ```

5. Perkrauk botą (Ctrl+C ir vėl `python -m arbus bot`).

## 5. Patikrink

```sh
python -m arbus check          # AI patikra + pranešimas į grupę
```

Jei nieko neužšaldyta, parašys „Nothing frozen is waiting for a check." — tai
normalu. Kai bus reali užšaldyta rinka, žinutė atkeliaus į grupę.

Nori pamatyti tekstą nieko nesiunčiant:

```sh
python -m arbus check --no-telegram
```

## Dažnos klaidos

| Simptomas | Priežastis |
|---|---|
| Žinučių nėra, loge „telegram not configured" | `.env` be `TELEGRAM_BOT_TOKEN` arba `TELEGRAM_CHAT_ID` |
| `chat not found` | ID be minuso arba botas išmestas iš grupės |
| `/id` neatsako grupėje | Privacy mode įjungtas — žr. 3 žingsnį |
| Žinutė atėjo į asmeninį pokalbį, ne į grupę | `.env` liko senas (teigiamas) asmeninio pokalbio ID |

## Ką siunčia botas

```
🧊 RINKA UŽŠALDYTA — reikia admino sprendimo

#128 Ar nedarbas viršys 7 % iki spalio?
Variantai: Taip / Ne
Terminas: 2026-10-01

👤 Pranešė: user_412
Siūloma baigtis: Taip
Užstatas: 200 Arbukų
Šaltinis: https://osp.stat.gov.lt/...

Taisyklės (pagal jas sprendžiama):
Pagal Statistikos departamento duomenis. Jei duomenys nebus paskelbti — „Ne".

🤖 AI PATIKRA (patariamoji — AI nieko nesprendžia):
ŠALTINIS PATVIRTINA: taip
KĄ SAKO ŠALTINIS: nedarbas rugpjūtį siekė 7,2 %.
PIRMINIS ŠALTINIS: https://osp.stat.gov.lt/...
DATA TINKA: taip — duomenys už rugpjūtį
ĮSPĖJIMAI: nėra
SIŪLYMAS ADMINUI: patvirtinti

Sprendimą priima adminas dashboarde. Po paspaudimo išmokėjimas įvyksta ne iš
karto — lieka undo langas.
```
