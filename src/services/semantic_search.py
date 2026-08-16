"""Curatarr — semantic library search core.

Extracted from chat's ``_get_rag_context`` so the SAME retrieval serves
both the hidden chat-RAG injection and the user-facing
``GET /api/library/semantic-search`` endpoint. The chat path stays
byte-identical via ``format_rag_context()``.

The endpoint runs ``curated_search()`` on top: query parse (LLM) →
anchor-aware dual-probe retrieval → **deterministic constraint-evidence
scoring**. History of that last stage: a granite listwise rerank was
tried and removed — it collapsed on 20-40 candidate lists (positional
boilerplate, hallucinated constraint matches), and its tag input was
starved anyway by a doc-id bug (title-key cache lookups hit 3-4% while
doc-id lookups hit 87%). "Does a tag evidence this constraint?" is a
similarity question, not a generation task: embedding cosine between a
parsed constraint and each individual tag cannot overlook a literal
"Femdom", cannot invent evidence, and produces an honest machine-built
fit note. The only LLM left in the loop is the query parse, which
proved reliable in every live round.
"""

import hashlib
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

# ── Evidence-scoring constants — calibrated on tests/test_search_fixtures.py
# (the owner's two live review rounds), do not tweak by feel. Constraint↔tag
# comparison is SYMMETRIC document-side embedding: the asymmetric
# search_query:/search_document: prefixes are tuned for query→long-document
# retrieval and collapse short-phrase similarity (measured: "subversive
# fetish dynamics"↔"Femdom" scored 0.23 asymmetric vs a usable symmetric
# scale where Gore↔"Body horror and gore" is 0.79). Literal substring
# matches short-circuit to 1.0 BEFORE any embedding — a tag that says the
# constraint outright can never be missed again. ─────────────────────────────
_EV_FLOOR = 0.72     # cosine below this contributes 0 evidence (symmetric
                     # short-tag noise floor measured at ~0.65-0.76:
                     # "darker"↔"Magic" scored 0.76)
_EV_CEIL = 0.95      # cosine at/above this counts as full evidence
_T_LOW = 0.78        # any positive constraint below this → fit capped at 5
_T_NEG = 0.85        # a NEGATED constraint at/above this → fit capped at 2
_W_RETRIEVAL = 0.35  # weight of the vector-retrieval score in fit
_W_EVIDENCE = 0.65   # weight of the constraint-evidence mean in fit
_TAG_CAP = 20        # tags per candidate fed into scoring
_NEG_PREFIXES = ("no ", "not ", "without ", "non-", "kein ", "keine ", "ohne ")
# Cast-demographic words form an ANTONYM family embeddings cannot separate
# ("adult cast"↔"Primarily Teen Cast" embeds near-identical). When both the
# constraint and a tag name a demographic and they DIFFER, that tag is
# counter-evidence — its similarity is forced to 0 for this constraint.
_DEMO_WORDS = ("adult", "teen", "child", "kid")

# Structure nouns that appear in BOTH constraints and tags without carrying
# the constraint's meaning. Live round 5: "subversive fetish DYNAMICS"
# lexically matched "School club DYNAMICS" and "teacher-student DYNAMICS"
# at 1.00 — the shared word is scaffolding, not evidence. These words never
# count as a single-word match and cannot be the sole carrier of an
# all-words match.
_GENERIC_WORDS = frozenset({
    "dynamics", "dynamic", "setting", "settings", "elements", "element",
    "tone", "tones", "theme", "themes", "story", "stories", "narrative",
    "structure", "style", "styles", "school", "comedy", "drama",
    "character", "characters", "protagonist", "antagonist", "cast",
    "series", "anime", "show", "content",
})

