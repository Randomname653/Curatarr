"""Watch history from Jellyfin (docs/JELLYFIN_DESIGN.md §4, phase 3).

Curatarr's history is one row per play; Jellyfin core keeps only each item's
CURRENT state per user (played or not, how often, when last). Four witnesses
rebuild the plays, no plugin required:

- **A, live sessions** (``SessionTracker`` + ``run_session_poller``): /Sessions
  every ``JELLYFIN_SESSION_POLL_S``; a session that ends past 80 % is a play,
  with its device. A restart in the same session after 80 % is a play too.
- **B, the Webhook plugin** (``jellyfin_hook``): a hint, re-read here.
- **C, the delta poll** (``poll_user``): play counts and last-played dates
  against the last poll's snapshot (``ServerPlayState``); when a count rose
  by more than one, Jellyfin's own activity log (no plugin) dates the
  extra plays.
- **D, the Playback Reporting plugin** (``import_report``): per-play history
  from before Curatarr, imported once per user, and later the plays of every
  stretch Curatarr was not watching (``jellyfin_gaps``, written by tier A).

All of them write through ``record_plays``, which keys a play on (user, item)
and merges two reports less than 20 minutes apart: the richer witness wins
field by field (hook > session > report > poll > marked), so a play seen by A
and later by C stays one row.
"""
from __future__ import annotations

import asyncio
import json
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
_REPORT_UPTO = "jellyfin_report_upto:{uid}"    # the end of the last gap read for this user
_POLL_SEEDED = "jellyfin_poll_seeded:{uid}"
_LAST_POLL = "jellyfin_last_poll:{uid}"
_SESSIONS_SEEN = "jellyfin_sessions_seen"
# Tier A's heartbeat and the stretches it missed. Curatarr may run a few hours
# a day on a PC: while it is off (or asleep, or cut off from Jellyfin) no
# session is seen, and Jellyfin itself keeps only each item's current state.
# A gap is read back from the Playback Reporting plugin (tier D), and the
# delta poll runs at once instead of at the next daily sync.
_ALIVE = "jellyfin_sessions_alive"
_GAPS = "jellyfin_gaps"
ALIVE_EVERY = timedelta(minutes=5)
# Without a plugin the times of repeat plays in a gap come from Jellyfin's
# activity log, 30 days by default; two weeks of absence leave room
# (docs/MEDIA_SERVERS.md tells the owner to run Curatarr that often).
LONG_GAP = timedelta(days=14)
GAP_MIN = timedelta(minutes=15)    # above the quiet poll's 5 minutes
MAX_GAPS = 50


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


_poller: Optional[asyncio.Task] = None


def start_session_poller() -> None:
    """Start tier A once (the lifespan on a Jellyfin install, the media
    server switch towards Jellyfin)."""
    global _poller
    if _poller is None or _poller.done():
        from src.services.bg_tasks import track_task
        _poller = track_task(run_session_poller(), name="jellyfin_sessions")


async def stop_session_poller() -> None:
    """Stop tier A (the media server switch: before the re-key, and for
    good when the install moves to Plex)."""
    global _poller
    task, _poller = _poller, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001 - a stopped poller's last error is moot
            pass


async def run_session_poller() -> None:
    """Tier A, for the life of the process: one cheap /Sessions read per
    interval, slower after a quiet day. Every failure is logged and the loop
    goes on - a Jellyfin restart must not end live tracking."""
    from src.config import settings
    from src.services.media_server import get_adapter
    tracker = SessionTracker()
    last_activity = datetime.utcnow()
    try:
        last_ok = _parse(await _read_state(_ALIVE))
    except Exception as e:  # noqa: BLE001 - without the last heartbeat the gap is left to the daily sync; logged
        logger.warning("[jellyfin] last live-session heartbeat not read: %s", e)
        last_ok = None
    beat = None
    while True:
        now = datetime.utcnow()
        sessions = []
        try:
            adapter = get_adapter()
            sessions = await adapter.live_sessions()
            if last_ok is not None and now - last_ok > GAP_MIN:
                await note_gap(last_ok, now)
            last_ok = now
            if beat is None or now - beat >= ALIVE_EVERY:
                await _note_state(_ALIVE, now.isoformat())
                beat = now
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


