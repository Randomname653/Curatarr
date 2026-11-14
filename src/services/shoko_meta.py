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

- ``weak_opener``: a member left after episode 1 or 2, and episode 1 is
  among the season's weakest ("it gets better from here").
- ``season_note``: either of them as one line for the chat.

Measured on the live server, 2026-11-14 (15 watched series): 98 % of the
regular episodes carry a rating, but on few votes (median 16, 10th
percentile 7). So a rating is pulled toward its season's median by its
vote count (Bayesian shrinkage, PRIOR_VOTES): 4.0 on 6 votes counts for
less than 4.0 on 40. A hard vote cut instead named five series on the
owner's data and dropped two clear cases that sat just under it; this
names those seven (compared 2026-11-14, owner's choice).
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
MIN_VOTES = 3            # fewer votes on an episode: no rating at all
PRIOR_VOTES = 5          # the season median counts as this many votes
MIN_RATED = 5            # a season ranks only with this many rated episodes
LOW_GAP = 0.8            # the shrunk rating sits this far under the median
LOW_RANK = 2             # ...and among the season's weakest two


# ── tags: what a series is about ────────────────────────────────────────────
# AniDB's tag tree, ids stable (read on the live server 2026-11-14 by the
# Shoko session). Kept: elements 2611, themes 2607, content indicators 2604,
# dynamic 2605 and the rest of setting (place 2613 holds "space", "prison",
# "dungeon", "fictional location": content). Dropped:
TAG_ROOTS = {2610, 2611, 2607, 2605, 2604, 2606, 2609, 6173, 6151, 30, 2608, 2931, 2613, 2612}
# A tag goes when it or any ancestor is one of these (the node itself too:
# the live run showed "Earth" and "summer" under "season" slipping through
# a children-only check).
_DROP_BRANCHES = {
    2630,   # Earth: countries, continents, cities - "Japan" 600 on nearly every series
    2624,   # present (and "alternative present" under it): 600, says nothing
    6185,   # season: spring, summer, winter
    2606,   # target audience (shounen, seinen): metadata
    2609,   # source material (manga, novel): metadata
    6173,   # origin (Japanese production): metadata
    30,     # maintenance tags
    2931,   # unsorted
    6151,   # technical aspects (CGI, black and white) - but see _KEEP_IN_DROPPED
    2608,   # fetishes: never into a prompt by default
}
_KEEP_IN_DROPPED = {4616}        # "based on real events" hangs in technical aspects


def _ancestors(tag_id: int, tree: dict) -> list:
    out, seen, cur = [], {tag_id}, tree.get(tag_id)
    while isinstance(cur, int) and cur not in seen:
        out.append(cur)
        seen.add(cur)
        cur = tree.get(cur)
    return out


def tag_keeps(tag_id, tree: dict) -> bool:
    """Whether a tag says what a series is about: not a root name, and
    neither it nor any ancestor in a dropped branch. A chain that ends
    without a known root (59 newer AniDB tags have no parent yet) is kept."""
    if not isinstance(tag_id, int):
        return True
    if tag_id in _KEEP_IN_DROPPED:
        return True
    if tag_id in TAG_ROOTS or tag_id in _DROP_BRANCHES:
        return False
    return not any(a in _DROP_BRANCHES for a in _ancestors(tag_id, tree))


def classify_tags(tags: list, tree: dict) -> list:
    """Mark each of a series' tags ``keep``. Besides tag_keeps: a broader
    tag ("cast", "fictional location", "the arts") goes when a narrower one
    under it is on the series with at least its weight - the narrower one
    says it. A broader tag weighted above all of its narrower ones stays."""
    best_below: dict = {}
    for t in tags:
        if isinstance(t.get("id"), int):
            w = t.get("weight") or 0
            for a in _ancestors(t["id"], tree):
                best_below[a] = max(best_below.get(a, -1), w)
    for t in tags:
        below = best_below.get(t.get("id"), -1)
        t["keep"] = tag_keeps(t.get("id"), tree) and not below >= (t.get("weight") or 0)
    return tags


def series_tags_for(anidb_id=None, title: str = None, limit: int = 12) -> Optional[list]:
    """The tags of a Shoko series that say what it is about, strongest
    first (weight 0 means "applies, unweighted": last, not dropped), no
    spoilers. Found by AniDB id, else by title. None when Shoko has nothing
    on it. Blocking."""
    from src.database.models import ShokoSeriesMeta
    with get_db_session() as db:
        q = db.query(ShokoSeriesMeta.tags)
        row = None
        if anidb_id:
            row = q.filter(ShokoSeriesMeta.anidb_id == int(anidb_id)).first()
        if row is None and title:
            row = q.filter(ShokoSeriesMeta.title == title).first()
    if row is None:
        return None
    try:
        tags = json.loads(row[0] or "[]")
    except (TypeError, ValueError):
        return None
    good = [t for t in tags if t.get("keep", True) and not t.get("spoiler") and t.get("name")]
    good.sort(key=lambda t: -(t.get("weight") or 0))
    return [t["name"] for t in good[:limit]]


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
        tree: dict = {}             # the AniDB tag tree, read once per run
        tree_read = False

        async def fetch(sid: int) -> None:
            nonlocal fetched, done, tree, tree_read
            # Marked handled before the cap on purpose: a series over the cap
            # is left for this run, and done=False sends the next one to it.
            handled.add(sid)
            if fetched >= MAX_SERIES_PER_RUN:
                done = False
                return
            episodes = await sess.series_episode_meta(sid)
            if episodes is None:
                return                # Shoko did not answer: next run
            tags = await sess.series_tags(sid) or []
            if not tree_read:
                tree, tree_read = (await sess.tag_tree()) or {}, True
            classify_tags(tags, tree)
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

def non_regular_play_ids(user_id: int) -> set:
    """watch_history ids of ``user_id``'s plays that landed on no regular
    episode: a Special (where AniDB files recaps), Credits, a Trailer, a
    Parody. Plex/TVDB often number a recap as a regular episode (S01E13),
    so only the Shoko episode a play was placed on can tell. A play counts
    only when every episode it covers is known and none is regular.
    Blocking."""
    from src.database.models import ShokoSeriesMeta, ShokoWatchSync
    with get_db_session() as db:
        plays = db.query(ShokoWatchSync.watch_history_id, ShokoWatchSync.shoko_episode_ids).filter(
            ShokoWatchSync.user_id == user_id, ShokoWatchSync.shoko_episode_ids.isnot(None)).all()
        if not plays:
            return set()
        types: dict = {}
        for (eps,) in db.query(ShokoSeriesMeta.episodes):
            try:
                for e in json.loads(eps or "[]"):
                    if isinstance(e.get("id"), int):
                        types[e["id"]] = e.get("type") or ""
            except (TypeError, ValueError):
                continue
    out = set()
    for wid, ids in plays:
        eps = [int(x) for x in str(ids).split(",") if x.strip().isdigit()]
        if eps and all(e in types for e in eps) and not any(types[e] == "Episode" for e in eps):
            out.add(wid)
    return out

def _regular_rated(episodes: list) -> list:
    return [e for e in episodes if e.get("type") == "Episode" and e.get("rating") is not None
            and (e.get("votes") or 0) >= MIN_VOTES]


def _shrunk(ep: dict, median: float) -> float:
    """The rating pulled toward the season median by its vote count:
    (votes * rating + PRIOR_VOTES * median) / (votes + PRIOR_VOTES)."""
    v = ep.get("votes") or 0
    return (v * ep["rating"] + PRIOR_VOTES * median) / (v + PRIOR_VOTES)


def low_point(episodes: list, episode_id: int) -> Optional[dict]:
    """The episode ``episode_id`` when it is among its season's weakest:
    a regular episode, rated on MIN_VOTES votes, in the bottom LOW_RANK by
    its shrunk rating and that rating LOW_GAP under the season's median.
    None otherwise (also for a recap or special: AniDB files those as
    Special, never as a ranked episode). ``after_median``: the median of
    the rated episodes after it, None when none is."""
    rated = _regular_rated(episodes)
    if len(rated) < MIN_RATED:
        return None
    ep = next((e for e in rated if e.get("id") == episode_id), None)
    if ep is None:
        return None
    median = statistics.median(e["rating"] for e in rated)
    adj = _shrunk(ep, median)
    rank = 1 + sum(1 for e in rated if _shrunk(e, median) < adj)
    if rank > LOW_RANK or adj > median - LOW_GAP:
        return None
    after = [e["rating"] for e in rated
             if isinstance(e.get("number"), int) and isinstance(ep.get("number"), int)
             and e["number"] > ep["number"]]
    return {"episode": ep.get("number"), "title": ep.get("title") or None,
            "anidb_rating": ep["rating"], "votes": ep["votes"],
            "season_median": round(median, 2), "rank_from_bottom": rank, "of": len(rated),
            "after_median": round(statistics.median(after), 2) if after else None}


def _last_regular(user_id: int, series_title: str) -> Optional[tuple]:
    """(completed, episode dict, the series' episodes) of ``user_id``'s last
    play of ``series_title`` that the watched sync placed on a REGULAR
    Shoko episode: a recap special watched after it is no place in the
    season (Shoko phase 2, recaps). None when nothing is known. Blocking."""
    from src.database.models import ShokoSeriesMeta, ShokoWatchSync, WatchHistoryEntry as W
    with get_db_session() as db:
        plays = (db.query(W.completed, ShokoWatchSync.shoko_episode_ids)
                 .join(ShokoWatchSync, ShokoWatchSync.watch_history_id == W.id)
                 .filter(W.user_id == user_id, W.series_title == series_title,
                         ShokoWatchSync.shoko_episode_ids.isnot(None))
                 .order_by(W.viewed_at.desc()).limit(20).all())
        if not plays:
            return None
        series_of: dict = {}
        for (eps,) in db.query(ShokoSeriesMeta.episodes):
            try:
                episodes = json.loads(eps or "[]")
            except (TypeError, ValueError):
                continue
            for e in episodes:
                if isinstance(e.get("id"), int):
                    series_of[e["id"]] = (e, episodes)
    for completed, ids in plays:
        for ep_id in reversed([int(x) for x in str(ids).split(",") if x.strip().isdigit()]):
            hit = series_of.get(ep_id)
            if hit is not None and hit[0].get("type") == "Episode":
                return bool(completed), hit[0], hit[1]
    return None


def stop_point(user_id: int, series_title: str) -> Optional[dict]:
    """Where ``user_id`` left ``series_title``, when that is one of its
    season's weakest episodes (low_point's dict plus ``finished``); None
    when it is not, or nothing is known. Blocking."""
    last = _last_regular(user_id, series_title)
    if last is None:
        return None
    completed, ep, episodes = last
    if not _left_midway(episodes, ep["id"]):
        return None
    low = low_point(episodes, ep["id"])
    return {**low, "finished": completed} if low else None


def weak_opener(user_id: int, series_title: str) -> Optional[dict]:
    """``user_id`` left ``series_title`` after episode 1 or 2, and episode 1
    is among the season's weakest: the season gets better past where they
    turned back. low_point's dict for episode 1 plus ``left_after_episode``;
    None otherwise. Blocking."""
    last = _last_regular(user_id, series_title)
    if last is None:
        return None
    _completed, ep, episodes = last
    regular = [e for e in episodes if e.get("type") == "Episode" and isinstance(e.get("number"), int)]
    n = ep.get("number")
    if not isinstance(n, int) or n > 2 or not regular or n >= max(e["number"] for e in regular):
        return None
    first = next((e for e in regular if e["number"] == 1), None)
    low = low_point(episodes, first["id"]) if first else None
    # ...and the rest of the season holds up: past the opening it rates at
    # least at the season's median.
    if not low or low["after_median"] is None or low["after_median"] < low["season_median"]:
        return None
    return {**low, "left_after_episode": n}


def _weakest(rank: int, of: int) -> str:
    return f"{'the weakest' if rank == 1 else 'the second weakest'} of {of} rated episodes"


def season_note(user_id: int, series_title: str) -> Optional[str]:
    """One line for the chat when ``user_id`` left ``series_title`` at one of
    its season's weakest episodes, or after a weak first episode. None
    otherwise. Blocking."""
    def nums(low: dict) -> str:
        after = (f", episodes after it {low['after_median']}"
                 if low.get("after_median") is not None else "")
        return (f"AniDB {low['anidb_rating']} on {low['votes']} votes; "
                f"season median {low['season_median']}{after}")
    low = stop_point(user_id, series_title)
    if low:
        name = f" '{low['title']}'" if low.get("title") else ""
        return (f"left at episode {low['episode']}{name}, "
                f"{_weakest(low['rank_from_bottom'], low['of'])} ({nums(low)})")
    low = weak_opener(user_id, series_title)
    if low:
        return (f"left after episode {low['left_after_episode']}; episode 1 is "
                f"{_weakest(low['rank_from_bottom'], low['of'])} ({nums(low)}): "
                f"the season rates better past its opening")
    return None


def _left_midway(episodes: list, episode_id: int) -> bool:
    """A stop is somewhere inside the season. Not the first episode: one
    episode is trying a series, not leaving it (the opener rule since
    #312; a weak first episode is weak_opener's case). Not the last
    regular one: that season is finished (the first live run named a
    finale "where you stopped")."""
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
    watched = _watched_series(1)
    for title in watched:
        note = season_note(1, title)
        if note:
            print(f"{title}: {note}")
    print(f"plays on no regular episode (recap specials, credits...), not counted: "
          f"{len(non_regular_play_ids(1))}")
    from src.database.models import ShokoSeriesMeta
    with get_db_session() as db:
        named = [(t, a) for t, a in db.query(ShokoSeriesMeta.title, ShokoSeriesMeta.anidb_id)
                 .order_by(ShokoSeriesMeta.title).limit(8)]
    for t, a in named:
        print(f"tags {t}: {', '.join(series_tags_for(anidb_id=a, title=t) or [])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