# Curated concept families over the CLOSED AniList tag vocabulary (389 tags
# on this install). Embeddings put "Femdom"↔"fetish dynamics" at 0.71 —
# inside the 0.65-0.76 short-tag noise band — so semantic-field membership
# is encoded explicitly: a constraint containing the trigger word treats any
# family member tag as literal evidence. Small on purpose; extend only with
# vocabulary actually observed in the library.
_CONCEPT_FAMILIES = {
    "fetish": ("femdom", "bondage", "sadism", "masochism", "vore", "bdsm",
               "psychosexual", "pain as pleasure", "yandere"),
    "kink": ("femdom", "bondage", "sadism", "masochism", "vore", "bdsm",
             "psychosexual", "pain as pleasure", "yandere"),
    "dark": ("gore", "body horror", "horror", "tragedy", "grim", "bleak",
             "dark", "psychological", "suicide", "death game"),
    # NB: no bare "sexual" member — it substring-matched "Heterosexual"
    # (an orientation tag, not a maturity marker) in calibration.
    "mature": ("seinen", "josei", "ecchi", "nudity", "explicit",
               "adult", "large breasts"),
}

# Mature TONE is a separate axis from the mature RATING: the owner's round-6
# review asked for Seinen/Josei/Psychological/Gore to synthesize "mature
# tone" (serious register) while slapstick nudity must NOT (the FSK trap
# fixed in round 5). Ordered by precision — best-match picks the earliest.
_TONE_FAMILY = ("seinen", "josei", "psychological", "noir", "gore", "tragedy")


def _families_for(ckey: str) -> tuple:
    """Family member tuples whose trigger appears in the constraint. The
    'mature'/'adult' trigger is skipped for CAST constraints — those are
    the demographic-guard's domain ("adult cast" must not be evidenced by
    Seinen/Ecchi) — and for TONE constraints: the family encodes the
    content RATING (nudity/ecchi), but "mature TONE" means serious
    register — live round 5 scored "slapstick nudity" a 1.00 for it.
    Tone constraints fall through to embeddings and stay honestly
    'unbelegt' rather than FSK-confused."""
    fams = []
    for trigger, members in _CONCEPT_FAMILIES.items():
        if trigger in ckey:
            if trigger == "mature" and ("cast" in ckey or "tone" in ckey):
                continue
            fams.append(members)
    if "adult" in ckey and "cast" not in ckey and "tone" not in ckey:
        fams.append(_CONCEPT_FAMILIES["mature"])
    if "tone" in ckey and any(w in ckey for w in ("mature", "adult",
                                                  "serious", "grim")):
        fams.append(_TONE_FAMILY)
    return tuple(fams)

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

# Deterministic net under the LLM parse: "like <X> but/with/except …" — a
# tonal-query live test showed granite missing a lowercase mid-sentence title
# (anchor null → no anchor vector, no self-filter, duplicate anchor cards).
# A wrong regex guess is harmless: _anchor_vector verifies the candidate
# against actual library titles and returns None on a miss.
_ANCHOR_RE = re.compile(
    r"\b(?:like|similar to|wie)\s+(.{3,80}?)\s+(?:but|with|without|except|aber|nur)\b",
    re.IGNORECASE)


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
    """Chroma result dict → hit dicts (keeps vector rank, score AND doc id).

    The doc id is load-bearing: the enrichment cache keys most entries by
    the arr doc-id ("sonarr:1694"), and title-keyed lookups only hit 3-4%
    of titles — the original cause of the search judging candidates with
    empty tag lists.
    """
    ids = (results.get("ids") or [[]])[0]
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
            "doc_id": str(ids[idx]) if idx < len(ids) and ids[idx] else None,
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
# Curated search: parse → anchor-aware retrieve → constraint-evidence scoring
# ─────────────────────────────────────────────────────────────────────────────

def _norm_title(t: str) -> str:
    return "".join(c for c in (t or "").lower() if c.isalnum())


def _norm_tag(t: str) -> str:
    """Display-preserving normalization key: the index themes carry U+2011
    non-breaking hyphens ("Gore‑heavy") and casing collisions."""
    return re.sub(r"\s+", " ", (t or "").replace("‑", "-").strip()).lower()


async def _summarizer_json(system: str, user: str, schema: dict,
                           num_predict: int, timeout: float):
    """One summarizer-tier JSON call (granite) — used ONLY for the query
    parse. None on any failure; callers fall back to the plain vector
    order. Deliberately NOT curator-tier: an interactive search must never
    evict the summarizer mid-enrichment or wait behind the chat gate."""
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


