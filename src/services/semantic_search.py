"""Curatarr — semantic library search core.

Extracted from chat's ``_get_rag_context`` so the SAME retrieval serves
both the hidden chat-RAG injection and the user-facing
``GET /api/library/semantic-search`` endpoint. The chat path stays
byte-identical via ``format_rag_context()``.

The endpoint additionally runs ``curated_search()`` — a two-stage
retrieve→rerank on top of the same vector store. Plain vector
neighbourhood ranks by premise-noun overlap ("magical girl" matches
everything magical-girl-shaped), so a query like "like Gushing Over
Magical Girls but with more adult cast" ignored the modifier entirely
and even returned the anchor itself as hit #1. The rerank judges the
overfetched candidates against the parsed constraints using the raw
enrichment tags (e.g. AniList "Primarily Adult Cast"), which never made
it into the embedding prose. Every stage hard-falls-back to the plain
vector order — the search is never worse than the old one.
"""

import json
import logging
import re

from src.services.watch_status import watched_lookup, watch_tag

logger = logging.getLogger(__name__)

# Interactive-search keep_alive: the summarizer default of 30s means every
# follow-up search pays a cold load; 5m keeps it warm across a search
# session without squatting on VRAM (curator_start evicts it anyway).
_SEARCH_KEEP_ALIVE = "5m"
_PARSE_TIMEOUT = 12.0
_RERANK_TIMEOUT = 30.0

_PARSE_SCHEMA = {
    "type": "object",
    "properties": {
        "anchor_title": {"type": ["string", "null"]},
        "constraints": {"type": "array", "items": {"type": "string"}},
        "search_text": {"type": "string"},
    },
    "required": ["anchor_title", "constraints", "search_text"],
}

_PARSE_SYS = (
    "You parse ONE media-library search query. "
    "anchor_title: the specific title referenced via like/similar-to, else null. "
    "Titles are often lowercase mid-sentence ('like gushing over magical girls but …' "
    "references the title Gushing Over Magical Girls). "
    "constraints: hard requirements or exclusions results must satisfy, as short phrases. "
    "search_text: a dense content description of what is wanted, WITHOUT the anchor title."
)

# Deterministic net under the LLM parse: "like <X> but/with/except …" — the
# tonal-query live test showed granite missing a lowercase mid-sentence title
# (anchor null → no anchor vector, no self-filter, duplicate anchor cards).
# A wrong regex guess is harmless: _anchor_vector verifies the candidate
# against actual library titles and returns None on a miss.
_ANCHOR_RE = re.compile(
    r"\b(?:like|similar to|wie)\s+(.{3,80}?)\s+(?:but|with|without|except|aber|nur)\b",
    re.IGNORECASE)

# minItems is load-bearing, not decoration: with grammar-forced output the
# summarizer satisfied the old schema with the CHEAPEST valid document —
# a literal {"ranking": []} — on every call, so the search silently fell
# back to vector order. An empty array must be grammatically illegal.
_RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranking": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "fit": {"type": "integer"},
                    "why": {"type": "string"},
                },
                "required": ["i", "fit", "why"],
            },
        }
    },
    "required": ["ranking"],
}

_RERANK_SYS = (
    "You rank a user's own library items against their search intent. "
    "Score each candidate 0-10 for satisfying the FULL query. "
    "Score 8-10 ONLY when EVERY constraint has a directly quoted tag as "
    "evidence. "
    "A constraint counts as satisfied ONLY when a listed tag or premise fact "
    "states it outright — 'implied' is not evidence. "
    "If any constraint lacks evidence, score at most 5 and name that "
    "constraint in why — never claim it is met. "
    "Violating an explicit constraint caps the score at 2. "
    "Constraints mean exactly what they say — never substitute a related "
    "concept (an adult CAST is about character ages, not adult themes; "
    "gore or violence alone is not a fetish dynamic). "
    "why: one short factual clause quoting the deciding tag. "
    "Return every candidate index exactly once."
)


