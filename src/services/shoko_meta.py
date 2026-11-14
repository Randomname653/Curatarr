"""
Curatarr — what Shoko knows per anime episode (Shoko phase 2).

Shoko keeps AniDB's data locally: per episode the type (Episode, Special,
Credits, Trailer, Parody, Other), the number, the title and the rating
with its votes; per series the tags with weight and spoiler flags. This
module copies that for the series the household watches (a weekly
refresh, read-only, every call answered from Shoko's own database), and
reads it back for the curator:

- ``stop_point``: where a member left a series, when that episode is
  among the weakest of its season ("the weakest episode is the one you
  stopped at").

How a play finds its series: the watched sync's ledger (shoko_watch_sync)
holds the Shoko episode ids of every reported play, and an episode names
its parent series. Not by title: a title search found the wrong anime.

Measured on the live server, 2026-11-14 (15 watched series): 98 % of the
regular episodes carry a rating, but on few votes (median 16, 10th
percentile 7), so an episode is only named with MIN_VOTES votes and a
clear distance to its season's median.
"""
from __future__ import annotations

import json
import logging
import statistics
from datetime import datetime, timedelta
from typing import Optional

from src.database.connection import get_db_session
from src.services import shoko_client

logger = logging.getLogger(__name__)

REFRESH_DAYS = 7
MAX_SERIES_PER_RUN = 200
MAX_EPISODE_LOOKUPS = 500
MIN_VOTES = 5            # fewer votes on an episode: noise, not a verdict
MIN_RATED = 5            # a season ranks only with this many rated episodes
LOW_GAP = 1.0            # the stop's rating sits this far under the median
LOW_RANK = 2             # ...and among the season's weakest two


# ── refresh ─────────────────────────────────────────────────────────────────

def _ledger_episode_ids() -> list[int]:
    """Every Shoko episode a reported play landed on. Blocking."""
    from src.database.models import ShokoWatchSync
    out: set[int] = set()
    with get_db_session() as db:
        for (ids,) in db.query(ShokoWatchSync.shoko_episode_ids).filter(
                ShokoWatchSync.status.in_(("sent", "already")),
                ShokoWatchSync.shoko_episode_ids.isnot(None)):
            out.update(int(x) for x in str(ids).split(",") if x.strip().isdigit())
    return sorted(out)


def _stored() -> dict:
    """{shoko_series_id: (fetched_at, {episode ids})}. Blocking."""
    from src.database.models import ShokoSeriesMeta
    out = {}
    with get_db_session() as db:
        for sid, eps, at in db.query(ShokoSeriesMeta.shoko_series_id, ShokoSeriesMeta.episodes,
                                     ShokoSeriesMeta.fetched_at):
            try:
                ids = {e.get("id") for e in json.loads(eps or "[]")}
            except (TypeError, ValueError):
                ids = set()
            out[sid] = (at, ids)
    return out


def _save(series_id: int, info: dict, episodes: list, tags: list, now: datetime) -> None:
    from src.database.models import ShokoSeriesMeta
    with get_db_session() as db:
        row = db.query(ShokoSeriesMeta).filter(ShokoSeriesMeta.shoko_series_id == series_id).first()
        if row is None:
            row = ShokoSeriesMeta(shoko_series_id=series_id)
            db.add(row)
        row.anidb_id = (info or {}).get("anidb_id")
        row.title = (info or {}).get("title") or row.title
        row.episodes = json.dumps(episodes)
        row.tags = json.dumps(tags)
        row.fetched_at = now
        db.commit()


async def refresh(task=None, *, transport=None, now: Optional[datetime] = None) -> dict:
    """Copy Shoko's episode data and tags for the series the ledger's plays
    landed on: new series, and stored ones older than REFRESH_DAYS. Reads
    only. ``done`` is False while series are still waiting."""
    if not shoko_client._configured():
        return {"ok": True, "skipped": "no_shoko", "done": True}
    from src.database.runner import run_read, run_write
    now = now or datetime.utcnow()
    ep_ids = await run_read(_ledger_episode_ids)
    stored = await run_read(_stored)
    known = {e: sid for sid, (_at, ids) in stored.items() for e in ids}
    unknown = [e for e in ep_ids if e not in known]
    looked = fetched = 0
    series: set[int] = {known[e] for e in ep_ids if e in known}
    done = True

    def is_due(sid: int) -> bool:
        return (sid not in stored or not stored[sid][0]
                or now - stored[sid][0] >= timedelta(days=REFRESH_DAYS))

    async with shoko_client.new_client(transport) as c:
        sess = shoko_client.ShokoSession(c, shoko_client.api_keys()[0])
        handled: set[int] = set()

        async def fetch(sid: int) -> None:
            nonlocal fetched, done
            handled.add(sid)
            if fetched >= MAX_SERIES_PER_RUN:
                done = False
                return
            episodes = await sess.series_episode_meta(sid)
            if episodes is None:
                return                # Shoko did not answer: next run
            tags = await sess.series_tags(sid) or []
            info = await sess.series_info(sid)
            await run_write(_save, sid, info, episodes, tags, now)
            fetched += 1
            # Every episode of the series is placed now: the plays on its
            # other episodes need no lookup of their own (500 lookups for
            # 83 series stopped the first live run at 500 of 700 episodes).
            for ep in episodes:
                if isinstance(ep.get("id"), int):
                    known[ep["id"]] = sid
            if task is not None and fetched % 20 == 0:
                from src.services.task_monitor import task_monitor
                task_monitor.update(task, message=f"{fetched} series refreshed")

        for e in unknown:
            if e in known:
                continue
            if looked >= MAX_EPISODE_LOOKUPS:
                done = False
                break
            looked += 1
            sid = await sess.episode_series(e)
            if sid is None:
                continue
            series.add(sid)
            known[e] = sid
            if sid not in handled and is_due(sid):
                await fetch(sid)
        for sid in sorted(series):
            if sid not in handled and is_due(sid):
                await fetch(sid)
    summary = (f"{fetched} series refreshed ({len(series)} watched, "
               f"{looked} episodes placed)")
    if task is not None:
        from src.services.task_monitor import task_monitor
        task_monitor.update(task, message=summary)
        task.noop = fetched == 0 and looked == 0
    return {"ok": True, "done": done, "series": len(series), "fetched": fetched,
            "looked_up": looked, "summary": summary}


