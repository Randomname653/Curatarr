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
import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Above this, catching up by hand stops being worth a button: the remainder
# is a rounding error the daily ticks clear on their own.
THRESHOLD_PCT = 90.0

VIDEO = ("movie", "show", "anime")


def _has_significance(raw: dict) -> bool:
    from src.services.media_enricher import _SIG_PROMPT_VERSION
    return raw.get("significance_v") == _SIG_PROMPT_VERSION


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
        "done": lambda raw: bool(raw.get("reception_checked")),
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


def _live_raw_entries(cache) -> list[dict]:
    """Every live raw entry for a video category, deduplicated by title."""
    cur = cache.conn.execute(
        """
        SELECT response FROM api_cache
        WHERE (cache_key LIKE 'v2:raw:%' OR cache_key LIKE 'raw:%')
          AND expires_at > datetime('now')
        """
    )
    seen, out = set(), []
    for (resp,) in cur.fetchall():
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
        } for raw in _live_raw_entries(cache) if not spec["done"](raw)]
    finally:
        if owns:
            cache.close()


async def run_source(source: str, *, limit: int = 0, cache=None,
                     task=None, should_stop=None, on_progress=None) -> dict:
    """Catch one source up. ``limit=0`` means no ceiling.

    ``should_stop`` is polled between titles so a UI cancel or a signal lands
    within one title rather than at the end of a multi-hour run; everything
    written so far stays written, and re-running resumes.

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
        todo = pending(source, cache=cache)
        if limit:
            todo = todo[:limit]
        added = 0
        for i, t in enumerate(todo, 1):
            if should_stop and should_stop():
                break
            ids = dict(tmdb_id=t["tmdb_id"], tvdb_id=t["tvdb_id"])
            try:
                if source == "significance":
                    ok = await topup_significance(t["title"], t["media_type"],
                                                  cache=cache, year=t["year"], **ids)
                elif source == "reception":
                    ok = await topup_reception(t["title"], t["media_type"],
                                               cache=cache, year=t["year"], **ids)
                elif source == "wikidata":
                    ok = await topup_wikidata(t["title"], t["media_type"],
                                              imdb_id=t["imdb_id"], cache=cache, **ids)
                elif source == "omdb":
                    if not t["imdb_id"]:
                        continue      # OMDb is keyed on the IMDb id, nothing to ask
                    ok = await topup_omdb(t["title"], t["media_type"],
                                          imdb_id=t["imdb_id"], cache=cache, **ids)
                else:
                    return {"error": f"unknown source {source!r}"}
                added += bool(ok)
            except Exception as e:
                logger.debug("[backfill] %s failed for %r: %s", source, t["title"], e)
            if i % 10 == 0 or i == len(todo):
                if task is not None:
                    from src.services.task_monitor import task_monitor
                    task_monitor.update(task, processed=i, total=len(todo),
                                        message=f"{t['title'][:40]} — +{added} so far")
                if on_progress is not None:
                    on_progress(i, len(todo), added)
        return {"source": source, "visited": len(todo), "added": added}
    finally:
        if owns:
            cache.close()
