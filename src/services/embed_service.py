"""
Curatarr — central embedding service (eval package 2).

ALL embedding traffic goes through this module: one place for the model,
the endpoint, the Nomic task prefixes, batching, normalization — and the
atomic migration flip. The four previously-duplicated raw POST callsites
(embedding_generator, taste_engine, episodic_memory, plex_sync's evict)
now delegate here.

The active configuration is an app_state "embedding profile":

    {"model": …, "collection": …, "prefixes": bool, "num_ctx": int,
     "schema": 1|2}

No stored profile → the legacy default (settings.EMBEDDING_MODEL,
media_knowledge, NO prefixes — the v1 corpus was embedded prefix-free, so
prefixing new queries against it would mix spaces). The migration runner
builds the v2 corpus in a parallel collection and then flips the profile
in one set_profile() call — model, prefixes and collection switch
together, never partially.

Endpoint: /api/embed (batched, unit-normalized, defined truncation) —
the legacy /api/embeddings returned RAW vectors (norm ~13) and is
deprecated. Vectors are L2-normalized again on receipt, defensively.
num_gpu=0 everywhere: a GPU-resident embedder pushes the packed 27B into
partial offload (0.5 t/s measured).
"""
from __future__ import annotations

import json
import logging
import math
from typing import Optional

import httpx

from src.config import settings

logger = logging.getLogger(__name__)

PROFILE_KEY = "embedding_profile"
_BATCH = 32
_TIMEOUT = 120.0   # a cold CPU load + a 32-batch takes a while

_profile_cache: dict = {"value": None}


def _default_profile() -> dict:
    return {"model": settings.EMBEDDING_MODEL, "collection": "media_knowledge",
            "prefixes": False, "num_ctx": 8192, "schema": 1}


def effective_embedding_model() -> str:
    """The embedding model the runtime ACTUALLY uses — the stored profile's
    model, falling back to the legacy settings default. External eval catch:
    tray/setup/UI kept checking settings.EMBEDDING_MODEL (v1
    'nomic-embed-text') after the v2-moe migration flipped the profile, so
    presence checks green-lit a model the runtime no longer uses."""
    return get_profile().get("model") or settings.EMBEDDING_MODEL


def get_profile() -> dict:
    """The active embedding profile (cached; set_profile invalidates)."""
    if _profile_cache["value"] is not None:
        return _profile_cache["value"]
    prof = _default_profile()
    try:
        from src.services.app_state import get_state
        stored = json.loads(get_state(PROFILE_KEY) or "{}")
        if stored.get("model"):
            prof.update(stored)
    except Exception as e:
        logger.debug("[embed] profile read failed (using default): %s", e)
    _profile_cache["value"] = prof
    return prof


def set_profile(profile: dict) -> None:
    """Atomic flip: model + prefixes + collection switch together. Also
    refreshes the ChromaDB singleton so the new collection is served."""
    from src.services.app_state import set_state
    set_state(PROFILE_KEY, json.dumps(profile))
    _profile_cache["value"] = None
    try:
        from src.vector_store import chromadb_wrapper
        chromadb_wrapper.refresh_singleton()
    except Exception as e:
        logger.warning("[embed] chroma singleton refresh failed: %s", e)
    logger.info("[embed] profile flipped: model=%s collection=%s prefixes=%s",
                profile.get("model"), profile.get("collection"),
                profile.get("prefixes"))


def _l2(vec: list) -> Optional[list]:
    if not vec:
        return None
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm > 0 else None


async def _post_embed(model: str, inputs: list, num_ctx: int) -> list:
    """One /api/embed call. Returns a vector list per input, or [] on error."""
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/embed",
                json={"model": model, "input": inputs,
                      "keep_alive": "2m",
                      "options": {"num_gpu": 0, "num_ctx": num_ctx}},
            )
        if r.status_code != 200:
            logger.warning("[embed] HTTP %s from /api/embed — is %r pulled?",
                           r.status_code, model)
            return []
        return r.json().get("embeddings") or []
    except Exception as e:
        logger.warning("[embed] request failed: %s", e)
        return []


async def embed_documents(texts: list, profile: dict = None) -> list:
    """Embed document-side texts (index items, memories, taste inputs).
    Returns one Optional[vector] per input text, unit-normalized."""
    prof = profile or get_profile()
    prefix = "search_document: " if prof.get("prefixes") else ""
    out: list = []
    for i in range(0, len(texts), _BATCH):
        chunk = [prefix + (t or "") for t in texts[i:i + _BATCH]]
        vecs = await _post_embed(prof["model"], chunk, int(prof.get("num_ctx") or 8192))
        if len(vecs) != len(chunk):
            out.extend([None] * len(chunk))
            continue
        out.extend(_l2(v) for v in vecs)
    return out


async def embed_query(text: str, profile: dict = None) -> Optional[list]:
    """Embed a retrieval QUERY (chat questions, semantic library search,
    memory lookups) — the asymmetric side of the Nomic prefix schema."""
    prof = profile or get_profile()
    prefix = "search_query: " if prof.get("prefixes") else ""
    vecs = await _post_embed(prof["model"], [prefix + (text or "")],
                             int(prof.get("num_ctx") or 8192))
    return _l2(vecs[0]) if vecs else None


async def evict_embedder(profile: dict = None) -> None:
    """Unload the embedder from RAM (keep_alive 0) — called before big
    curator generations so nothing competes for memory."""
    prof = profile or get_profile()
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(f"{settings.effective_ollama}/api/embed",
                         json={"model": prof["model"], "input": "unload",
                               "keep_alive": 0})
    except Exception as e:
        logger.debug("[embed] evict failed: %s", e)
