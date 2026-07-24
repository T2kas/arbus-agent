The market candidates below were REJECTED because their headline wording
violates the clarity rules (the rejection reason is included with each).
The underlying market ideas are good — fix the wording, keep the idea.

Rewrite each candidate's question_lt to be SHORT, CLEAN and instantly readable.
The headline carries the idea; ALL rigor lives in the rules.

STRIP OUT of the question:
- Data-source attribution: "pagal Lietuvos hidrometeorologijos tarnybos
  duomenis", "pagal LAMA BPO duomenis", "pagal „pricer.lt" duomenis" → delete
  it from the headline, keep it in resolution_hint_lt.
- Day-precision dates: "iki 2026 m. rugpjūčio 15 d.", "tarp rugpjūčio 1–31 d.",
  "2026-08-15" → delete, or compress to a month/event ("rugpjūčio mėnesį").
  The resolve_by field already carries the exact deadline.
- Parentheses and every parenthetical caveat → move to resolution_hint_lt.
- The word "viešai" and other rules-only qualifiers → move to the rules.
- Redundant or non-defining qualifiers: "Vengrijos „Grand Prix" etapą,
  vyksiantį Vengrijoje" → "Vengrijos „Grand Prix"". Say each thing once.
- Vague filler: "panašaus", "pvz.", "ir pan.", "artimiausiu metu".
- Unmeasurable descriptors ("emocinga reakcija", "kardinaliai pakeistą
  įvaizdį", "pagrindiniu turinio akcentu") → replace with a concrete,
  countable act, or restate the market around a checkable event.

KEEP in the question: the exact subject (name the person, team, song title),
the exact threshold or number, and nothing else.

Worked example — this bloated headline:
  "Ar Vilniuje bent vieną dieną tarp 2026 m. rugpjūčio 1–31 d. oficialiai bus
   užfiksuotas ≥30 mm paros kritulių kiekis pagal Lietuvos hidrometeorologijos
   tarnybos duomenis?"
becomes:
  "Ar Vilniuje rugpjūčio mėnesį bus užfiksuotas ≥30 mm paros kritulių kiekis?"
with the source, the "bent vieną dieną" definition and the exact window moved
into resolution_hint_lt.

A headline may be a clean title rather than a question ("Naujas Palangos
meras") when that is shorter and clearer.

Update resolution_hint_lt so it now carries everything you removed from the
headline — the source, the exact date window, and the edge cases. Keep every
other field unchanged. Return ALL candidates, corrected.

CANDIDATES (with rejection reasons):
{items}
