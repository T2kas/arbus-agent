Convert the market-candidate draft below into the structured format exactly.

Rules while converting:
- Copy Lithuanian text faithfully; fix only obvious typos.
- Binary markets: options_lt must be exactly ["Taip", "Ne"].
- resolve_by must be ISO YYYY-MM-DD.
- probabilities align with options_lt order and sum to ~1.0.
- Keep every grounding URL mentioned for a candidate in its sources list.
- If a draft candidate is missing a required field and it cannot be inferred
  from the draft text, drop that candidate rather than inventing data.

DRAFT:
{draft}
