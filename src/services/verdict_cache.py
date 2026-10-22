"""Deletion verdicts kept while nothing they rest on changed (2026-10-22,
NAS move phase 2, PR 4c).

A deletion run judged every candidate from scratch: a title kept for the
owner's taste, or left EVALUATE, was judged again the next scan - the same
model, the same constitution, the same facts, temperature 0 and a fixed
seed, so the same answer, paid for again on the GPU. On a split install that
GPU is the gaming PC's, and only while it is free.

A verdict is now stored under a hash of everything that decides it: the
bake and its weights (the digest Ollama reports), the judging host's profile
and window, the system prompt (constitution, untrusted-data rule and the
learned principles) and the evidence. The next run builds the evidence again
- cheap, no model - and reuses the verdict while that hash still matches.
Anything that changed - a play, a rating, a household member's watch, a new
principle, new weights - changes the hash, and the title is judged afresh.
The pitch of a CUT is kept the same way, per language.

Outages and other failed verdicts are never stored. Entries expire after
TTL_DAYS anyway.
"""
from __future__ import annotations

import hashlib
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

VERSION = 1
TTL_DAYS = 30


def key(facts: str, law_extra: str, model: str, digest: str, host) -> str:
    """The verdict's identity: every input of adjudicate()."""
    from src.services.pillars import _JUDGE_PREDICT, VERDICT_SCHEMA, judge_system_prompt
    blob = json.dumps([VERSION, model, digest or "", host.profile, host.num_ctx,
                       _JUDGE_PREDICT, VERDICT_SCHEMA,
                       judge_system_prompt(law_extra, host.profile), facts],
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _get(cache_key: str) -> Optional[dict]:
    from src.cache.metadata_cache import MetadataCache
    with MetadataCache() as cache:
        entry = cache.get_cache(cache_key)
    if not entry:
        return None
    value = entry.get("response") if isinstance(entry, dict) and "response" in entry else entry
    return value if isinstance(value, dict) else None


def _put(cache_key: str, value: dict) -> None:
    from src.cache.metadata_cache import MetadataCache
    with MetadataCache() as cache:
        cache.set_cache(cache_key, value, days=TTL_DAYS)


def get_verdict(vkey: str) -> Optional[dict]:
    """The stored verdict for this identity, or None. Blocking (sqlite):
    call through run_read."""
    v = _get(f"verdict:{vkey}")
    if not v or v.get("_error") or not v.get("verdict"):
        return None
    return {**v, "_cached": True}


def put_verdict(vkey: str, verdict: dict) -> bool:
    """Store a verdict the model actually gave. Failures (outages, parse
    errors - anything carrying _error) are never stored. Blocking."""
    if not verdict or verdict.get("_error") or not verdict.get("verdict"):
        return False
    clean = {k: v for k, v in verdict.items() if k != "_cached"}
    _put(f"verdict:{vkey}", clean)
    return True


def _pitch_key(vkey: str, variant: str) -> str:
    return f"pitch:{vkey}:{hashlib.sha256((variant or '').encode('utf-8')).hexdigest()[:16]}"


def get_pitch(vkey: str, variant: str) -> Optional[str]:
    """The stored pitch for this verdict identity and ``variant`` (language
    directive and guard: a floored CUT is pitched differently). Blocking."""
    v = _get(_pitch_key(vkey, variant))
    text = (v or {}).get("text")
    return text if isinstance(text, str) and text.strip() else None


def put_pitch(vkey: str, variant: str, text: str) -> None:
    if text and text.strip():
        _put(_pitch_key(vkey, variant), {"text": text})


async def bake_digest(url: str, model: str) -> str:
    """The digest of ``model`` on the Ollama at ``url`` (short), "" when it
    cannot be read - the TTL still bounds a stale entry then."""
    try:
        from src.services.pc_guard import ollama_client
        async with ollama_client(timeout=5) as client:
            r = await client.get(f"{url}/api/tags")
        r.raise_for_status()
        for m in r.json().get("models") or []:
            name = m.get("name") or m.get("model") or ""
            if name == model or name == f"{model}:latest" or name.split(":")[0] == model:
                return str(m.get("digest") or "")[:16]
    except Exception as e:  # noqa: BLE001 - a missing digest only shortens what the cache can prove
        logger.info("[verdict cache] no digest for %s: %s", model, e)
    return ""
