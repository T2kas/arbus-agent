"""Turn a generated candidate into an app-ready market spec, on demand.

The generator produces many quick-mode candidates (one line + rough odds). When
a human picks one to launch, THIS module does the second, deliberate step: a
single web-grounded LLM call that re-checks the event is still live and writes
the full Lithuanian rules, a short context, clean options + probabilities, and a
trading-close time — everything `app_api.create_market` needs. The call is
metered so the Telegram flow can report its EUR cost.

Pure assembly (options normalisation, category mapping, the closes_at fallback)
is split from the one network call so it stays testable offline.
"""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field

from . import app as app_api, config, llm
from .series import _pct_ints

log = logging.getLogger(__name__)

# The app's fixed Lithuanian categories (config.CATEGORIES keys) + a catch-all.
APP_CATEGORIES = list(config.CATEGORIES.keys()) + [config.DEFAULT_CATEGORY]

# English candidate slugs → the app's Lithuanian category, used as the fallback
# when the model does not return one of the fixed values.
_CATEGORY_MAP = {
    "sports": "sportas", "politics": "politika", "culture": "kultura",
    "influencers": "influenceriai", "economics": "ekonomika", "economy": "ekonomika",
    "business": "verslas", "weather": "orai", "geopolitics": "geopolitika",
    "tech": "verslas", "society": "kultura",
}


class ComposedOption(BaseModel):
    label: str = Field(description="Outcome label in Lithuanian")
    probability: float = Field(description="Starting probability 0-100")


class ComposedMarket(BaseModel):
    title: str = Field(description="Market question in Lithuanian (the headline)")
    subtitle: str = Field(description="Short subtitle, e.g. a date or scope; may be empty")
    category: str = Field(description=f"One of: {', '.join(APP_CATEGORIES)}")
    rules: str = Field(description="Full Lithuanian resolution rules: exact trigger, "
                                   "definitions, edge cases (postpone/cancel/tie), the "
                                   "official source + a backup, timezone")
    context: str = Field(description="1-3 short Lithuanian sentences of context")
    options: list[ComposedOption] = Field(description="Outcomes with starting %")
    closes_at: str = Field(description="ISO 8601 datetime trading should close, "
                                       "Europe/Vilnius, before the outcome is knowable")
    still_open: bool = Field(description="False if the event already happened / is "
                                         "decided / was cancelled — do not launch it")


def _tz() -> ZoneInfo:
    return ZoneInfo(config.WEATHER_TZ)


def candidate_summary(row) -> dict:
    """The fields of a stored candidate the compose call needs, name-tolerant to
    both a sqlite3.Row and a plain dict."""
    def g(key, default=""):
        try:
            v = row[key]
        except (KeyError, IndexError, TypeError):
            v = None
        return v if v not in (None, "") else default
    try:
        options = json.loads(g("options_json", "[]"))
    except (ValueError, TypeError):
        options = []
    try:
        probs = json.loads(g("probabilities_json", "[]"))
    except (ValueError, TypeError):
        probs = []
    try:
        sources = json.loads(g("sources_json", "[]"))
    except (ValueError, TypeError):
        sources = []
    return {
        "question": g("question_lt"),
        "options": options,
        "probabilities": probs,
        "category": g("category"),
        "resolve_by": str(g("resolve_by"))[:10],
        "resolution_hint": g("resolution_hint_lt"),
        "sources": [s for s in sources if isinstance(s, str)],
        "image_url": g("image_url"),
        "image_source": g("image_source"),
    }


def summary_from_candidate(cand) -> dict:
    """The same summary dict as `candidate_summary`, but from an in-memory
    Candidate object — so a batch can persist its ideas without the SQLite DB
    (which CI does not keep between runs)."""
    return {
        "question": getattr(cand, "question_lt", ""),
        "options": list(getattr(cand, "options_lt", []) or []),
        "probabilities": list(getattr(cand, "probabilities", []) or []),
        "category": getattr(cand, "category", ""),
        "resolve_by": str(getattr(cand, "resolve_by", ""))[:10],
        "resolution_hint": getattr(cand, "resolution_hint_lt", ""),
        "sources": [s for s in (getattr(cand, "sources", []) or []) if isinstance(s, str)],
        "image_url": getattr(cand, "image_url", ""),
        "image_source": getattr(cand, "image_source", ""),
    }


def _normalise_category(value: str, candidate_category: str) -> str:
    v = (value or "").strip().lower()
    if v in APP_CATEGORIES:
        return v
    return _CATEGORY_MAP.get((candidate_category or "").strip().lower(),
                             config.DEFAULT_CATEGORY)