async def semantic_hits(query: str, n_results: int = 5, domain: str = None,
                        user_id: int = None) -> list:
    """Ranked semantic hits over the ChromaDB vector store.

    When *domain* is given, only vectors tagged with that domain are
    considered — eliminating cross-media-type contamination. Each hit
    carries the watched-status tag (per *user_id*) and the size tag the
    chat context has always shown. This is the fast, LLM-free path the
    chat RAG rides; the search endpoint layers ``curated_search`` on top.
    """
    try:
        from src.services.embed_service import embed_query
        from src.vector_store.chromadb_wrapper import get_chroma_db
        # QUERY side of the Nomic prefix schema — the one place the missing
        # prefixes measurably hurt retrieval (asymmetric search).
        embedding = await embed_query(query)
        if not embedding:
            return []
        chroma = get_chroma_db()
        results = chroma.query(query_embeddings=[embedding],
                               n_results=n_results,
                               where={"domain": domain} if domain else None)
        return _hits_from_results(results, user_id=user_id, domain=domain)
    except Exception as e:
        logger.debug("semantic search failed: %s", e)
        return []


def _hits_from_results(results: dict, user_id: int = None,
                       domain: str = None) -> list:
    """Chroma result dict → hit dicts (keeps the vector rank + score)."""
    docs = results.get("documents", [[]])[0]
    metas = results.get("metadatas", [[]])[0]
    dists = (results.get("distances") or [[]])[0]
    from src.services.size_norms import short_size_tag
    watched = watched_lookup(user_id, [m.get("title", "") for m in metas],
                             category=domain)
    hits = []
    seen_titles = set()
    for idx, (doc, meta) in enumerate(zip(docs, metas)):
        title = meta.get("title", "Unknown")
        # The index can hold two docs for one title (id-keyed + title-keyed
        # from the writer's doc_id cascade) — the owner saw the same series
        # twice in one result list. First (closest) doc wins.
        tkey = _norm_title(title)
        if tkey and tkey in seen_titles:
            continue
        seen_titles.add(tkey)
        dist = dists[idx] if idx < len(dists) else None
        hits.append({
            "title": title,
            "year": meta.get("year") or None,
            "genres": meta.get("genres", ""),
            "themes": meta.get("themes", ""),
            "doc": doc or "",
            "watch_tag": watch_tag(watched.get(title)),
            # The index never wrote tmdb/tvdb/plex keys into metadata, so the
            # size lookup has always resolved by title — call it that way.
            "size_tag": short_size_tag(title=title),
            "score": round(1.0 - dist, 4) if isinstance(dist, (int, float)) else None,
        })
    return hits


def format_rag_context(hits: list) -> str:
    """The exact line format the chat prompt has always injected."""
    lines = []
    for h in hits:
        stag = h.get("size_tag") or ""
        themes = h.get("themes") or ""
        lines.append(f"- {h['title']} [{h['watch_tag']}]{(' ' + stag) if stag else ''} "
                     f"({h.get('genres', '')}{', '+themes if themes else ''}): "
                     f"{(h.get('doc') or '')[:200]}")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Curated search (endpoint path): parse → anchor-aware retrieve → LLM rerank
# ─────────────────────────────────────────────────────────────────────────────

def _norm_title(t: str) -> str:
    return "".join(c for c in (t or "").lower() if c.isalnum())


async def _summarizer_json(system: str, user: str, schema: dict,
                           num_predict: int, timeout: float):
    """One summarizer-tier JSON call (granite). None on any failure —
    callers fall back to the plain vector order. Deliberately NOT
    curator-tier: an interactive search must never evict the summarizer
    mid-enrichment or wait behind the chat gate."""
    import httpx
    from src.config import settings
    from src.services.llm_utils import ollama_options, strip_think_tags
    for model in (settings.SUMMARIZER_MODEL, settings.BASE_SUMMARIZER_MODEL):
        if not model:
            continue
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{settings.effective_ollama}/api/chat", json={
                    "model": model,
                    "messages": [{"role": "system", "content": system},
                                 {"role": "user", "content": user}],
                    "stream": False,
                    "format": schema,
                    "keep_alive": _SEARCH_KEEP_ALIVE,
                    **ollama_options(temperature=0.1, num_predict=num_predict),
                })
            if r.status_code != 200:
                continue
            raw = strip_think_tags(r.json().get("message", {}).get("content", "") or "")
            return json.loads(raw)
        except Exception as e:
            logger.debug("[search] summarizer json via %s failed: %s", model, e)
    return None