# ── Tag acquisition (doc-id first — the 87% path) ────────────────────────────

def _candidate_tags(hits: list, domain: str = None) -> list:
    """Per candidate: raw AniList tags (doc-id keyed — the truth source; the
    LLM-enriched keyword rewrite DROPS critical tags like Gleipnir's Femdom)
    ∪ the Chroma themes phrases already on the hit. Cache-only, one handle.
    Returns a list[str] per hit, display casing preserved, deduped, capped.
    """
    from src.cache.metadata_cache import MetadataCache
    cache = None
    try:
        cache = MetadataCache()
    except Exception as e:
        logger.debug("[search] cache open failed: %s", e)
    out = []
    for h in hits:
        tags, seen = [], set()

        def _add(items):
            for t in items or []:
                s = str(t).replace("‑", "-").strip()
                k = _norm_tag(s)
                if s and k not in seen:
                    seen.add(k)
                    tags.append(s)

        if cache is not None and domain:
            keys = []
            if h.get("doc_id"):
                keys += [f"raw:{domain}:{h['doc_id']}",
                         f"enriched:{domain}:{h['doc_id']}"]
            keys += [f"raw:{domain}:{(h.get('title') or '')[:40]}",
                     f"enriched:{domain}:{(h.get('title') or '')[:40]}"]
            for k in keys:
                try:
                    hit = cache.get_cache(k)
                except Exception:
                    hit = None
                resp = (hit or {}).get("response") or {}
                _add(resp.get("tags") or resp.get("keywords"))
                if tags:
                    break
        _add((h.get("themes") or "").split(","))
        out.append(tags[:_TAG_CAP])
    try:
        if cache is not None:
            cache.close()
    except Exception:
        pass
    return out


# ── Embedding with a persistent per-text cache ───────────────────────────────

_vec_memo: dict = {}   # in-process: norm_text -> unit vector


async def _texts_to_vectors(texts: list) -> dict:
    """norm_text -> unit vector for every distinct text, via nomic (CPU).

    ALWAYS document-side (symmetric): constraints and tags are compared as
    short phrases against each other, and the asymmetric prefix schema is
    a retrieval tool that collapses short-phrase cosines (see the constants
    block). Two cache layers: an in-process memo and the api_cache sqlite
    (``tagemb:{model}:{sha1}`` — the taste_engine emb:* pattern). The raw
    AniList vocabulary is a CLOSED 389-tag set, so after warm-up almost
    every search scores fully from cache. Misses are embedded batched.
    """
    from src.services.embed_service import (get_profile, _post_embed, _l2)
    prof = get_profile()
    model = prof.get("model") or ""
    want = {}
    for t in texts:
        k = _norm_tag(t)
        if k:
            want.setdefault(k, t)
    result, misses = {}, []
    cache = None
    try:
        from src.cache.metadata_cache import MetadataCache
        cache = MetadataCache()
    except Exception:
        cache = None
    for k, orig in want.items():
        if k in _vec_memo:
            result[k] = _vec_memo[k]
            continue
        if cache is not None:
            try:
                ck = f"tagemb2:{model}:{hashlib.sha1(k.encode()).hexdigest()[:16]}"
                hit = cache.get_cache(ck)
                vec = ((hit or {}).get("response") or {}).get("v")
                if vec:
                    _vec_memo[k] = vec
                    result[k] = vec
                    continue
            except Exception:
                pass
        misses.append((k, orig))
    if misses:
        prefix = "search_document: " if prof.get("prefixes") else ""
        num_ctx = int(prof.get("num_ctx") or 512)
        texts_in = [prefix + orig for _, orig in misses]
        vecs = []
        for i in range(0, len(texts_in), 32):
            vecs.extend(await _post_embed(model, texts_in[i:i + 32], num_ctx))
        if len(vecs) == len(misses):
            for (k, _orig), v in zip(misses, vecs):
                unit = _l2(v)
                if unit:
                    _vec_memo[k] = unit
                    result[k] = unit
                    if cache is not None:
                        try:
                            ck = f"tagemb2:{model}:{hashlib.sha1(k.encode()).hexdigest()[:16]}"
                            cache.set_cache(ck, {"v": unit}, days=90)
                        except Exception:
                            pass
    try:
        if cache is not None:
            cache.close()
    except Exception:
        pass
    return result