# ── read back ────────────────────────────────────────────────────────────────

def _regular_rated(episodes: list) -> list:
    return [e for e in episodes if e.get("type") == "Episode" and e.get("rating") is not None
            and (e.get("votes") or 0) >= MIN_VOTES]


def low_point(episodes: list, episode_id: int) -> Optional[dict]:
    """The episode ``episode_id`` when it is among its season's weakest:
    a regular episode, rated on MIN_VOTES votes, in the bottom LOW_RANK and
    LOW_GAP under the season's median. None otherwise (also for a recap or
    special: AniDB files those as Special, never as a ranked episode)."""
    rated = _regular_rated(episodes)
    if len(rated) < MIN_RATED:
        return None
    ep = next((e for e in rated if e.get("id") == episode_id), None)
    if ep is None:
        return None
    median = statistics.median(e["rating"] for e in rated)
    rank = 1 + sum(1 for e in rated if e["rating"] < ep["rating"])
    if rank > LOW_RANK or ep["rating"] > median - LOW_GAP:
        return None
    return {"episode": ep.get("number"), "title": ep.get("title") or None,
            "anidb_rating": ep["rating"], "votes": ep["votes"],
            "season_median": round(median, 2), "rank_from_bottom": rank, "of": len(rated)}


def stop_point(user_id: int, series_title: str) -> Optional[dict]:
    """Where ``user_id`` left ``series_title``, when that is one of its
    season's weakest episodes ({episode, title, anidb_rating, votes,
    season_median, rank_from_bottom, of, finished}); None when it is not,
    or nothing is known. The last play of the series that the watched sync
    placed on a Shoko episode. Blocking."""
    from src.database.models import ShokoSeriesMeta, ShokoWatchSync, WatchHistoryEntry as W
    with get_db_session() as db:
        row = (db.query(W.completed, ShokoWatchSync.shoko_episode_ids)
               .join(ShokoWatchSync, ShokoWatchSync.watch_history_id == W.id)
               .filter(W.user_id == user_id, W.series_title == series_title,
                       ShokoWatchSync.shoko_episode_ids.isnot(None))
               .order_by(W.viewed_at.desc()).first())
        if row is None:
            return None
        ids = [int(x) for x in str(row[1]).split(",") if x.strip().isdigit()]
        if not ids:
            return None
        for (eps,) in db.query(ShokoSeriesMeta.episodes):
            try:
                episodes = json.loads(eps or "[]")
            except (TypeError, ValueError):
                continue
            if any(e.get("id") == ids[-1] for e in episodes):
                if not _left_midway(episodes, ids[-1]):
                    return None
                low = low_point(episodes, ids[-1])
                return {**low, "finished": bool(row[0])} if low else None
    return None


def _left_midway(episodes: list, episode_id: int) -> bool:
    """A stop is somewhere inside the season. Not the first episode: one
    episode is trying a series, not leaving it (the opener rule since
    #312). Not the last regular one: that season is finished (the first
    live run named a finale "where you stopped")."""
    regular = [e for e in episodes if e.get("type") == "Episode" and isinstance(e.get("number"), int)]
    ep = next((e for e in regular if e.get("id") == episode_id), None)
    if ep is None or not regular:
        return False
    return 1 < ep["number"] < max(e["number"] for e in regular)


# ── CLI: ``python -m src.services.shoko_meta`` ──────────────────────────────

def _watched_series(user_id: int) -> list[str]:
    from src.database.models import ShokoWatchSync, WatchHistoryEntry as W
    with get_db_session() as db:
        return sorted({t for (t,) in db.query(W.series_title)
                       .join(ShokoWatchSync, ShokoWatchSync.watch_history_id == W.id)
                       .filter(W.user_id == user_id, W.series_title.isnot(None))})


def _main() -> int:
    """Refresh (reads Shoko only), then show every watched series whose
    last play is one of its season's weakest episodes."""
    import asyncio
    from src.database.connection import init_db
    init_db()
    res = asyncio.run(refresh())
    print(res.get("summary") or res)
    for title in _watched_series(1):
        low = stop_point(1, title)
        if low:
            print(f"{title}: stopped at episode {low['episode']} "
                  f"(AniDB {low['anidb_rating']} on {low['votes']} votes, season median "
                  f"{low['season_median']}, #{low['rank_from_bottom']} from the bottom of {low['of']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
