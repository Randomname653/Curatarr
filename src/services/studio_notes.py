"""
Curatarr — Studio notes: what a studio is KNOWN FOR, as evidence.

The Princess Lover! debate flipped on outside knowledge: "early GoHands,
chaotic experimental visual language". The curator knew the studio only as a
name — it could neither verify nor counter the claim, so it folded
instantly. A studio's REPUTATION is title-independent knowledge that belongs
in the evidence file of every title it produced.

Prototype (tests/studio_proto.py, 20 real library studios): 20/20 Wikipedia
articles found (3 homonym mis-hits -> the title-match guard below), and the
8B summarizer distilled curator-grade notes — GoHands: "known for its
distinctive animation style ... K, Hand Shakers ... bold character designs
and dynamic action sequences".

One note per STUDIO (~400 distinct across 2.3k anime), cached ~forever
(reputation moves slowly), fetched JIT for discussed titles and back-filled
piggyback by the reception walker.
"""
from __future__ import annotations

import logging
import re
from typing import Optional

import httpx

from src.config import settings
from src.services.llm_utils import (clean_llm_text, ollama_options,
                                    strip_think_tags, SUMMARIZER_KEEP_ALIVE)

logger = logging.getLogger(__name__)

WIKI_API = "https://en.wikipedia.org/w/api.php"
WIKI_HEADERS = {"User-Agent": "Curatarr/1.0 (https://github.com/Randomname653/curatarr; "
                              "personal media curator) python-httpx"}
_NOTE_CACHE_DAYS = 3650   # reputation moves slowly; NONE results cache too

_CONDENSE_SYS = (
    "You write a one-line STUDIO NOTE for a media curator's evidence file. "
    "Use ONLY the text given. State what the studio is KNOWN FOR — its visual "
    "style, reputation, signature works. Skip founding dates, corporate "
    "history and ownership. 2 sentences maximum, plain prose. If the text "
    "documents no style or reputation, output exactly: NONE")

_DIRECTOR_SYS = (
    "You write a one-line DIRECTOR NOTE for a media curator's evidence file. "
    "Use ONLY the text given. State what the director is KNOWN FOR — style, "
    "reputation, signature works. Skip birth dates, family and biography. "
    "2 sentences maximum, plain prose. If the text documents no style or "
    "reputation, output exactly: NONE")

# a person hit must actually be a film person, not a same-named homonym
_PERSON_HINT = re.compile(r"director|filmmaker|screenwriter|animator|producer", re.I)

# Placeholder values must never be treated as names: Director "Unknown"
# searched Wikipedia, matched the FILM "A Complete Unknown" (substring!),
# and granite condensed it into a James Mangold note that got cached for
# ten years under director_note:unknown.
_NAME_SENTINELS = {"unknown", "n a", "na", "none", "various", "tba", "tbd",
                   "unbekannt", "misc", "not available", "no director"}


def _norm(s: str) -> str:
    s = (s or "").lower()
    # Fold romanisation variants before comparing: credits arrive
    # wapuro-style ("shinbou", "gorou") while Wikipedia files modified
    # Hepburn ("Shinbo", "Goro" with a macron the ascii-fold below strips) —
    # the exact-name guard permanently rejected the very people it was built
    # to find. Macrons first, then long vowels collapse.
    s = s.translate(str.maketrans("āēīōū", "aeiou"))
    s = re.sub(r"[^a-z0-9]+", " ", s).strip()
    return re.sub(r"(ou|oo|uu)", lambda m: m.group(1)[0], s)


def _cache_key(studio: str) -> str:
    return f"studio_note:{_norm(studio)}"


def get_studio_note_cached(studio: str, cache) -> Optional[str]:
    """Sync cache read for build_verified_data (no fetching)."""
    try:
        hit = cache.get_cache(_cache_key(studio))
        if hit and isinstance(hit.get("response"), dict):
            return hit["response"].get("note")
    except Exception:
        pass
    return None


async def _wiki_studio_lead(studio: str) -> str:
    """Lead extract of the studio's article, homonym-guarded: the hit's title
    must contain the studio name (proto: 'A-1 Pictures' -> 'Sony Pictures
    Animation' and 'Felix Film' -> 'Felix the Cat' were exactly this trap)."""
    want = _norm(studio)
    answered = False
    async with httpx.AsyncClient(timeout=20, headers=WIKI_HEADERS) as client:
        for query in (studio, f"{studio} animation studio"):
            try:
                sr = await client.get(WIKI_API, params={
                    "action": "query", "list": "search", "format": "json",
                    "srsearch": query, "srlimit": 5})
                hits = (sr.json().get("query", {}).get("search", [])
                        if sr.status_code == 200 else [])
                answered = answered or sr.status_code == 200
            except Exception:
                hits = []
            for h in hits[:4]:
                t = h.get("title") or ""
                if want not in _norm(t):
                    continue
                try:
                    ex = await client.get(WIKI_API, params={
                        "action": "query", "prop": "extracts", "format": "json",
                        "titles": t, "exintro": 1, "explaintext": 1, "redirects": 1})
                    pages = (ex.json().get("query") or {}).get("pages") or {}
                    extract = next(iter(pages.values()), {}).get("extract") or ""
                except Exception:
                    continue
                low = extract.lower()
                if ("studio" in low or "animation" in low) and len(extract) > 200:
                    return extract
    return "" if answered else None    # silence is not "no article"