def _dot(a: list, b: list) -> float:
    return sum(x * y for x, y in zip(a, b))


def _split_negation(constraint: str):
    """'no gore' → ('gore', True); 'darker' → ('darker', False)."""
    c = constraint.strip()
    low = c.lower()
    for p in _NEG_PREFIXES:
        if low.startswith(p):
            return c[len(p):].strip() or c, True
    return c, False


def _evidence_scores(constraints: list, cand_tags: list, hits: list) -> list:
    """Deterministic fit per candidate from constraint↔tag cosine evidence.

    Returns a list of (fit_int_0_10, note_str) aligned with *hits*. The
    caller has already resolved all needed vectors into _vec_memo via
    _texts_to_vectors — this function is pure math and cannot hallucinate:
    every claim in the note quotes the argmax tag and its cosine.
    """
    cons = [_split_negation(c) for c in constraints]
    # Batch-relative retrieval normalization (scores are cosine-similarity
    # derived and shift with the query — the ORDER carries the signal).
    scores = [h.get("score") for h in hits]
    known = [s for s in scores if isinstance(s, (int, float))]
    lo, hi = (min(known), max(known)) if known else (0.0, 1.0)
    span = (hi - lo) or 1.0

    out = []
    for hi_idx, (h, tags) in enumerate(zip(hits, cand_tags)):
        r = scores[hi_idx]
        r_norm = ((r - lo) / span) if isinstance(r, (int, float)) else 0.5
        # Constraint-probe hits are tone matches from OUTSIDE the anchor's
        # genre neighbourhood — welcome (Mnemosyne), but at equal evidence
        # they should rank behind actual neighbours (round 6: the mahjong
        # thriller Akagi strolled into a magical-girl query on a
        # "Psychological manipulation" tag). Half retrieval weight.
        if h.get("_probe") == "constraint":
            r_norm *= 0.5
        evs, notes = [], []
        capped5 = capped2 = False
        for text, negated in cons:
            ckey = _norm_tag(text)
            cvec = _vec_memo.get(ckey)
            c_demo = {w for w in _DEMO_WORDS if w in ckey}
            best_sim, best_tag = 0.0, None
            # LEXICAL FIRST: literal containment is evidence by definition,
            # no model involved — the hard guarantee that a literal "Femdom"
            # can never be overlooked again. Deliberately strict: the whole
            # constraint core as substring, ALL its words, or a single
            # DISTINCTIVE word (≥6 chars — "fetish", "femdom"; not "cast"/
            # "tone", which matched Teen/Female Cast in calibration).
            cwords = [w for w in ckey.split() if len(w) >= 4]
            carriers = [w for w in cwords if w not in _GENERIC_WORDS]
            families = _families_for(ckey)
            # Collect ALL lexical hits and pick the most precise one instead
            # of the first in tag order — round 6: Gleipnir cited "yandere
            # romance" while the tags literally contained "female antagonist
            # femdom". Priority: direct constraint/carrier containment (0),
            # then family members by their curated order (1 + index).
            lex_best = None   # (priority, tag)
            for t in tags:
                tkey = _norm_tag(t)
                if not tkey:
                    continue
                # All-words counts only when at least one CARRIER word is
                # among the matches (a tag matching nothing but scaffolding
                # words is no evidence).
                all_words_hit = (cwords and all(w in tkey for w in cwords)
                                 and any(w in tkey for w in carriers))
                prio = None
                if (len(ckey) >= 4 and (ckey in tkey or tkey in ckey)) \
                        or all_words_hit \
                        or any(w in tkey for w in carriers if len(w) >= 6):
                    prio = 0
                else:
                    for fam in families:
                        for mi, m in enumerate(fam):
                            if m in tkey:
                                prio = 1 + mi
                                break
                        if prio is not None:
                            break
                if prio is not None and (lex_best is None or prio < lex_best[0]):
                    lex_best = (prio, t)
                    if prio == 0:
                        break
            if lex_best is not None:
                best_sim, best_tag = 1.0, lex_best[1]
            if best_sim < 1.0 and cvec:
                for t in tags:
                    tkey = _norm_tag(t)
                    # Demographic antonym guard: a DIFFERING cast-age tag is
                    # counter-evidence, not near-evidence.
                    if c_demo:
                        t_demo = {w for w in _DEMO_WORDS if w in tkey}
                        if t_demo and t_demo != c_demo:
                            continue
                    tvec = _vec_memo.get(tkey)
                    if tvec:
                        s = _dot(cvec, tvec)
                        if s > best_sim:
                            best_sim, best_tag = s, t
            if negated:
                # A satisfied exclusion is FULL evidence — "no gore" met is
                # as good as a positive constraint met, not a 1-sim penalty
                # that punishes benign vocabulary proximity.
                if best_sim >= _T_NEG:
                    capped2 = True
                    evs.append(0.0)
                    notes.append(f"violates '{text}' ↔ {best_tag} ({best_sim:.2f})")
                else:
                    evs.append(1.0)
                    notes.append(f"free of '{text}' ✓")
            else:
                ev = max(0.0, min(1.0, (best_sim - _EV_FLOOR) / (_EV_CEIL - _EV_FLOOR)))
                evs.append(ev)
                if best_sim < _T_LOW:
                    capped5 = True
                    notes.append(
                        f"{text}: unbelegt"
                        + (f" (best: {best_tag} {best_sim:.2f})" if best_tag else ""))
                else:
                    notes.append(f"{text} ↔ {best_tag} ({best_sim:.2f})")
        ev_mean = sum(evs) / len(evs) if evs else 0.5
        fit = 10.0 * (_W_RETRIEVAL * r_norm + _W_EVIDENCE * ev_mean)
        if capped5:
            fit = min(fit, 5.0)
        if capped2:
            fit = min(fit, 2.0)
        out.append((int(round(fit)), " · ".join(notes)[:220]))
    return out


