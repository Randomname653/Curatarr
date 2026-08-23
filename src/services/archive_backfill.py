"""One place that knows how complete each metadata source is, and how to
catch it up.

A fresh install starts with none of it. The background custodian fills it at
a deliberately gentle pace, which is right for a library that is already
running and wrong for one that was set up an hour ago — at 150 titles a day
a large library is not backfilling, it is waiting out the year. Meanwhile
every deletion verdict passed in the interim is reasoned from whatever
happened to be there.

So: the same walkers, on demand, with the coverage figure that says whether
that is still worth doing. Above THRESHOLD the offer disappears from the UI
and the ordinary ticks carry the remainder — a backfill button that never
goes away is just clutter with a progress bar.

Coverage is measured over LIVE raw cache entries for video categories, which
is the population every one of these sources actually applies to.
"""
import asyncio
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Above this, catching up by hand stops being worth a button: the remainder
# is a rounding error the daily ticks clear on their own.
THRESHOLD_PCT = 90.0

VIDEO = ("movie", "show", "anime")

# Titles in flight at once. Two is enough to hide the fetch behind the model
# and no more: Ollama serialises requests for one model on one card, so a
# wider pool queues rather than distils, while pressing harder on services
# that asked us to be gentle.
WIDTH = 2


def _has_significance(raw: dict) -> bool:
    from src.services.media_enricher import _SIG_PROMPT_VERSION
    return raw.get("significance_v") == _SIG_PROMPT_VERSION


def _has_reception(raw: dict) -> bool:
    from src.services.reception import _reception_settled
    return _reception_settled(raw)


def _has_wikidata(raw: dict) -> bool:
    from src.services.wikidata import _LOGIC_VERSION
    if raw.get("wikidata_v") == _LOGIC_VERSION:
        return True
    # Both Wikidata and OMDb are keyed on the IMDb id. A title without one can
    # never gain either, so counting it as outstanding would hold coverage
    # below the threshold for good and leave a button on screen that cannot
    # finish its own job. "Has what it can have" is the honest measure.
    return not raw.get("imdb_id")


SOURCES = {
    "external_ids": {
        "label": "External ids from your *arr",
        "blurb": "Sonarr and Radarr already know each title's IMDb id. Two of "
                 "the sources below cannot run without it — do this one first.",
        "done": lambda raw: bool(raw.get("imdb_id")),
    },
    "significance": {
        "label": "Wikipedia significance",
        "blurb": "Whether a title has documented cultural standing — the "
                 "archive pillar's evidence. Uses the summariser, so it wants "
                 "the GPU.",
        "done": _has_significance,
    },
    "reception": {
        "label": "Community reception",
        "blurb": "What audiences actually said. Plain HTTP plus a short "
                 "summarisation.",
        # the settled-test, not the bare marker: a checked-but-empty entry
        # from before the transient/empty distinction is offered once more
        "done": _has_reception,
    },
    "wikidata": {
        "label": "On-record facts",
        "blurb": "Source novel and named awards, looked up rather than "
                 "summarised. No model involved.",
        "done": _has_wikidata,
    },
    "omdb": {
        "label": "Ratings & awards (OMDb)",
        "blurb": "Critic scores and award totals.",
        "done": lambda raw: bool(raw.get("omdb_checked") or not raw.get("imdb_id")),
    },
}


def _key_tail(cache_key: str) -> str:
    """The part of ``raw:{media_type}:{X}`` that is X.

    Titles contain colons ("Code Geass: Lelouch of the Rebellion"), so the
    split is bounded rather than greedy.
    """
    key = cache_key
    if key.startswith("v2:"):
        key = key[3:]
    parts = key.split(":", 2)
    return parts[2] if len(parts) == 3 else ""


def _live_raw_entries(cache) -> list[dict]:
    """Every live raw entry for a video category, deduplicated by title."""
    cur = cache.conn.execute(
        """
        SELECT cache_key, response FROM api_cache
        WHERE (cache_key LIKE 'v2:raw:%' OR cache_key LIKE 'raw:%')
          AND expires_at > datetime('now')
        """
    )
    seen, out = set(), []
    for cache_key, resp in cur.fetchall():
        try:
            raw = json.loads(resp)
        except Exception:
            continue
        title = raw.get("title")
        # raw entries carry TMDB's vocabulary ("tv"); the rest of the app says
        # "show". Normalising here keeps that detail out of four call sites.
        mtype = {"tv": "show"}.get(raw.get("media_type"), raw.get("media_type"))
        if not title or mtype not in VIDEO:
            continue
        key = (title, mtype)
        if key in seen:
            continue
        seen.add(key)
        raw["_media_type"] = mtype
        # The identifier this row is FILED under, which is not reliably
        # derivable from what the row contains. Entries are keyed by the
        # library's title while the stored "title" is the enriched one, so
        # "Frieren: Beyond Journey's End" holds a row titled "Sousou no
        # Frieren", and "Dan Da Dan" one titled "DAN DA DAN". A top-up that
        # rebuilds the key from title[:40] misses both, finds no row to write,
        # and reports done — leaving the title outstanding for ever.
        raw["_cache_id"] = _key_tail(cache_key)
        out.append(raw)
    return out


