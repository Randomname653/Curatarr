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


async def ensure_main_models() -> dict:
    """Build what the main host is missing; report the embedding weights.
    Never raises (a background task at startup). Returns what it did."""
    from src.services.model_check import embedding_identity, host_plan, missing_on
    from src.services.ollama_hosts import split
    out: dict = {"built": None, "missing": None, "embedding": None}
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
        if split():
            pc = await asyncio.to_thread(host_plan, "pc")
            pc_missing = await asyncio.to_thread(missing_on, pc["url"], pc["models"])
            if pc_missing:
                from src.services.pc_host import node_name
                logger.warning("[models] %s is missing %s - run "
                               "python build_models.py --host pc while it is on",
                               node_name(cap=True), ", ".join(pc_missing))
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