async def _parse_query(query: str, domain: str = None):
    """Query → {anchor_title, constraints, search_text} or None."""
    hint = f" (library category: {domain})" if domain else ""
    parsed = await _summarizer_json(
        _PARSE_SYS, f"QUERY{hint}: {query}", _PARSE_SCHEMA,
        num_predict=220, timeout=_PARSE_TIMEOUT)
    if not isinstance(parsed, dict) or "search_text" not in parsed:
        return None
    anchor = parsed.get("anchor_title")
    return {
        "anchor_title": anchor.strip() if isinstance(anchor, str) and anchor.strip() else None,
        "constraints": [c for c in (parsed.get("constraints") or [])
                        if isinstance(c, str) and c.strip()][:6],
        "search_text": str(parsed.get("search_text") or "").strip() or query,
    }


async def _anchor_vector(anchor_title: str, domain: str = None):
    """Resolve an anchor title to its STORED index vector.

    The index ids follow the plex-key→tmdb→anilist→title cascade, so a
    direct get_by_id(title) misses id-keyed docs. Instead: a tiny vector
    probe for the title, then a normalized-title match against the top
    hits, then get_by_id on the MATCHED id for the stored embedding.
    Returns (vector|None, matched_title|None).
    """
    try:
        from src.services.embed_service import embed_query
        from src.vector_store.chromadb_wrapper import get_chroma_db
        probe = await embed_query(anchor_title)
        if not probe:
            return None, None
        chroma = get_chroma_db()
        res = chroma.query(query_embeddings=[probe], n_results=3,
                           where={"domain": domain} if domain else None)
        ids = (res.get("ids") or [[]])[0]
        metas = (res.get("metadatas") or [[]])[0]
        want = _norm_title(anchor_title)
        for cid, meta in zip(ids, metas):
            got = _norm_title(meta.get("title", ""))
            if got and (got == want or want in got or got in want):
                doc = chroma.get_by_id(str(cid))
                emb = doc.get("embedding") if doc else None
                if emb is not None and len(emb):
                    return list(emb), meta.get("title", "")
    except Exception as e:
        logger.debug("[search] anchor resolve failed: %s", e)
    return None, None


def _candidate_lines(hits: list, domain: str = None) -> list:
    """Compact fact line per candidate for the rerank prompt.

    Tags come from the verified-data cache merge PLUS the title-keyed raw
    entry — the raw AniList tags carry the demographic facts ("Primarily
    Adult Cast") that the embedding prose never contained. Cache-only,
    one shared handle, no network."""
    from src.cache.metadata_cache import MetadataCache
    from src.services.media_enricher import build_verified_data
    lines = []
    cache = None
    try:
        cache = MetadataCache()
    except Exception as e:
        logger.debug("[search] cache open failed: %s", e)
    for i, h in enumerate(hits):
        tags = []
        if cache is not None:
            try:
                vd = build_verified_data(h["title"], domain or "movie",
                                         cache=cache) or {}
                kw = vd.get("keywords") or []
                tags = list(kw) if isinstance(kw, list) else [str(kw)]
                raw_hit = cache.get_cache(f"raw:{domain}:{h['title'][:40]}") if domain else None
                raw = (raw_hit or {}).get("response") or {}
                for t in (raw.get("tags") or raw.get("keywords") or []):
                    if t not in tags:
                        tags.append(t)
            except Exception as e:
                logger.debug("[search] facts for %s failed: %s", h.get("title"), e)
        year = f" ({h['year']})" if h.get("year") else ""
        tag_s = ", ".join(str(t) for t in tags[:15]) or "none"
        lines.append(f"[{i}] {h['title']}{year} · {h.get('genres') or '?'} · "
                     f"tags: {tag_s} · {h.get('watch_tag') or ''} :: "
                     f"{(h.get('doc') or '')[:180]}")
    try:
        if cache is not None:
            cache.close()
    except Exception:
        pass
    return lines