def coverage(cache=None) -> dict:
    """Per-source completeness over the live library.

    Returns ``{"total": N, "sources": [{key, label, blurb, done, missing,
    pct, offer}]}``. ``offer`` is the whole point: it says whether a manual
    catch-up is still worth showing.
    """
    from src.cache.metadata_cache import MetadataCache
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        entries = _live_raw_entries(cache)
        total = len(entries)
        sources = []
        for key, spec in SOURCES.items():
            try:
                done = sum(1 for raw in entries if spec["done"](raw))
            except Exception as e:
                logger.debug("[backfill] coverage for %s failed: %s", key, e)
                continue
            pct = (100.0 * done / total) if total else 100.0
            sources.append({
                "key": key,
                "label": spec["label"],
                "blurb": spec["blurb"],
                "done": done,
                "missing": total - done,
                "pct": round(pct, 1),
                "offer": total > 0 and pct < THRESHOLD_PCT,
            })
        return {"total": total, "threshold_pct": THRESHOLD_PCT,
                "sources": sources,
                "any_offer": any(s["offer"] for s in sources)}
    finally:
        if owns:
            cache.close()


def pending(source: str, cache=None) -> list[dict]:
    """The titles still missing ``source``, as top-up arguments."""
    spec = SOURCES.get(source)
    if not spec:
        return []
    from src.cache.metadata_cache import MetadataCache
    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        return [{
            "title": raw["title"],
            "media_type": raw["_media_type"],
            "tmdb_id": raw.get("tmdb_id"),
            "tvdb_id": raw.get("tvdb_id"),
            "imdb_id": raw.get("imdb_id"),
            "year": raw.get("year"),
            "cache_id": raw.get("_cache_id"),
            "anilist_id": raw.get("anilist_id"),
            "anidb_id": raw.get("anidb_id"),
        } for raw in _live_raw_entries(cache) if not spec["done"](raw)]
    finally:
        if owns:
            cache.close()


async def run_source(source: str, *, limit: int = 0, cache=None,
                     task=None, should_stop=None, on_progress=None,
                     width: int = WIDTH) -> dict:
    """Catch one source up. ``limit=0`` means no ceiling.

    ``should_stop`` is polled between titles so a UI cancel or a signal lands
    within one title rather than at the end of a multi-hour run; everything
    written so far stays written, and re-running resumes.

    ``width`` is how many titles are in flight at once — see WIDTH.

    ``on_progress(done, total, added)`` is for callers with no Activity view to
    write to. The significance walk is seven hours long on a full library, and
    with the app stopped — which is the right way to run it — ``task`` is None
    and the run is completely silent. A caller that prints cannot tell a slow
    job from a hung one; this lets it.
    """
    from src.cache.metadata_cache import MetadataCache
    from src.services.media_enricher import topup_significance, topup_omdb
    from src.services.reception import topup_reception
    from src.services.wikidata import topup_wikidata

    owns = cache is None
    if owns:
        cache = MetadataCache()
    try:
        if source == "external_ids":
            # Bulk by nature: one library fetch answers thousands of titles,
            # so it does not walk the per-title path below.
            from src.services.external_ids import harvest
            return await harvest(cache, limit=limit, task=task,
                                 should_stop=should_stop,
                                 on_progress=on_progress)
        if source not in ("significance", "reception", "wikidata", "omdb"):
            return {"error": f"unknown source {source!r}"}
        todo = pending(source, cache=cache)
        if limit:
            todo = todo[:limit]

        async def _one(t):
            # cache_id first: the key the row is actually filed under beats any
            # key rebuilt from its contents.
            ids = dict(tmdb_id=t["tmdb_id"], tvdb_id=t["tvdb_id"],
                       anilist_id=t.get("anilist_id"),
                       anidb_id=t.get("anidb_id"),
                       cache_id=t.get("cache_id"))
            if source == "significance":
                return await topup_significance(t["title"], t["media_type"],
                                                cache=cache, year=t["year"], **ids)
            if source == "reception":
                return await topup_reception(t["title"], t["media_type"],
                                             cache=cache, year=t["year"], **ids)
            if source == "wikidata":
                return await topup_wikidata(t["title"], t["media_type"],
                                            imdb_id=t["imdb_id"], cache=cache, **ids)
            if not t["imdb_id"]:
                return False      # OMDb is keyed on the IMDb id, nothing to ask
            return await topup_omdb(t["title"], t["media_type"],
                                    imdb_id=t["imdb_id"], cache=cache, **ids)

        added = seen = 0
        remaining = iter(todo)

        def _tick(n, t):
            if n % 10 and n != len(todo):
                return
            if task is not None:
                from src.services.task_monitor import task_monitor
                task_monitor.update(task, processed=n, total=len(todo),
                                    message=f"{t['title'][:40]} — +{added} so far")
            if on_progress is not None:
                on_progress(n, len(todo), added)

        # A few titles in flight at once. Measured on the significance walk:
        # 0.6s of Wikipedia and 2.0s of model per title, strictly alternating,
        # so the GPU idled through every fetch and the network through every
        # distillation. Overlapping them hides the smaller behind the larger —
        # the same ~23% an offline Wikipedia copy would buy, without the copy.
        #
        # Deliberately small. Ollama serialises requests for one model on one
        # card, so a wider pool would not distil any faster; it would only
        # queue, and press harder on services that asked us to be gentle.
        async def _worker():
            nonlocal added, seen
            while True:
                if should_stop and should_stop():
                    return
                try:
                    t = next(remaining)   # single-threaded, no await: no race
                except StopIteration:
                    return
                try:
                    added += bool(await _one(t))
                except Exception as e:
                    logger.debug("[backfill] %s failed for %r: %s",
                                 source, t["title"], e)
                seen += 1
                _tick(seen, t)

        await asyncio.gather(*(_worker() for _ in range(max(1, width))))
        return {"source": source, "visited": seen, "added": added}
    finally:
        if owns:
            cache.close()