async def curated_search(query: str, n_results: int = 10, domain: str = None,
                         user_id: int = None) -> dict:
    """Parse → anchor-aware dual-probe retrieval → deterministic
    constraint-evidence scoring. ``mode`` reports the serving path:
    "evidence" (scored against parsed constraints) or "vector" (plain
    similarity order — parse failed or the query stated no constraints)."""
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
    # the constraints alone widens it; evidence scoring sorts the union.
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
                for h in extra:
                    h["_probe"] = "constraint"
                hits += [h for h in extra
                         if _norm_title(h["title"]) not in seen][:max(0, 40 - len(hits))]
        except Exception as e:
            logger.debug("[search] constraint probe failed: %s", e)

    if not hits:
        return {"results": [], "mode": "vector", "anchor": anchor_used}

    if parsed and parsed["constraints"]:
        try:
            cand_tags = _candidate_tags(hits, domain)
            cons_core = [_split_negation(c)[0] for c in parsed["constraints"]]
            await _texts_to_vectors(
                cons_core + [t for tags in cand_tags for t in tags])
            scored = _evidence_scores(parsed["constraints"], cand_tags, hits)
            order = sorted(range(len(hits)),
                           key=lambda i: (-scored[i][0], i))
            out = []
            for i in order[:limit]:
                h = dict(hits[i])
                h.pop("_probe", None)
                h["fit"], h["fit_note"] = scored[i]
                out.append(h)
            return {"results": out, "mode": "evidence", "anchor": anchor_used}
        except Exception as e:
            logger.warning("[search] evidence scoring failed — vector order: %s", e)

    out = []
    for h in hits[:limit]:
        h = dict(h)
        h.pop("_probe", None)
        out.append(h)
    return {"results": out, "mode": "vector", "anchor": anchor_used}