async def curated_search(query: str, n_results: int = 10, domain: str = None,
                         user_id: int = None) -> dict:
    """Two-stage search for the endpoint: retrieve (anchor-aware, overfetched)
    → summarizer rerank against the parsed constraints. Falls back to the
    plain vector order at every stage → ``mode`` says which one served."""
    limit = max(1, min(int(n_results or 10), 25))
    parsed = await _parse_query(query, domain)
    if parsed and not parsed["anchor_title"]:
        m = _ANCHOR_RE.search(query)
        if m:
            parsed["anchor_title"] = m.group(1).strip(" .,;:'\"")
    anchor_used = None

    # Retrieval vector: the anchor item's stored embedding when the query
    # references a library title (similar-to-item), else the query text.
    vec = None
    if parsed and parsed["anchor_title"]:
        vec, anchor_used = await _anchor_vector(parsed["anchor_title"], domain)
    try:
        if vec is None:
            from src.services.embed_service import embed_query
            vec = await embed_query((parsed or {}).get("search_text") or query)
        if not vec:
            return {"results": [], "mode": "vector", "anchor": None}
        from src.vector_store.chromadb_wrapper import get_chroma_db
        fetch_n = min(40, max(3 * limit, 24))
        results = get_chroma_db().query(
            query_embeddings=[vec], n_results=fetch_n,
            where={"domain": domain} if domain else None)
        hits = _hits_from_results(results, user_id=user_id, domain=domain)
    except Exception as e:
        logger.debug("[search] curated retrieval failed: %s", e)
        return {"results": await semantic_hits(query, limit, domain, user_id),
                "mode": "vector", "anchor": None}

    # "like X but …" wants neighbours of X, not X itself.
    if anchor_used:
        want = _norm_title(anchor_used)
        hits = [h for h in hits if _norm_title(h["title"]) != want]

    # SECOND, constraint-focused probe: the anchor/search-text vector stays
    # glued to the anchor's genre neighbourhood ("magical girl …"), so
    # cross-genre titles that nail the TONE (the owner's Mnemosyne /
    # Speed Grapher examples) never enter the pool. A probe embedded from
    # the constraints alone widens it; the rerank sorts the union.
    if parsed and parsed["constraints"]:
        try:
            from src.services.embed_service import embed_query as _eq
            cvec = await _eq(", ".join(parsed["constraints"]))
            if cvec:
                cres = get_chroma_db().query(
                    query_embeddings=[cvec], n_results=max(12, fetch_n // 2),
                    where={"domain": domain} if domain else None)
                extra = _hits_from_results(cres, user_id=user_id, domain=domain)
                seen = {_norm_title(h["title"]) for h in hits}
                if anchor_used:
                    seen.add(_norm_title(anchor_used))
                hits += [h for h in extra
                         if _norm_title(h["title"]) not in seen][:max(0, 40 - len(hits))]
        except Exception as e:
            logger.debug("[search] constraint probe failed: %s", e)

    if not hits:
        return {"results": [], "mode": "vector", "anchor": anchor_used}

    ranking = None
    if parsed:
        lines = _candidate_lines(hits, domain)
        cons = "; ".join(parsed["constraints"]) or "none stated"
        ranking = await _summarizer_json(
            _RERANK_SYS,
            f"QUERY: {query}\nHARD CONSTRAINTS: {cons}\n"
            f"Score ALL {len(hits)} candidates (indices 0-{len(hits) - 1}), "
            f"one ranking entry each.\nCANDIDATES:\n" + "\n".join(lines),
            _RERANK_SCHEMA,
            num_predict=min(2000, 80 + 45 * len(hits)),
            timeout=_RERANK_TIMEOUT)
        if isinstance(ranking, dict) and not ranking.get("ranking"):
            logger.info("[search] rerank returned an empty ranking — "
                        "falling back to vector order")

    if isinstance(ranking, dict) and isinstance(ranking.get("ranking"), list):
        scored = {}
        for r in ranking["ranking"]:
            try:
                i = int(r.get("i"))
                if 0 <= i < len(hits) and i not in scored:
                    fit = int(r.get("fit", 0))
                    why = str(r.get("why") or "").strip()[:120]
                    # Mechanical enforcement of the no-substitution rule: the
                    # 8B reranker keeps writing "X implied by Y" in its own
                    # evidence while scoring 8-10 (live: Madoka got a 10 with
                    # "fetish dynamics implied by predatory entity"). When
                    # the model confesses to inference instead of evidence,
                    # cap the score where the prompt said it belongs.
                    if fit > 5 and re.search(r"\bimplie[ds]?\b|\bsuggests?\b",
                                             why, re.IGNORECASE):
                        fit = 5
                    scored[i] = (fit, why)
            except Exception:
                continue
        if scored:
            order = sorted(range(len(hits)),
                           key=lambda i: (-scored.get(i, (-1,))[0], i))
            out = []
            for i in order[:limit]:
                h = dict(hits[i])
                if i in scored:
                    h["fit"] = scored[i][0]
                    h["fit_note"] = scored[i][1]
                out.append(h)
            return {"results": out, "mode": "reranked", "anchor": anchor_used}

    return {"results": hits[:limit], "mode": "vector", "anchor": anchor_used}
