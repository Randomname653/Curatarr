"""
Curatarr — the collection DESIGNER: the curator invents rotating themed
shelves from the household's OWNED library.

Where Kometa builds collections from static rules, this asks the 27B to
design them: pick themes that fit the household taste ("Winter Noir",
"Absurd Action Nights"), select 6-12 owned titles each, rotate weekly.
Candidates come straight from MediaTechProfile (whole-library sweep incl.
unwatched, keys already attached) — the LLM only ever picks FROM the list,
and anything it invents is dropped at the mapping step.

Collections are household-global (see plex_collections.py), so the taste
input is the ADMIN's per-category summary.
"""
from __future__ import annotations

import json
import logging

from src.services.llm_utils import parse_llm_json, seasonal_context

logger = logging.getLogger(__name__)

THEMES_PER_CATEGORY = {"movie": 2, "show": 1, "anime": 1}
PLEX_TYPE = {"movie": 1, "show": 2, "anime": 2}
_CANDIDATE_CAP = 150
MIN_ITEMS = 4          # a shelf with fewer resolved titles is discarded
MAX_ITEMS = 12


def map_designs(themes: list, by_title: dict, by_norm: dict,
                section_key: str, plex_type: int) -> list:
    """Pure mapping step: LLM theme JSON -> pushable designs.

    Titles resolve exact-first then normalized; unknown titles are dropped
    silently (the LLM occasionally reformats punctuation or hallucinates),
    and a theme that keeps fewer than MIN_ITEMS resolved keys is discarded.
    """
    from src.services.library_memory import normalize_title
    from src.services.plex_collections import COLLECTION_PREFIX

    designs = []
    for t in themes or []:
        name = (t.get("title") or "").strip()
        items = t.get("items") or []
        if not name or not isinstance(items, list):
            continue
        keys, seen = [], set()
        for title in items[:MAX_ITEMS * 2]:
            item = by_title.get(title) or by_norm.get(normalize_title(str(title)))
            key = item and str(item.get("plex_rating_key") or "")
            if key and key not in seen:
                seen.add(key)
                keys.append(key)
        if len(keys) < MIN_ITEMS:
            logger.info("[collections] theme %r dropped — only %d/%d titles "
                        "resolved", name, len(keys), len(items))
            continue
        designs.append({
            "section_key": str(section_key),
            "plex_type": plex_type,
            "title": f"{COLLECTION_PREFIX}{name}"[:120],
            "keys": keys[:MAX_ITEMS],
            "description": (t.get("description") or "")[:300],
        })
    return designs


def _design_prompt(cat: str, n: int, taste: str, titles: list,
                   prev_names: list) -> str:
    prev = ", ".join(prev_names) if prev_names else "—"
    return f"""[MODE: COLLECTION DESIGNER]
You design {n} themed collection shelves from the household's OWNED {cat} library for their media server.
HOUSEHOLD TASTE: {taste or "broad, adventurous"}
{seasonal_context()}
CANDIDATES (owned — pick ONLY from this list, exact spelling): {"; ".join(titles)}
RULES: exactly {n} theme(s); {MIN_ITEMS + 2}-{MAX_ITEMS} titles each; shelf names short and evocative (2-4 words, English); a one-sentence description each; do NOT reuse last rotation's shelf names: {prev}.
JSON ONLY: [{{"title": "Winter Noir", "description": "one sentence", "items": ["Exact Title", "..."]}}]"""


async def design_collections() -> list:
    """One design pass over all video categories → pushable design list."""
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig, MediaTechProfile, TasteVectorEntry, User
    from src.services.app_state import get_state
    from src.services.recommendations_engine import _call_llm, _taste_section

    with get_db_session() as db:
        sections = {lc.media_category: lc.plex_section_key
                    for lc in db.query(LibraryConfig).all()}
        admin = db.query(User).filter(User.is_admin == True).first()  # noqa: E712
        tv = admin and db.query(TasteVectorEntry).filter(
            TasteVectorEntry.user_id == admin.id).first()
        summary_text = (tv.summary_text or "") if tv else ""
        cands = {}
        for cat in ("movie", "show", "anime"):
            rows = (db.query(MediaTechProfile.title, MediaTechProfile.plex_rating_key)
                    .filter(MediaTechProfile.media_type == cat,
                            MediaTechProfile.plex_rating_key.isnot(None))
                    .order_by(MediaTechProfile.id.desc())
                    .limit(_CANDIDATE_CAP).all())
            cands[cat] = [{"title": t, "plex_rating_key": k} for t, k in rows if t]

    from src.services.plex_collections import COLLECTION_PREFIX
    try:
        prev = json.loads(get_state("curatarr_collections") or "{}")
        prev_names = [d.get("title", "").removeprefix(COLLECTION_PREFIX)
                      for d in prev.get("designs", [])]
    except Exception:
        prev_names = []

    from src.services.library_memory import normalize_title
    designs = []
    for cat, n in THEMES_PER_CATEGORY.items():
        section = sections.get(cat)
        pool = cands.get(cat) or []
        if not section or len(pool) < MIN_ITEMS * 2:
            logger.info("[collections] %s skipped (section=%s, pool=%d)",
                        cat, section, len(pool))
            continue
        taste = _taste_section(summary_text, cat)
        prompt = _design_prompt(cat, n, taste, [c["title"] for c in pool],
                                prev_names)
        raw = await _call_llm(prompt, max_tokens=900)
        if not raw:
            logger.warning("[collections] designer LLM returned nothing for %s", cat)
            continue
        try:
            themes = parse_llm_json(raw)
        except Exception as e:
            logger.warning("[collections] designer JSON parse failed for %s: %s",
                           cat, e)
            continue
        by_title = {c["title"]: c for c in pool}
        by_norm = {normalize_title(c["title"]): c for c in pool}
        designs.extend(map_designs(themes if isinstance(themes, list) else [],
                                   by_title, by_norm, section, PLEX_TYPE[cat]))
    return designs