def _parse(raw: Optional[str]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _gaps(raw: Optional[str]) -> list:
    try:
        out = [(datetime.fromisoformat(a), datetime.fromisoformat(b)) for a, b in json.loads(raw or "[]")]
    except (ValueError, TypeError):
        return []
    return sorted(out)


async def note_gap(start: datetime, end: datetime) -> None:
    """Remember a stretch tier A missed and catch it up in the background:
    the delta poll for every member, and the plugin's plays where it is
    installed (``sync_history``)."""
    gaps = _gaps(await _read_state(_GAPS)) + [(start, end)]
    await _note_state(_GAPS, json.dumps([[a.isoformat(), b.isoformat()] for a, b in gaps[-MAX_GAPS:]]))
    logger.info("[jellyfin] no live sessions seen %s - %s; catching up", start, end)
    if end - start > LONG_GAP:
        logger.warning("[jellyfin] Curatarr was away for %d days: every play still counts, but "
                       "Jellyfin's activity log (30 days by default) may no longer date repeat plays "
                       "from the start of that stretch; run Curatarr at least every two weeks",
                       (end - start).days)
    from src.services.bg_tasks import track_task
    track_task(sync_history(), name="jellyfin_gap_catchup")


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
            # The poll's date-only plays are a count: all of them sit at the
            # previous poll's time, and merging them would make three plays
            # one. The poll has already left out what other witnesses filed.
            counted = r["source"] == "jellyfin_poll" and r["date_only"]
            near = [] if counted else (db.query(W).filter(
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


SWITCH_MARKS = "switch_jellyfin_marks"    # app_state: what a move to Jellyfin marked played
_SWITCH_SLACK_S = 5 * 60                   # the Jellyfin host's clock against this one


def drop_switch_marks(plays: list, raw: Optional[str]) -> list:
    """The plays that are not the played marks the media server switch set
    on its move here (rebind.sync_watched_state). Jellyfin stamps a mark
    with the moment it was set, and a server still scanning lists the item
    as played only later: a poll after the switch read the mark as a new
    play beside the history the switch carried over (2026-10-31, 133 marks
    on a test server mid-scan, 4 of them visible to the played filter)."""
    import json
    try:
        note = json.loads(raw) if raw else None
        keys = set(note["keys"])
        lo = float(note["from"]) - _SWITCH_SLACK_S
        hi = float(note["to"]) + _SWITCH_SLACK_S
    except (ValueError, TypeError, KeyError):
        return plays
    from calendar import timegm
    return [p for p in plays if not (p.item_key in keys and p.played_at is not None
                                     and lo <= timegm(p.played_at.timetuple()) <= hi)]


async def record_plays(plays: list, adapter, *, min_share: Optional[float] = None) -> dict:
    """Write ``plays`` (any tier) as watch-history rows. Plays of users who
    never signed in to Curatarr, of items in no mapped library, or of an
    ignored library are skipped, as the Plex sync skips them. ``min_share``
    drops plays whose played length is below that share of the runtime
    (tier D's rows are every start, not every play)."""
    from src.database.runner import run_read, run_write
    from src.services.app_state import get_state
    if plays:
        plays = drop_switch_marks(plays, await run_read(get_state, SWITCH_MARKS))
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


def _known_finished(user_id: int, keys: list) -> set:
    """The items this user has a finished play of on record, any source."""
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    out = set()
    with get_db_session() as db:
        for i in range(0, len(keys), 500):
            out.update(k for (k,) in db.query(W.plex_item_id).filter(
                W.user_id == user_id, W.completed.is_(True),
                W.plex_item_id.in_(keys[i:i + 500])).distinct())
    return out


def _seen_since(user_id: int, since: datetime, keys: list) -> dict:
    """When the richer Jellyfin witnesses (hook, session, plugin) filed plays
    of these items after ``since``: item -> [times]."""
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    out: dict = {}
    with get_db_session() as db:
        for i in range(0, len(keys), 500):
            for k, at in db.query(W.plex_item_id, W.viewed_at).filter(
                    W.user_id == user_id, W.plex_item_id.in_(keys[i:i + 500]),
                    W.source.in_(("jellyfin_hook", "jellyfin_session", "jellyfin_report")),
                    W.viewed_at > since):
                out.setdefault(k, []).append(at)
    return out


async def _playback_log(adapter, since: datetime) -> list:
    """The activity log's playback stops, or [] when it cannot be read (an
    older server, a key without admin rights): the extra plays stay
    date-only then, as before."""
    try:
        return await adapter.playback_log(since=since)
    except Exception as e:  # noqa: BLE001 - the log only dates plays the counts already prove; logged
        logger.info("[jellyfin] activity log not read, extra plays stay date-only: %s", e)
        return []


def _log_plays(log: list, user_key: str, st, *, after: datetime, taken: list) -> list:
    """Stop times of ``st``'s item by this user after ``after`` that can be
    a play: start to stop spans 80 % of the runtime (a stop after a minute
    is a stop, not a play; without a start or a runtime it cannot be told
    and is left out), not within the merge window of a play already filed.
    Newest first."""
    out = []
    for e in log:
        if e["user_key"] != user_key or e["item_key"] != st.item_key or e["stopped_at"] <= after:
            continue
        if not (e["started_at"] and st.duration_ms):
            continue
        if (e["stopped_at"] - e["started_at"]).total_seconds() * 1000 < SESSION_PLAY_SHARE * st.duration_ms:
            continue
        if any(abs(e["stopped_at"] - t) <= MERGE_WINDOW for t in taken + out):
            continue
        out.append(e["stopped_at"])
    return sorted(out, reverse=True)


def reset_poll(user_ids: list) -> int:
    """Blocking (run_write): forget the delta poll's snapshot, so the next
    poll is a first one again. The media server switch calls it when this
    install moves (back) to Jellyfin: the snapshot of an earlier Jellyfin
    period would read the played marks the switch just set as new plays."""
    from src.database.connection import get_db_session
    from src.database.models import AppState, ServerPlayState as S
    with get_db_session() as db:
        n = db.query(S).filter(S.user_id.in_(user_ids)).delete(synchronize_session=False)
        keys = [k.format(uid=u) for u in user_ids for k in (_POLL_SEEDED, _LAST_POLL)]
        db.query(AppState).filter(AppState.key.in_(keys)).delete(synchronize_session=False)
        db.commit()
    return n


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
    # A first poll only learns "played at some point": for an item with a
    # finished play on record - carried over from Plex by the switch, then
    # marked played here by it - that is nothing new, and was a twin row.
    known = set() if seeded else await run_read(_known_finished, user_id,
                                                [st.item_key for st in states])
    # A count up by n is n plays, but the live sessions, the webhook or the
    # plugin may have filed some of them since the last poll already (a
    # track played five times today): only the rest are new.
    seen = (await run_read(_seen_since, user_id, last, [st.item_key for st in states])
            if (seeded and last) else {})
    log = None                           # the activity log, read once and only when needed
    plays, updates = [], []
    for st in states:
        prev = snap.get(st.item_key)
        updates.append((st.item_key, st.play_count, st.last_played_at))
        at = st.last_played_at
        if not seeded:
            if st.item_key not in known:
                plays.append(Play(user_key=user_key, item_key=st.item_key, played_at=at or now,
                                  source="marked", completed=True, date_only=at is None))
            continue
        if prev is None:
            new = max(1, st.play_count)
        else:
            new = st.play_count - prev[0]
            if new <= 0 and at and (prev[1] is None or at > prev[1] + timedelta(minutes=1)):
                new = 1                   # marked played again without a count change
        had = seen.get(st.item_key, [])
        new -= len(had)
        if new <= 0:
            continue
        dated = []
        if not had:
            plays.append(Play(user_key=user_key, item_key=st.item_key, played_at=at or now,
                              source="jellyfin_poll", completed=True, date_only=at is None))
            new -= 1
            if at:
                dated.append(at)
        if new > 0 and last:
            if log is None:
                log = await _playback_log(adapter, last)
            for t in _log_plays(log, user_key, st, after=last, taken=dated + had)[:new]:
                plays.append(Play(user_key=user_key, item_key=st.item_key, played_at=t,
                                  source="jellyfin_poll", completed=True))
                new -= 1
        for _ in range(new):
            plays.append(Play(user_key=user_key, item_key=st.item_key, played_at=last or now,
                              source="jellyfin_poll", completed=True, date_only=True))
    res = await record_plays(plays, adapter)
    await run_write(_save_snapshot, user_id, updates, now)
    await _note_state(_LAST_POLL.format(uid=user_id), now.isoformat())
    if not seeded:
        await _note_state(_POLL_SEEDED.format(uid=user_id), "1")
    return {**res, "items": len(states), "first": not seeded}


# ── ratings (design D6) ──────────────────────────────────────────────────────

def _store_ratings(user_id: int, rated: list) -> dict:
    """Replace this user's Jellyfin-sourced music ratings (Plex stars carried
    over by a rebind have source NULL and stay)."""
    from src.database.connection import get_db_session
    from src.database.models import PlexRating
    with get_db_session() as db:
        mine = {r.plex_item_id: r for r in db.query(PlexRating).filter(
            PlexRating.user_id == user_id, PlexRating.source == "jellyfin_like").all()}
        others = {k for (k,) in db.query(PlexRating.plex_item_id).filter(
            PlexRating.user_id == user_id, PlexRating.source.is_(None)).all()}
        added = changed = 0
        keep = set()
        for r in rated:
            if r["key"] in others:
                continue                     # a Plex star rating on the same item wins
            keep.add(r["key"])
            row = mine.get(r["key"])
            if row is None:
                db.add(PlexRating(user_id=user_id, plex_item_id=r["key"], media_type=r["kind"],
                                  rating=r["rating"], rated_at=r["rated_at"],
                                  artist_name=(r["artist"] or "")[:512] or None, source="jellyfin_like"))
                added += 1
            elif row.rating != r["rating"]:
                row.rating = r["rating"]
                changed += 1
        removed = 0
        for key, row in mine.items():
            if key not in keep:
                db.delete(row)
                removed += 1
        db.commit()
    return {"added": added, "changed": changed, "removed": removed}


async def sync_ratings(adapter, user_id: int, user_key: str) -> dict:
    from src.database.runner import run_write
    rated = await adapter.rated_music(ServerUser(key=user_key, name=""))
    return await run_write(_store_ratings, user_id, rated)


# ── tier D ───────────────────────────────────────────────────────────────────

async def import_report(adapter, user_id: int, user_key: str, now: Optional[datetime] = None) -> dict:
    """Tier D for one user: once, the Playback Reporting plugin's plays from
    before Curatarr; after that, its plays inside the gaps tier A noted
    (``note_gap``) that this user has not had read yet. Without the plugin
    this records that and returns."""
    from src.services.media_server.jellyfin_adapter import PluginMissing
    now = now or datetime.utcnow()
    key = _REPORT_DONE.format(uid=user_id)
    done = await _read_state(key)
    if done == "absent":
        return {"skipped": "no plugin"}
    upto_key = _REPORT_UPTO.format(uid=user_id)
    if done:
        upto = _parse(await _read_state(upto_key))
        gaps = [(a, b) for a, b in _gaps(await _read_state(_GAPS)) if upto is None or b > upto]
        if not gaps:
            return {"skipped": "done"}
        try:
            plays = await adapter.plays(ServerUser(key=user_key, name=""), since=gaps[0][0])
        except PluginMissing:
            await _note_state(key, "absent")
            return {"plugin": False}
        inside = [p for p in plays if any(a <= p.played_at <= b for a, b in gaps)]
        res = await record_plays(inside, adapter, min_share=SESSION_PLAY_SHARE)
        await _note_state(upto_key, gaps[-1][1].isoformat())
        return {**res, "plugin": True, "gaps": len(gaps)}
    try:
        plays = await adapter.plays(ServerUser(key=user_key, name=""))
    except PluginMissing:
        await _note_state(key, "absent")
        return {"plugin": False}
    res = await record_plays(plays, adapter, min_share=SESSION_PLAY_SHARE)
    await _note_state(key, "1")
    await _note_state(upto_key, now.isoformat())     # earlier gaps are inside this import
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
            try:
                per_user[uid]["ratings"] = await sync_ratings(adapter, uid, key)
            except Exception as e:  # noqa: BLE001 - the plays are stored; ratings retry next sync; logged
                logger.warning("[jellyfin] ratings of user %s not synced: %s", uid, e)
                per_user[uid]["ratings"] = {"error": type(e).__name__}
        if synced and followup:
            # The flag scheduler.job_plex_sync sets for Plex: a fresh value,
            # so a follow-up already running does not clear these plays.
            await _note_state("plex_followup_pending", datetime.utcnow().isoformat())
        return {"synced": synced, "merged": merged, "users": per_user}


# The item type watch history stores per category (as plex_sync's
# _HISTORY_TYPE_BY_CATEGORY): episodes for shows, tracks for music.
_HISTORY_KIND = {"movie": "movie", "show": "episode", "anime": "episode", "music": "track"}


async def live_item_keys(adapter=None) -> dict:
    """``{"keys": set}`` of every item history can point at, from every
    mapped library, for the orphan cleanup; ``{"error": ...}`` unless EVERY
    library was read in full and none answered empty (an unmounted share
    looks exactly like that)."""
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig
    from src.database.runner import run_read
    from src.services.media_server import get_adapter
    from src.services.media_server.base import normalize_jellyfin_id

    def _mapped() -> list:
        with get_db_session() as db:
            return [(c.plex_section_key, c.media_category) for c in db.query(LibraryConfig).all()
                    if c.media_category != "ignore"]

    adapter = adapter or get_adapter()
    try:
        libs = {lib.key: lib for lib in await adapter.libraries()}
    except Exception as e:  # noqa: BLE001 - reported to the caller, which hides nothing
        return {"error": f"Could not list the Jellyfin libraries: {e}"}
    keys, empty = set(), []
    for sec, cat in await run_read(_mapped):
        kind = _HISTORY_KIND.get(cat or "")
        lib = libs.get(normalize_jellyfin_id(sec) or "")
        if not kind or lib is None:
            return {"error": f"Library {sec} ({cat}) is not a Jellyfin library this server has "
                             "- refusing to hide history"}
        try:
            found = {k async for r in adapter.raw_items(lib, kinds=(kind,))
                     if (k := normalize_jellyfin_id(r.get("Id")))}
        except Exception as e:  # noqa: BLE001 - an unread library aborts: its plays would read as orphaned
            logger.warning("[jellyfin] orphan cleanup: library %s unreadable: %s", lib.title, e)
            return {"error": f"Could not read library {lib.title}: {e}"}
        if not found:
            empty.append(lib.title)
        keys |= found
    if empty:
        return {"error": f"Library {', '.join(empty)} returned no items (unmounted share or "
                         "Jellyfin still scanning?) - refusing to hide history"}
    if not keys:
        return {"error": "No library items read - refusing to hide history"}
    return {"keys": keys}


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