async def _condense(studio: str, extract: str,
                    sys_prompt: str = None, label: str = "STUDIO") -> Optional[str]:
    for model in (settings.SUMMARIZER_MODEL, settings.BASE_SUMMARIZER_MODEL):
        if not model:
            continue
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                r = await client.post(f"{settings.effective_ollama}/api/chat", json={
                    "model": model,
                    "messages": [{"role": "system", "content": sys_prompt or _CONDENSE_SYS},
                                 {"role": "user",
                                  "content": f"{label}: {studio}\n\nTEXT:\n{extract[:2400]}"}],
                    "stream": False,
                    "keep_alive": SUMMARIZER_KEEP_ALIVE,
                    **ollama_options(temperature=0.1, num_predict=200),
                })
            if r.status_code != 200:
                continue
            out = clean_llm_text(strip_think_tags(
                r.json().get("message", {}).get("content", "") or "")).strip()
            out = re.sub(r"\s{2,}", " ", out.replace("**", "")).strip()
            if not out or out.upper().startswith("NONE") or len(out) < 25:
                return ""      # definitive — the model read it and said NONE
            return out
        except Exception as e:
            logger.debug("[studio-note] condense via %s failed: %s", model, e)
    return None                # transient — no model answered


def get_director_note_cached(name: str, cache) -> Optional[str]:
    """Sync cache read for build_verified_data (no fetching)."""
    try:
        hit = cache.get_cache(f"director_note:{_norm(name)}")
        if hit and isinstance(hit.get("response"), dict):
            return hit["response"].get("note")
    except Exception:
        pass
    return None


async def _wiki_person_lead(name: str) -> str:
    """Lead extract of a film person's article, homonym-guarded (title must
    contain the name, lead must read like a film person)."""
    want = _norm(name)
    answered = False
    async with httpx.AsyncClient(timeout=20, headers=WIKI_HEADERS) as client:
        for query in (name, f"{name} film director"):
            try:
                sr = await client.get(WIKI_API, params={
                    "action": "query", "list": "search", "format": "json",
                    "srsearch": query, "srlimit": 5})
                hits = (sr.json().get("query", {}).get("search", [])
                        if sr.status_code == 200 else [])
                answered = answered or sr.status_code == 200
            except Exception:
                hits = []
            for h in hits[:4]:
                t = h.get("title") or ""
                tn = _norm(t)
                # PERSON articles must BE the name (plus optional
                # disambiguator) — a contains-check let "A Complete
                # Unknown" pass for director "Unknown".
                if tn != want and not tn.startswith(want + " "):
                    continue
                try:
                    ex = await client.get(WIKI_API, params={
                        "action": "query", "prop": "extracts", "format": "json",
                        "titles": t, "exintro": 1, "explaintext": 1, "redirects": 1})
                    pages = (ex.json().get("query") or {}).get("pages") or {}
                    extract = next(iter(pages.values()), {}).get("extract") or ""
                except Exception:
                    continue
                if _PERSON_HINT.search(extract) and len(extract) > 200:
                    return extract
    return "" if answered else None    # silence is not "no article"


async def ensure_director_note(name: str, cache=None) -> Optional[str]:
    """Director counterpart of ensure_studio_note — JIT-ONLY by design:
    4,303 distinct directors across the library make a bulk walker too
    expensive, but debated/judged titles collect the relevant names fast
    (proto: 12/16 coverage incl. long-tail, NONE results cache forever)."""
    name = (name or "").split(",")[0].strip()
    if not name or _norm(name) in _NAME_SENTINELS or not _norm(name):
        return None
    from src.cache.metadata_cache import MetadataCache
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        key = f"director_note:{_norm(name)}"
        hit = cache.get_cache(key)
        if hit and isinstance(hit.get("response"), dict):
            return hit["response"].get("note")
        extract = await _wiki_person_lead(name)
        if extract is None:
            return None        # transient — Wikipedia never answered; no stamp
        note = None
        if extract:
            out = await _condense(name, extract, sys_prompt=_DIRECTOR_SYS,
                                  label="DIRECTOR")
            if out is None:
                return None    # transient — no model answered; no stamp
            note = out or None
        cache.set_cache(key, {"checked": True, "note": note}, days=_NOTE_CACHE_DAYS)
        if note:
            logger.info("[director-note] %s: %s", name, note[:100])
        return note
    except Exception as e:
        logger.debug("[director-note] failed for %r: %s", name, e)
        return None
    finally:
        if owns:
            cache.close()


async def ensure_studio_note(studio: str, cache=None) -> Optional[str]:
    """Fetch-and-cache the note for one studio (idempotent — a checked
    marker is written even when Wikipedia has nothing, so each studio costs
    at most one Wikipedia + summarizer round ever)."""
    if not studio or _norm(studio) in _NAME_SENTINELS or not _norm(studio):
        return None
    from src.cache.metadata_cache import MetadataCache
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        hit = cache.get_cache(_cache_key(studio))
        if hit and isinstance(hit.get("response"), dict):
            return hit["response"].get("note")
        extract = await _wiki_studio_lead(studio)
        if extract is None:
            return None        # transient — Wikipedia never answered; no stamp
        note = None
        if extract:
            out = await _condense(studio, extract)
            if out is None:
                return None    # transient — no model answered; no stamp
            note = out or None
        cache.set_cache(_cache_key(studio), {"checked": True, "note": note},
                        days=_NOTE_CACHE_DAYS)
        if note:
            logger.info("[studio-note] %s: %s", studio, note[:100])
        return note
    except Exception as e:
        logger.debug("[studio-note] failed for %r: %s", studio, e)
        return None
    finally:
        if owns:
            cache.close()