def _fallback_closes_at(iso: str, resolve_by: str, tz: ZoneInfo) -> str:
    """Validate the model's closes_at; fall back to resolve_by 23:45 Vilnius (the
    generator's default for an open-ended market) when it is missing/unparseable."""
    if iso:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.isoformat()
        except ValueError:
            pass
    try:
        d = date.fromisoformat(resolve_by[:10])
    except (ValueError, TypeError):
        d = date.today() + timedelta(days=7)
    return datetime.combine(d, dtime(23, 45), tzinfo=tz).isoformat()


def build_spec(composed: ComposedMarket, cand: dict, tz: ZoneInfo | None = None,
               *, liquidity: int, image_url: str) -> dict:
    """Assemble the app_api.create_market payload from the model's ComposedMarket
    plus the human-supplied liquidity/image. Pure — no network."""
    tz = tz or _tz()
    opts = [(o.label.strip(), max(float(o.probability), 0.5))
            for o in composed.options if o.label and o.label.strip()]
    if not opts:                                        # model returned none — reuse candidate's
        cp = cand.get("probabilities") or []
        opts = [(str(lbl), float(cp[i]) * 100 if i < len(cp) else 1.0)
                for i, lbl in enumerate(cand.get("options") or [])]
    pcts = _pct_ints([w for _, w in opts])
    return {
        "title": composed.title.strip() or cand.get("question", ""),
        "subtitle": (composed.subtitle or "").strip() or None,
        "category": _normalise_category(composed.category, cand.get("category", "")),
        "image_url": image_url or "",
        "liquidity": int(liquidity),
        "rules": (composed.rules or "").strip(),
        "context": (composed.context or "").strip(),
        "options": [{"label": label, "probability": p}
                    for (label, _), p in zip(opts, pcts)],
        "closes_at": _fallback_closes_at(composed.closes_at,
                                         cand.get("resolve_by", ""), tz),
    }


_SYSTEM = ("Tu esi „Arbus“ prognozių rinkų taisyklių redaktorius. Rašyk aiškia "
           "lietuvių kalba, naudok lietuviškas kabutes, venk azartinių lošimų "
           "terminijos. Remkis tik realiais, patikrinamais faktais — nekurk "
           "neegzistuojančių datų, dalyvių ar šaltinių.")


def _prompt(cand: dict, today: str) -> str:
    return (
        f"Šiandien {today}. Ši „Arbus“ rinkos idėja atrinkta ĮKĖLIMUI — paruošk "
        "ją galutiniam pateikimui programėlėje.\n\n"
        "Per interneto paiešką: (1) įsitikink, kad įvykis dar NEĮVYKO ir rezultatas "
        "nežinomas; (2) patvirtink oficialų rezultato šaltinį ir surask nepriklausomą "
        "atsarginį; (3) patikslink tikimybes pagal naujausią informaciją.\n\n"
        "Grąžink: title (klausimas LT), trumpą subtitle (data/apimtis arba tuščią), "
        f"category (vieną iš: {', '.join(APP_CATEGORIES)}), pilnas rules "
        "(tikslus sprendimo momentas, sąvokų apibrėžimai, kraštutiniai atvejai — "
        "atšaukimas/nukėlimas/lygiosios/šaltinis tyli, laiko juosta = Lietuvos, "
        "oficialus + atsarginis šaltinis), context (1–3 sakiniai), options su "
        "starto tikimybėmis (sveiki procentai, iš viso 100; dvinarei rinkai "
        "„Taip“/„Ne“), closes_at (ISO, Lietuvos laiku, PRIEŠ tampant rezultatui "
        "žinomam), ir still_open (false jei įvykis jau įvyko/atšauktas).\n\n"
        "IDĖJA:\n" + json.dumps(cand, ensure_ascii=False, indent=2)
    )


def compose(cand: dict, today: str | None = None) -> tuple[ComposedMarket, dict]:
    """Run the metered web-grounded compose call. Returns (ComposedMarket, meta)
    where meta carries provider/model/cost_eur. Raises on a hard LLM failure."""
    today = today or date.today().isoformat()
    llm.reset_usage()
    text = llm.research(_prompt(cand, today), system=_SYSTEM,
                        max_uses=config.COMPOSE_SEARCHES,
                        max_tokens=config.COMPOSE_MAX_TOKENS, stage="draft")
    composed = llm.structure(text, ComposedMarket)
    snap = llm.usage_snapshot()
    prov = llm._USAGE_PROVIDER or ""
    meta = {"provider": prov, "model": llm.model_for(prov) if prov else "",
            "cost_eur": llm.usage_cost_eur(snap, prov) if any(snap.values()) else 0.0}
    return composed, meta
