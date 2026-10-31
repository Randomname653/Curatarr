"""server_id_map: the id memory a rebind runs on (docs/JELLYFIN_DESIGN.md §8.2).

Every title, user and library is filed under a server-independent
``canonical_key`` with the id each server knows it by. The ACTIVE server's
column is written on every library walk (phase 1c: the Plex tech-profile
walk), so on a switch most of the old side is already known, and on a switch
BACK the old ids are still there to be re-verified instead of re-matched.

Rules:
- a row is never deleted here. A server id the walk now files under other
  canonical keys is cleared on the rows that no longer fit (that side only);
- one server id may stand for several canonical keys: Jellyfin files a
  double episode as one item (IndexNumberEnd), Plex lists two;
- two DIFFERENT server ids with the same canonical key in one walk (one film
  in a 4K and a 1080p library) are a duplicate: the first keeps the row, the
  rest are counted, and a rebind matches those copies by file;
- a ``file:`` key is only built when the item's library roots are known: an
  absolute path would never match the other server's mount point.
"""
from __future__ import annotations

import dataclasses
import logging
import unicodedata
from datetime import datetime
from typing import Iterable, Optional

from src.services.media_server.base import (
    Library, MediaItem, ServerKind, _norm_name, canonical_keys,
)

logger = logging.getLogger(__name__)

# Parents before children: a season, episode, album or track key chains to
# its parent's canonical key.
_ORDER = {"movie": 0, "series": 0, "artist": 0, "album": 1, "season": 1, "episode": 2, "track": 2}
_KEY_LEN = 512


def key_rank(key: str) -> int:
    """How strong a match on ``key`` is: an external id first, then a key
    chained to one (a series' id plus season and episode number), a file or
    a name last."""
    if key.startswith(("file:", "name:")):
        return 2
    return 1 if "/" in key else 0


def match_method(key: str) -> str:
    if key.startswith("file:"):
        return "file"
    if key.startswith("name:"):
        return "name"
    if key.startswith(("user:", "library:")):
        return "server"
    return "external_id"


def item_rows(items: Iterable[MediaItem], roots_by_library: dict, *,
              aliases: bool = True) -> list[tuple]:
    """``(entity_type, canonical_key, server_key)`` for every key of every
    item that has one (``canonical_keys``: the strongest first, then the
    other ids, the series chain, the file). ``aliases=False`` gives each
    item's strongest key only: one identity per item, which is what a
    conflict is counted against. Seasons the walk did not list come from
    their episodes (``parent_key`` + ``season_number``), so a walk of
    episodes alone still files every season. A double episode files its
    keys once per episode number."""
    items = sorted(items, key=lambda i: _ORDER.get(i.kind, 3))
    canon: dict = {}                     # server key -> its keys, strongest first
    rows, seasons = [], set()

    def keys_of(it: MediaItem, parents: list, roots: tuple) -> list:
        ks = [k for k in canonical_keys(it, parent_keys=parents, roots=roots)
              if not (k.startswith("file:") and not roots)]
        return ks if aliases else ks[:1]

    for it in items:
        roots = tuple(roots_by_library.get(it.library_key or "", ()))
        # An episode chains to its series; everything else to its parent.
        parents = canon.get((it.grandparent_key if it.kind == "episode" else it.parent_key) or "", [])
        keys = keys_of(it, parents, roots)
        if keys:
            canon.setdefault(it.key, keys)
            rows += [(it.kind, k, it.key) for k in keys]
            if it.kind == "season":
                seasons.add(it.key)
        if it.kind == "episode" and it.episode_number_end and it.episode_number is not None:
            for n in range(it.episode_number + 1, it.episode_number_end + 1):
                # The extra numbers have no ids of their own: the series chain
                # or the numbered file key, never the first episode's tvdb id.
                extra = dataclasses.replace(
                    it, episode_number=n, episode_number_end=None,
                    external_ids=dataclasses.replace(it.external_ids, tvdb_episode_id=None))
                rows += [("episode", k, it.key) for k in keys_of(extra, parents, roots)]
        if (it.kind == "episode" and it.parent_key and it.parent_key not in seasons
                and parents and it.season_number is not None):
            seasons.add(it.parent_key)
            season_keys = [f"{p}/s{it.season_number:02d}" for p in parents]
            if not aliases:
                season_keys = season_keys[:1]
            canon[it.parent_key] = season_keys
            rows += [("season", k, it.parent_key) for k in season_keys]
    return rows


