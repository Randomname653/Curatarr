"""Wikidata facts for the archive pillar — structured, no model involved.

Wikipedia and Wikidata are not alternatives. Wikipedia is prose, so reading
it means distilling it, and a distillation is only ever as good as the model
and the prompt behind it. Wikidata is a graph of statements: "based on" points
at a work, that work has an "author", the series has "award received". There
is nothing to summarise and nothing to get wrong.

That matters for two facts this app kept getting wrong:

* the SOURCE of an adaptation. TMDB carries it as a crew credit, which is now
  read, but coverage is uneven; Wikidata is a second, independent answer.
  Measured live: Apocalypse Now -> Joseph Conrad, The Godfather -> Mario Puzo,
  The Night Manager -> John le Carré, Breaking Bad -> nothing, correctly.
* NAMED awards. OMDb says "27 wins & 52 nominations"; Wikidata says "Palme
  d'Or", "Academy Award for Best Cinematography". For a pillar that has to
  weigh objective stature, the difference is the whole point.

Coverage is uneven and that is fine — this is an additive source. Films are
better covered than series (Night Manager returns an author and no awards),
so it supplements OMDb rather than replacing it.

No API key, no account, one HTTP call. The endpoint asks for a descriptive
User-Agent and we send one.
"""
import asyncio
import hashlib
import json
import logging
import urllib.parse
from typing import Optional

import httpx

from src.cache.metadata_cache import MetadataCache, write_fields

logger = logging.getLogger(__name__)

SPARQL_URL = "https://query.wikidata.org/sparql"
USER_AGENT = "Curatarr/1.0 (https://github.com/Randomname653/Curatarr)"

# Properties: P345 IMDb id, P144 based on, P50 author, P166 award received,
# P1476 title. Fetched in ONE query per title; the OPTIONAL blocks mean a
# title with no adaptation and no awards still returns its label.
_QUERY = """
SELECT ?itemLabel ?basedOnLabel ?authorLabel ?awardLabel WHERE {
  ?item wdt:P345 "%s" .
  OPTIONAL { ?item wdt:P144 ?basedOn . OPTIONAL { ?basedOn wdt:P50 ?author . } }
  OPTIONAL { ?item wdt:P166 ?award . }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en" . }
} LIMIT 60
"""

# Same contract as the significance distillation: the stored answer records
# which version of THIS logic produced it, so changing what we ask for retires
# the old answers instead of freezing them (see media_enricher's
# _SIG_PROMPT_VERSION for why that lesson was learned the hard way).
_LOGIC_VERSION = hashlib.sha1(_QUERY.encode("utf-8")).hexdigest()[:8]

# Awards worth naming to a judge weighing objective stature. Wikidata lists
# everything from the Palme d'Or to local festival prizes; an unfiltered dump
# would drown the signal it exists to provide.
_MAJOR_AWARDS = (
    "academy award", "palme d'or", "golden globe", "bafta", "primetime emmy",
    "emmy award", "cannes", "golden bear", "golden lion", "grammy",
    "peabody", "hugo award", "nebula award", "césar", "goya",
    "critics' choice", "screen actors guild", "directors guild",
    "writers guild", "national board of review", "sundance",
)


def _is_major(award: str) -> bool:
    a = (award or "").lower()
    return any(marker in a for marker in _MAJOR_AWARDS)


async def fetch_wikidata_facts(imdb_id: str, *, timeout: float = 25.0) -> Optional[dict]:
    """Structured facts for one IMDb id, or None on a transient failure.

    Returns ``{}`` when the query succeeds but Wikidata knows nothing — a
    definitive answer, distinct from "the endpoint was unreachable", so the
    caller can stamp one and retry the other.
    """
    if not imdb_id or not str(imdb_id).startswith("tt"):
        return {}
    url = f"{SPARQL_URL}?format=json&query={urllib.parse.quote(_QUERY % imdb_id)}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(url, headers={"User-Agent": USER_AGENT,
                                               "Accept": "application/json"})
        if r.status_code != 200:
            logger.debug("[wikidata] HTTP %s for %s", r.status_code, imdb_id)
            return None
        rows = r.json().get("results", {}).get("bindings", [])
    except Exception as e:
        logger.debug("[wikidata] query failed for %s: %s", imdb_id, e)
        return None

    if not rows:
        return {}

    def vals(key):
        return sorted({row[key]["value"].strip() for row in rows
                       if key in row and row[key].get("value")})

    awards = vals("awardLabel")
    facts = {}
    authors = vals("authorLabel")
    if authors:
        facts["source_author"] = authors[0]
        based = vals("basedOnLabel")
        if based:
            facts["source_work"] = based[0]
    major = [a for a in awards if _is_major(a)]
    if major:
        facts["awards"] = major[:8]
    elif awards:
        # Nothing major, but the count is still a weak signal worth keeping.
        facts["award_count"] = len(awards)
    return facts


async def topup_wikidata(
    title: str,
    media_type: str,
    *,
    imdb_id=None, tmdb_id=None, tvdb_id=None, anilist_id=None, anidb_id=None,
    cache_id=None, plex_rating_key=None, cache=None,
) -> bool:
    """Attach Wikidata facts to a title's raw:* cache entries, idempotent.

    Returns True when facts were actually added. A transient failure is NOT
    stamped — the same trap that once left titles permanently significance-less
    (see topup_significance): one bad moment must not become forever.
    """
    if media_type not in ("anime", "movie", "show"):
        return False
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        from src.services.media_enricher import _RAW_CACHE_DAYS
        id_keys = [v for v in (cache_id, anilist_id, anidb_id, tmdb_id, tvdb_id,
                               plex_rating_key) if v]
        t40 = (title or "")[:40]
        if t40:
            id_keys.append(t40)

        targets, imdb = [], imdb_id
        for k in id_keys:
            hit = cache.get_cache(f"raw:{media_type}:{k}")
            if not hit or not isinstance(hit.get("response"), dict):
                continue
            raw = hit["response"]
            if (raw.get("wikidata_checked")
                    and raw.get("wikidata_v") == _LOGIC_VERSION):
                return False
            imdb = imdb or raw.get("imdb_id")
            targets.append((f"raw:{media_type}:{k}", raw))
        if not targets or not imdb:
            return False

        facts = await fetch_wikidata_facts(imdb)
        if facts is None:
            return False          # transient — do not stamp, the walker retries

        added = False
        for key, raw in targets:
            fields = {"wikidata_checked": True, "wikidata_v": _LOGIC_VERSION}
            drop = ()
            if facts:
                fields["wikidata"] = facts
                added = True
            else:
                drop = ("wikidata",)
            write_fields(cache, key, raw, fields, drop=drop, days=_RAW_CACHE_DAYS)
        return added
    except Exception as e:
        logger.debug("[wikidata] top-up failed for %r: %s", title, e)
        return False
    finally:
        if owns:
            cache.close()


def format_wikidata_line(facts: dict) -> str:
    """One evidence line, or "" — the shape the judge reads facts in."""
    if not facts:
        return ""
    parts = []
    if facts.get("source_author"):
        work = facts.get("source_work")
        parts.append(f"adapted from {work or 'a work'} by {facts['source_author']}"
                     if work else f"adapted from a work by {facts['source_author']}")
    if facts.get("awards"):
        parts.append("won " + ", ".join(facts["awards"]))
    elif facts.get("award_count"):
        parts.append(f"{facts['award_count']} recorded awards (none major)")
    return "; ".join(parts)
