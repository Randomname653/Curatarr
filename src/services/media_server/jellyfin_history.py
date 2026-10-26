"""Watch history from Jellyfin (docs/JELLYFIN_DESIGN.md §4, phase 3).

Curatarr's history is one row per play; Jellyfin core keeps only each item's
CURRENT state per user (played or not, how often, when last). Four witnesses
rebuild the plays, no plugin required:

- **A, live sessions** (``SessionTracker`` + ``run_session_poller``): /Sessions
  every ``JELLYFIN_SESSION_POLL_S``; a session that ends past 80 % is a play,
  with its device. A restart in the same session after 80 % is a play too.
- **B, the Webhook plugin** (``jellyfin_hook``): a hint, re-read here.
- **C, the delta poll** (``poll_user``): play counts and last-played dates
  against the last poll's snapshot (``ServerPlayState``).
- **D, the Playback Reporting plugin** (``import_history_for_user``): per-play
  history from before Curatarr, imported once per user.

All of them write through ``record_plays``, which keys a play on (user, item)
and merges two reports less than 20 minutes apart: the richer witness wins
field by field (hook > session > report > poll > marked), so a play seen by A
and later by C stays one row.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Optional

from src.services.media_server.base import LiveSession, MediaItem, Play, ServerUser

logger = logging.getLogger(__name__)

SESSION_PLAY_SHARE = 0.80      # a session that got this far is a play (tier A, tier D)
REWIND_SHARE = 0.15            # ...and starting again below this is a second run
COMPLETED_SHARE = 0.90         # "finished", the rule plex_sync uses
MAX_PAUSE = timedelta(hours=4)
MERGE_WINDOW = timedelta(minutes=20)
QUIET_AFTER = timedelta(hours=24)
QUIET_POLL_S = 300
# How far before the last poll the next one reads again. A play started
# before a poll and finished after it can carry a last-played date from
# before that poll; the snapshot keeps a re-read from counting twice.
POLL_MARGIN = timedelta(hours=24)

# Richer witness first. Plays from other sources (Plex history, Spotify,
# Netflix) are never merged with these.
RANK = {"jellyfin_hook": 5, "jellyfin_session": 4, "jellyfin_report": 3,
        "jellyfin_poll": 2, "marked": 1}

_REPORT_DONE = "jellyfin_report_done:{uid}"    # "1" imported, "absent" no plugin
_POLL_SEEDED = "jellyfin_poll_seeded:{uid}"
_LAST_POLL = "jellyfin_last_poll:{uid}"
_SESSIONS_SEEN = "jellyfin_sessions_seen"


# ── tier A ───────────────────────────────────────────────────────────────────

@dataclass
class _Pair:
    user_key: str
    duration_ms: Optional[int]
    high_ms: int = 0
    paused_since: Optional[datetime] = None
    device: Optional[str] = None
    client: Optional[str] = None


@dataclass
class SessionTracker:
    """What each live session has played so far. In memory: a restart loses
    only plays in progress, and tier C picks those up."""
    pairs: dict = field(default_factory=dict)      # (session key, item key) -> _Pair

    def _play(self, key: tuple, p: _Pair, now: datetime) -> Optional[Play]:
        if not p.duration_ms or p.high_ms < SESSION_PLAY_SHARE * p.duration_ms:
            return None
        return Play(user_key=p.user_key, item_key=key[1], played_at=now, source="jellyfin_session",
                    position_ms=p.high_ms, duration_ms=p.duration_ms,
                    completed=p.high_ms >= COMPLETED_SHARE * p.duration_ms,
                    device=p.device, client=p.client)

    def observe(self, sessions: Iterable[LiveSession], now: datetime) -> list[Play]:
        """Feed one poll; returns the plays it completed."""
        out, live = [], set()
        for s in sessions:
            key = (s.session_key, s.item_key)
            live.add(key)
            p = self.pairs.get(key)
            if p is None:
                p = self.pairs[key] = _Pair(user_key=s.user_key, duration_ms=s.duration_ms)
            p.duration_ms = s.duration_ms or p.duration_ms
            p.device, p.client = s.device or p.device, s.client or p.client
            if (p.duration_ms and p.high_ms >= SESSION_PLAY_SHARE * p.duration_ms
                    and s.position_ms < REWIND_SHARE * p.duration_ms):
                # Started again in the same session: the first run was a play.
                done = self._play(key, p, now)
                if done:
                    out.append(done)
                p.high_ms = 0
            p.high_ms = max(p.high_ms, s.position_ms)
            if s.paused:
                p.paused_since = p.paused_since or now
                if now - p.paused_since > MAX_PAUSE:
                    done = self._play(key, p, now)
                    if done:
                        out.append(done)
                    self.pairs.pop(key)
                    live.discard(key)
            else:
                p.paused_since = None
        for key in [k for k in self.pairs if k not in live]:
            done = self._play(key, self.pairs.pop(key), now)
            if done:
                out.append(done)
        return out


async def run_session_poller() -> None:
    """Tier A, for the life of the process: one cheap /Sessions read per
    interval, slower after a quiet day. Every failure is logged and the loop
    goes on - a Jellyfin restart must not end live tracking."""
    from src.config import settings
    from src.services.media_server import get_adapter
    tracker = SessionTracker()
    last_activity = datetime.utcnow()
    while True:
        now = datetime.utcnow()
        sessions = []
        try:
            adapter = get_adapter()
            sessions = await adapter.live_sessions()
            plays = tracker.observe(sessions, now)
            if sessions:
                last_activity = now
            if plays:
                res = await record_plays(plays, adapter)
                logger.info("[jellyfin] %d play(s) from live sessions: %s", len(plays), res)
                await _note_state(_SESSIONS_SEEN, now.isoformat())
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 - the poller outlives a Jellyfin restart; logged at WARNING
            logger.warning("[jellyfin] live sessions not read: %s", e)
        quiet = not sessions and not tracker.pairs and now - last_activity > QUIET_AFTER
        await asyncio.sleep(QUIET_POLL_S if quiet else max(10, int(settings.JELLYFIN_SESSION_POLL_S)))


# ── writing plays ────────────────────────────────────────────────────────────

async def _note_state(key: str, value: str) -> None:
    from src.database.runner import run_write
    from src.services.app_state import set_state
    await run_write(set_state, key, value)


async def _read_state(key: str) -> Optional[str]:
    from src.database.runner import run_read
    from src.services.app_state import get_state
    return await run_read(get_state, key)


def _library_of(item: MediaItem, roots: dict) -> Optional[str]:
    """The library whose folder holds the item's file (longest root wins):
    ``/Items?Ids=`` does not say which library an item is in."""
    path = (item.files[0].path if item.files else "").replace("\\", "/")
    best, best_len = None, -1
    for lib_key, lib_roots in roots.items():
        for r in lib_roots:
            r = r.replace("\\", "/").rstrip("/")
            if r and (path == r or path.startswith(r + "/")) and len(r) > best_len:
                best, best_len = lib_key, len(r)
    return best


def _play_row(play: Play, item: MediaItem, series: Optional[MediaItem], category: str,
              user_id: int) -> dict:
    duration = play.duration_ms or item.duration_ms
    completed = bool(play.completed) or play.source in ("jellyfin_poll", "marked") or bool(
        play.position_ms and duration and play.position_ms >= COMPLETED_SHARE * duration)
    if play.source == "jellyfin_hook":
        completed = True                 # the re-read confirmed Played
    ids_from = series if (item.kind == "episode" and series) else item
    x = ids_from.external_ids
    music = item.kind == "track"
    return {
        "user_id": user_id, "plex_user_id": play.user_key, "plex_item_id": item.key,
        "title": item.title[:512], "media_type": category,
        "series_title": (item.grandparent_title or None) if item.kind in ("episode", "track") else None,
        "season": item.season_number if item.kind == "episode" else None,
        "episode": item.episode_number if item.kind == "episode" else None,
        "viewed_at": play.played_at, "duration_ms": duration,
        "view_offset_ms": play.position_ms, "completed": completed,
        "genres": ",".join(item.genres or (series.genres if series else ())) or None,
        "tmdb_id": x.tmdb_id, "source": play.source,
        "player": (play.device or "")[:64] or None, "platform": (play.client or "")[:64] or None,
        "album": (item.parent_title or "")[:512] or None if music else None,
        "date_only": True if play.date_only else None,
        "identity_key": (item.grandparent_key if item.kind == "episode" and item.grandparent_key else item.key),
        "identity_title": (item.grandparent_title if item.kind == "episode" else item.title) or item.title,
        "ids": {"tmdb_id": x.tmdb_id, "tvdb_id": x.tvdb_id, "imdb_id": x.imdb_id,
                "anilist_id": x.anilist_id, "anidb_id": x.anidb_id},
        "year": ids_from.year,
    }


def _write_plays(rows: list) -> dict:
    """Blocking (run_write): insert or merge each play, file its identity."""
    from src.database.connection import get_db_session
    from src.database.models import MediaIdentity, WatchHistoryEntry as W
    inserted = merged = 0
    with get_db_session() as db:
        for r in rows:
            at = r["viewed_at"]
            near = (db.query(W).filter(
                W.user_id == r["user_id"], W.plex_item_id == r["plex_item_id"],
                W.source.in_(sorted(RANK)),
                W.viewed_at >= at - MERGE_WINDOW, W.viewed_at <= at + MERGE_WINDOW).all())
            hit = min(near, key=lambda w: abs((w.viewed_at - at).total_seconds())) if near else None
            if hit is not None:
                richer = RANK.get(r["source"], 0) > RANK.get(hit.source or "", 0)
                if richer:
                    hit.source, hit.viewed_at = r["source"], at
                    hit.date_only = r["date_only"]
                    if r["view_offset_ms"] is not None:
                        hit.view_offset_ms = r["view_offset_ms"]
                for col in ("player", "platform", "duration_ms", "genres", "tmdb_id", "album"):
                    if r[col] is not None and (richer or getattr(hit, col) is None):
                        setattr(hit, col, r[col])
                hit.completed = bool(hit.completed) or r["completed"]
                merged += 1
                continue
            db.add(W(**{k: v for k, v in r.items()
                        if k not in ("identity_key", "identity_title", "ids", "year")}))
            ident = db.query(MediaIdentity).filter(MediaIdentity.plex_rating_key == r["identity_key"]).first()
            if ident is None:
                ident = MediaIdentity(plex_rating_key=r["identity_key"], media_type=r["media_type"],
                                      title=r["identity_title"][:500], year=r["year"])
                db.add(ident)
            for k, v in r["ids"].items():
                if v and not getattr(ident, k):
                    setattr(ident, k, v)
            db.flush()
            inserted += 1
        db.commit()
    return {"inserted": inserted, "merged": merged}


def _load_context() -> tuple[dict, dict]:
    """Blocking (run_read): Jellyfin user id -> Curatarr user id (active
    users), and library key -> category from the library mapping."""
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig, User
    with get_db_session() as db:
        users = {u.plex_user_id: u.id for u in db.query(User).filter(User.is_active == True).all()}  # noqa: E712
        cats = {c.plex_section_key: c.media_category for c in db.query(LibraryConfig).all()}
    return users, cats


async def record_plays(plays: list, adapter, *, min_share: Optional[float] = None) -> dict:
    """Write ``plays`` (any tier) as watch-history rows. Plays of users who
    never signed in to Curatarr, of items in no mapped library, or of an
    ignored library are skipped, as the Plex sync skips them. ``min_share``
    drops plays whose played length is below that share of the runtime
    (tier D's rows are every start, not every play)."""
    from src.database.runner import run_read, run_write
    if not plays:
        return {"inserted": 0, "merged": 0, "skipped": 0}
    users, cats = await run_read(_load_context)
    roots = {lib.key: lib.roots for lib in await adapter.libraries()}
    items: dict = {}

    async def item(key):
        if key and key not in items:
            items[key] = await adapter.item(key)
        return items.get(key)

    rows, skipped = [], 0
    for p in plays:
        uid = users.get(p.user_key)
        it = await item(p.item_key) if uid else None
        cat = cats.get(_library_of(it, roots) or "") if it else None
        if not uid or not it or not cat or cat == "ignore":
            skipped += 1
            continue
        dur = p.duration_ms or it.duration_ms
        if min_share and (not p.position_ms or not dur or p.position_ms < min_share * dur):
            skipped += 1
            continue
        series = await item(it.grandparent_key) if it.kind == "episode" else None
        rows.append(_play_row(p, it, series, cat, uid))
    res = await run_write(_write_plays, rows) if rows else {"inserted": 0, "merged": 0}
    return {**res, "skipped": skipped}


# ── tier C ───────────────────────────────────────────────────────────────────

def _load_snapshot(user_id: int) -> dict:
    from src.database.connection import get_db_session
    from src.database.models import ServerPlayState as S
    with get_db_session() as db:
        return {s.item_key: (s.play_count, s.last_played_at)
                for s in db.query(S).filter(S.user_id == user_id).all()}


def _save_snapshot(user_id: int, updates: list, now: datetime) -> None:
    from src.database.connection import get_db_session
    from src.database.models import ServerPlayState as S
    with get_db_session() as db:
        have = {s.item_key: s for s in db.query(S).filter(S.user_id == user_id).all()}
        for key, count, last in updates:
            row = have.get(key)
            if row is None:
                db.add(S(user_id=user_id, item_key=key, play_count=count, last_played_at=last, seen_at=now))
            else:
                row.play_count, row.last_played_at, row.seen_at = count, last, now
        db.commit()


async def poll_user(adapter, user_id: int, user_key: str, now: Optional[datetime] = None) -> dict:
    """Tier C for one user: what changed since the last poll becomes plays.
    The first poll knows only that items were played: one ``marked`` row per
    item at its last-played date (Wrap-Up leaves those out of time-of-day
    facts). A count up by n is n plays: the newest at the last-played date,
    the rest at the previous poll's time, flagged date-only."""
    now = now or datetime.utcnow()
    last_raw = await _read_state(_LAST_POLL.format(uid=user_id))
    last = datetime.fromisoformat(last_raw) if last_raw else None
    seeded = bool(await _read_state(_POLL_SEEDED.format(uid=user_id)))
    states = await adapter.user_states(ServerUser(key=user_key, name=""),
                                       since=(last - POLL_MARGIN) if (seeded and last) else None)
    from src.database.runner import run_read, run_write
    snap = await run_read(_load_snapshot, user_id)
    plays, updates = [], []
    for st in states:
        prev = snap.get(st.item_key)
        updates.append((st.item_key, st.play_count, st.last_played_at))
        at = st.last_played_at
        if not seeded:
            plays.append(Play(user_key=user_key, item_key=st.item_key, played_at=at or now,
                              source="marked", completed=True, date_only=at is None))
            continue
        if prev is None:
            new = max(1, st.play_count)
        else:
            new = st.play_count - prev[0]
            if new <= 0 and at and (prev[1] is None or at > prev[1] + timedelta(minutes=1)):
                new = 1                   # marked played again without a count change
        if new <= 0:
            continue
        plays.append(Play(user_key=user_key, item_key=st.item_key, played_at=at or now,
                          source="jellyfin_poll", completed=True, date_only=at is None))
        for _ in range(new - 1):
            plays.append(Play(user_key=user_key, item_key=st.item_key, played_at=last or now,
                              source="jellyfin_poll", completed=True, date_only=True))
    res = await record_plays(plays, adapter)
    await run_write(_save_snapshot, user_id, updates, now)
    await _note_state(_LAST_POLL.format(uid=user_id), now.isoformat())
    if not seeded:
        await _note_state(_POLL_SEEDED.format(uid=user_id), "1")
    return {**res, "items": len(states), "first": not seeded}


# ── tier D ───────────────────────────────────────────────────────────────────

async def import_report(adapter, user_id: int, user_key: str) -> dict:
    """Tier D for one user, once: the Playback Reporting plugin's plays from
    before Curatarr. Without the plugin this records that and returns."""
    from src.services.media_server.jellyfin_adapter import PluginMissing
    key = _REPORT_DONE.format(uid=user_id)
    if await _read_state(key):
        return {"skipped": "done"}
    try:
        plays = await adapter.plays(ServerUser(key=user_key, name=""))
    except PluginMissing:
        await _note_state(key, "absent")
        return {"plugin": False}
    res = await record_plays(plays, adapter, min_share=SESSION_PLAY_SHARE)
    await _note_state(key, "1")
    oldest = min((p.played_at for p in plays), default=None)
    return {**res, "plugin": True, "since": oldest.isoformat() if oldest else None}


def _user_rows(user_id: Optional[int] = None) -> list:
    from src.database.connection import get_db_session
    from src.database.models import User
    from src.services.media_server.base import is_jellyfin_key
    with get_db_session() as db:
        q = db.query(User.id, User.plex_user_id).filter(User.is_active == True)  # noqa: E712
        if user_id is not None:
            q = q.filter(User.id == user_id)
        return [(uid, key) for uid, key in q.all() if is_jellyfin_key(key)]


async def import_history_for_user(user_id: int, *, adapter=None) -> dict:
    """A new member's history (auth.py auto-onboarding): the plugin's
    backfill if there is one, then the first delta poll."""
    from src.database.runner import run_read
    from src.services.media_server import get_adapter
    adapter = adapter or get_adapter()
    rows = await run_read(_user_rows, user_id)
    if not rows:
        return {"error": "not a Jellyfin account"}
    uid, key = rows[0]
    return {"report": await import_report(adapter, uid, key),
            "poll": await poll_user(adapter, uid, key)}


_sync_lock = asyncio.Lock()


async def sync_history(*, adapter=None, followup: bool = True) -> dict:
    """The sync on a Jellyfin install (scheduler.job_plex_sync, startup, the
    Sync button): each member's backfill once, then the delta poll. Returns
    ``synced`` = new rows. New rows leave the custodian's follow-up flag
    (taste vectors, recommendations) unless the caller chains that work
    itself (``followup=False``, the scheduler)."""
    from src.database.runner import run_read
    from src.services.media_server import get_adapter
    if _sync_lock.locked():
        return {"busy": True, "reason": "a Jellyfin sync is already running"}
    async with _sync_lock:
        adapter = adapter or get_adapter()
        synced = merged = 0
        per_user = {}
        for uid, key in await run_read(_user_rows):
            try:
                rep = await import_report(adapter, uid, key)
                poll = await poll_user(adapter, uid, key)
            except Exception as e:  # noqa: BLE001 - one member's failure must not cost the others; logged
                logger.warning("[jellyfin] history of user %s not synced: %s", uid, e)
                per_user[uid] = {"error": type(e).__name__}
                continue
            synced += rep.get("inserted", 0) + poll.get("inserted", 0)
            merged += rep.get("merged", 0) + poll.get("merged", 0)
            per_user[uid] = {"report": rep, "poll": poll}
        if synced and followup:
            # The flag scheduler.job_plex_sync sets for Plex: a fresh value,
            # so a follow-up already running does not clear these plays.
            await _note_state("plex_followup_pending", datetime.utcnow().isoformat())
        return {"synced": synced, "merged": merged, "users": per_user}


async def walk_library(*, adapter=None) -> dict:
    """The daily library walk on Jellyfin: every item filed in the id memory
    (server_id_map), as the Plex tech walk does on Plex."""
    from src.services.media_server import get_adapter, id_map
    adapter = adapter or get_adapter()
    items = []
    for lib in await adapter.libraries():
        items += [it async for it in adapter.items(lib)]
    return await id_map.record_walk("jellyfin", items, adapter=adapter)


async def history_sources() -> dict:
    """Which witnesses this install has, for the UI badge (design §4.5)."""
    from src.services.app_state import get_state
    from src.database.runner import run_read

    def _read():
        from src.database.connection import get_db_session
        from src.database.models import AppState
        with get_db_session() as db:
            rows = db.query(AppState.key, AppState.value).filter(
                AppState.key.like("jellyfin_report_done:%")).all()
        return {k: v for k, v in rows}
    reports = await run_read(_read)
    hook = await run_read(get_state, "jellyfin_hook_seen")
    seen = await run_read(get_state, _SESSIONS_SEEN)
    recent = lambda iso: bool(iso) and datetime.utcnow() - datetime.fromisoformat(iso) < timedelta(days=7)
    return {"server": "jellyfin", "live_sessions": True, "sessions_seen_at": seen,
            "webhook": recent(hook), "webhook_seen_at": hook,
            "playback_reporting": any(v == "1" for v in reports.values())}