def library_rows(libraries: Iterable[Library]) -> list[tuple]:
    out = []
    for lib in libraries:
        name = _norm_name(lib.title)
        if name:
            out.append(("library", f"library:{lib.kind}:{name}", lib.key))
    return out


def user_rows(users: Iterable[tuple]) -> list[tuple]:
    """``users`` is ``(curatarr users.id, server user id)`` pairs."""
    return [("user", f"user:{uid}", str(sid)) for uid, sid in users if uid and sid]


def upsert(db, server: ServerKind, rows: Iterable[tuple],
           now: Optional[datetime] = None) -> dict:
    """Blocking (run_write). Files ``rows`` under ``server``'s column and
    commits. Returns counts: added, updated, verified, released, duplicates."""
    from src.database.models import ServerIdMap as M
    col = "plex_id" if server == "plex" else "jellyfin_id"
    now = now or datetime.utcnow()
    counts = {"added": 0, "updated": 0, "verified": 0, "released": 0, "duplicates": 0}

    # One canonical key, one server id: the first of a walk wins.
    winner: dict = {}
    for etype, ckey, sid in rows:
        ck = unicodedata.normalize("NFC", ckey)[:_KEY_LEN]
        if (etype, ck) in winner:
            if winner[(etype, ck)] != sid:
                counts["duplicates"] += 1
            continue
        winner[(etype, ck)] = sid
    if not winner:
        return counts
    wanted: dict = {}                    # (etype, sid) -> canonical keys of this walk
    for (etype, ck), sid in winner.items():
        wanted.setdefault((etype, sid), set()).add(ck)

    existing = db.query(M).filter(M.entity_type.in_(sorted({e for e, _ in winner}))).all()
    by_key = {(m.entity_type, m.canonical_key): m for m in existing}
    for m in existing:
        sid = getattr(m, col)
        if sid and (m.entity_type, sid) in wanted and m.canonical_key not in wanted[(m.entity_type, sid)]:
            # This server id now files under other keys (it gained an external
            # id, its file moved): it no longer stands for this one.
            setattr(m, col, None)
            m.last_verified_at = now
            counts["released"] += 1
    for (etype, ck), sid in winner.items():
        row = by_key.get((etype, ck))
        if row is None:
            db.add(M(entity_type=etype, canonical_key=ck, match_method=match_method(ck),
                     last_verified_at=now, **{col: sid}))
            counts["added"] += 1
            continue
        if getattr(row, col) == sid:
            counts["verified"] += 1
        else:
            setattr(row, col, sid)
            counts["updated"] += 1
        row.last_verified_at = now
    db.commit()
    return counts


async def record_walk(server: ServerKind, items: list, *, adapter=None) -> dict:
    """File one library walk's items, plus the server's libraries and every
    Curatarr user, under ``server``'s column. The libraries come from the
    adapter: their roots make the ``file:`` keys mount-independent. Raises
    when the server or the DB does not answer; callers keep their own work."""
    from src.database.connection import get_db_session
    from src.database.models import User
    from src.database.runner import run_read, run_write
    if adapter is None:
        from src.services.media_server import get_adapter
        adapter = get_adapter()
    libraries = await adapter.libraries()
    roots = {lib.key: lib.roots for lib in libraries}

    def _users() -> list:
        with get_db_session() as db:
            return [(uid, sid) for uid, sid in db.query(User.id, User.plex_user_id).all()]

    rows = item_rows(items, roots) + library_rows(libraries) + user_rows(await run_read(_users))

    def _write() -> dict:
        with get_db_session() as db:
            return upsert(db, server, rows)

    counts = await run_write(_write)
    logger.info("[id-map] %s walk: %s", server, counts)
    return counts
