"""The container builds its own models (2026-10-23, NAS move).

On the desktop, start.bat checks the models before the server starts and
builds the missing ones (build_models.py --check, then build_models.py). The
container starts the server directly, so a fresh NAS install came up with no
bakes: every model call failed until someone opened a shell. With
AUTO_BUILD_MODELS (the Dockerfile sets it) the app does it itself, in the
background, right after start: the server answers at once - a first build
downloads 20 GB and more, far past any health check - and calls fail closed
until the bakes exist, as they would anyway.

Only the main host is built here. The node's bakes (build_models.py
--host pc) are pulled onto a machine someone games on, so that stays a
deliberate step; a missing one is named in the log.
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


def _report_drift(drift, url: str, host: str) -> None:
    """Name the bakes that no longer match the code (the summariser is
    rebuilt before this runs; what is left is the curator and the pitcher,
    whose prompt shapes chat, the benchmarks and the curator examples - a
    rebuild there stays the owner's call)."""
    for d in drift or []:
        logger.warning("[models] %s on %s differs from the code: %s - rebuild it with "
                       "python build_models.py --host %s", d["model"], url, "; ".join(d["reasons"]), host)


def rebake_summarizer(plan: dict, drift, post=None, tags=None) -> bool:
    """Rebuild the summariser bake on ``plan``'s host when it differs from
    the code: from its base model already on the host (nothing is pulled,
    the bake is a new manifest over the same weights), then unload it so the
    next call reads the new one. The curator and the pitcher are left alone.

    Measured before this went automatic (2026-10-26, PC, the summariser's
    system prompt "modes are open" against the old six-mode one): on 60
    titles the profiles moved no more than two runs of the same bake do
    (embedding text 0.899 against 0.910, n.s.), significance and memory
    extraction answered the same or better, music moved slightly towards its
    own prompt's rules (0.897 against 0.916). No re-enrichment needed.
    True when it rebuilt."""
    import httpx
    from src.config import settings
    from src.services.model_check import host_tags
    from src.services.setup_wizard import SUMMARIZER_SYSTEM_PROMPT
    name = (settings.SUMMARIZER_MODEL or "").strip()
    if not name.startswith("curatarr-") or not any(d["model"] == name for d in drift or []):
        return False
    base = plan.get("summarizer")
    have = (tags or host_tags)(plan["url"])
    if not base or have is None or (base not in have and f"{base}:latest" not in have):
        logger.warning("[models] %s on %s differs from the code, but its base %s is not on that "
                       "host - not pulled automatically; python build_models.py rebuilds it",
                       name, plan["url"], base)
        return False
    post = post or httpx.post
    url = plan["url"].rstrip("/")
    try:
        r = post(f"{url}/api/create", json={"model": name, "from": base, "system": SUMMARIZER_SYSTEM_PROMPT,
                                            "parameters": {"temperature": 0.7, "num_ctx": 8192},
                                            "stream": False}, timeout=300)
        if r.status_code != 200:
            logger.warning("[models] rebuilding %s on %s failed: HTTP %s", name, url, r.status_code)
            return False
        post(f"{url}/api/generate", json={"model": name, "keep_alive": 0}, timeout=60)
    except Exception as e:  # noqa: BLE001 - the old bake keeps working; the next start tries again
        logger.warning("[models] rebuilding %s on %s failed: %s", name, url, e)
        return False
    logger.info("[models] %s on %s rebuilt from %s with the code's system prompt", name, url, base)
    return True


async def ensure_main_models() -> dict:
    """Build what the main host is missing; report the embedding weights.
    Never raises (a background task at startup). Returns what it did."""
    from src.services.model_check import bake_drift, embedding_identity, host_plan, missing_on
    from src.services.ollama_hosts import split
    out: dict = {"built": None, "missing": None, "embedding": None, "drift": None}
    try:
        plan = await asyncio.to_thread(host_plan, "main")
        missing = await asyncio.to_thread(missing_on, plan["url"], plan["models"])
        out["missing"] = missing
        if missing is None:
            logger.warning("[models] Ollama at %s is not answering - the models were "
                           "not checked; restart Curatarr once it runs", plan["url"])
            return out
        if missing:
            logger.info("[models] missing on %s: %s - pulling and building now "
                        "(model calls fail until this is done)", plan["url"], ", ".join(missing))
            from src.services.setup_wizard import build_ollama_models
            results = await build_ollama_models(
                plan["url"], plan["curator"], plan["summarizer"],
                base_pitcher=plan["pitcher"], num_ctx=plan["num_ctx"],
                with_embedding=bool(plan["embedding"]), embed_model=plan["embedding"])
            out["built"] = results
            failed = [k for k, ok in results.items() if not ok]
            if failed:
                logger.error("[models] build on %s failed for: %s - see the lines above; "
                             "python build_models.py --host main retries", plan["url"],
                             ", ".join(failed))
            else:
                logger.info("[models] %s ready on %s", ", ".join(plan["models"]), plan["url"])
        out["drift"] = {"main": await asyncio.to_thread(bake_drift, plan)}
        if await asyncio.to_thread(rebake_summarizer, plan, out["drift"]["main"]):
            out["drift"]["main"] = await asyncio.to_thread(bake_drift, plan)
        _report_drift(out["drift"]["main"], plan["url"], "main")
        if split():
            pc = await asyncio.to_thread(host_plan, "pc")
            pc_missing = await asyncio.to_thread(missing_on, pc["url"], pc["models"])
            if pc_missing:
                from src.services.pc_host import node_name
                logger.warning("[models] %s is missing %s - run "
                               "python build_models.py --host pc while it is on",
                               node_name(cap=True), ", ".join(pc_missing))
            out["drift"]["pc"] = await asyncio.to_thread(bake_drift, pc)
            if await asyncio.to_thread(rebake_summarizer, pc, out["drift"]["pc"]):
                out["drift"]["pc"] = await asyncio.to_thread(bake_drift, pc)
            _report_drift(out["drift"]["pc"], pc["url"], "pc")
        ident = await asyncio.to_thread(embedding_identity)
        out["embedding"] = ident["status"]
        if ident["status"] == "mismatch":
            logger.error("[models] EMBEDDING WEIGHTS DIFFER: %s on %s is %s, the stored "
                         "vectors were made with %s. New vectors will not compare with the "
                         "old ones - pull the same weights or re-embed the library.",
                         ident["model"], plan["url"], (ident["host"] or "")[:12],
                         ident["recorded"][:12])
        elif ident["status"] == "match":
            logger.info("[models] embedding weights match the stored vectors (%s %s)",
                        ident["model"], ident["recorded"][:12])
    except Exception as e:  # noqa: BLE001 - startup helper: the server runs without it
        logger.warning("[models] automatic model check failed: %s", e, exc_info=True)
    return out
