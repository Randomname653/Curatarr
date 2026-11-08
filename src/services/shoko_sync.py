"""
Curatarr — finished anime episodes reported to Shoko as watched.

Shoko keeps watched state per Shoko user and a key acts as its user
(services/shoko_client), so every configured key is one user: its Shoko
user's ``PlexUsernames`` (or its own name) names the Curatarr user whose
plays it gets. A single admin key with no such name gets the Curatarr
admin's plays - the one-person install.

What is reported: watch_history rows of category anime, completed, not
Spotify. Each row is a play; the ledger (shoko_watch_sync) holds what
Shoko made of it, so a play goes out once.

Which Shoko episode: the media server's file for the played item, looked
up in Shoko by file name and confirmed by size (Plex and Shoko mount the
same disk under different roots, so only the tail of the path is shared);
without a file match, the item's TMDB id through Shoko's TMDB links.
Season and episode numbers never pick the Shoko episode: AniDB splits
what TVDB and TMDB call one show into one entry per season. They only find
the media server's CURRENT copy of a played episode whose key is gone (a
replaced file, a series moved to another library: 726 of 859 plays on the
2026-10-25 history), by series title, season and number; that copy's file
then goes through the same file match, so Shoko still decides.

How it is written: ``PUT /Episode/{id}/UserData`` with the play's own date.
A play no later than Shoko's last play of that episode (plus the window in
which two rows are one viewing, series_progress.SAME_VIEW_WINDOW_H) is
"already" there and left alone; a later one counts one more playback.

What none of that places, the owner pins per series and season
(services/shoko_pins): a link to an AniDB anime, or skip. A pin wins.

Whether Shoko passes a watched episode on to the AniDB MyList depends on
Shoko's own MyList settings, not on Curatarr.

    python -m src.services.shoko_sync --plan      what a run would do, no writes
"""
from __future__ import annotations

import asyncio
import logging
import posixpath
from datetime import datetime, timedelta
from typing import Optional

from src.services import shoko_client
from src.services.series_progress import SAME_VIEW_WINDOW_H

logger = logging.getLogger(__name__)

BATCH = 400                    # plays per user per run; the next tick goes on
UNMATCHED_RETRY = timedelta(days=7)   # Shoko may import the file later
UNMATCHED_ATTEMPTS = 5
_STOP_AFTER_FAILURES = 5       # Shoko stopped answering mid-run
_SAME_VIEW = timedelta(hours=SAME_VIEW_WINDOW_H)


# ── users ────────────────────────────────────────────────────────────────────

def _names(raw) -> set[str]:
    if isinstance(raw, (list, tuple)):
        parts = raw
    else:
        parts = str(raw or "").replace(";", ",").split(",")
    return {str(p).strip().casefold() for p in parts if str(p).strip()}


def match_user(shoko_user: dict, users: list[dict], single_key: bool) -> Optional[int]:
    """The Curatarr user id a Shoko user stands for, or None.

    ``users``: [{"id", "name", "is_admin"}] of the active Curatarr users."""
    plex = _names(shoko_user.get("PlexUsernames"))
    own = (shoko_user.get("Username") or "").strip().casefold()
    by_name = {(u.get("name") or "").strip().casefold(): u["id"] for u in users}
    for n in plex:
        if n in by_name:
            return by_name[n]
    if own and own in by_name:
        return by_name[own]
    if single_key and shoko_user.get("IsAdmin"):
        admins = sorted(u["id"] for u in users if u.get("is_admin"))
        return admins[0] if admins else None
    return None


def _active_users() -> list[dict]:
    from src.database.connection import get_db_session
    from src.database.models import User
    with get_db_session() as db:
        return [{"id": u.id, "name": u.plex_username, "is_admin": bool(u.is_admin)}
                for u in db.query(User).filter(User.is_active == True).all()]  # noqa: E712


# ── plays ────────────────────────────────────────────────────────────────────

def _due_plays(user_id: int, limit: int, now: datetime) -> list[dict]:
    """This user's finished anime plays Shoko has not had yet, oldest first:
    never tried, or unmatched and due for another try."""
    from sqlalchemy import and_, or_
    from src.database.connection import get_db_session
    from src.database.models import ShokoWatchSync, WatchHistoryEntry as W
    with get_db_session() as db:
        rows = (db.query(W.id, W.plex_item_id, W.viewed_at, W.title, W.series_title,
                         ShokoWatchSync.attempts, W.season, W.episode)
                .outerjoin(ShokoWatchSync, ShokoWatchSync.watch_history_id == W.id)
                .filter(W.user_id == user_id, W.media_type == "anime",
                        W.completed == True,  # noqa: E712
                        or_(W.source.is_(None), W.source != "spotify"),
                        or_(ShokoWatchSync.id.is_(None),
                            and_(ShokoWatchSync.status == "unmatched",
                                 ShokoWatchSync.attempts < UNMATCHED_ATTEMPTS,
                                 ShokoWatchSync.last_attempt_at < now - UNMATCHED_RETRY)))
                .order_by(W.viewed_at.asc(), W.id.asc())
                .limit(limit).all())
    return [{"id": r[0], "item": r[1], "viewed_at": r[2],
             "title": (f"{r[4]} - {r[3]}" if r[4] else r[3]), "attempts": r[5] or 0,
             "series": r[4], "season": r[6], "episode": r[7]}
            for r in rows]


def _record(user_id: int, outcomes: list[dict], now: datetime) -> None:
    """Upsert the ledger rows of one batch."""
    from src.database.connection import get_db_session
    from src.database.models import ShokoWatchSync
    if not outcomes:
        return
    with get_db_session() as db:
        have = {r.watch_history_id: r for r in db.query(ShokoWatchSync).filter(
            ShokoWatchSync.watch_history_id.in_([o["id"] for o in outcomes])).all()}
        for o in outcomes:
            row = have.get(o["id"])
            if row is None:
                row = ShokoWatchSync(watch_history_id=o["id"], user_id=user_id, attempts=0)
                db.add(row)
            row.status = o["status"]
            row.shoko_episode_ids = ",".join(str(i) for i in o.get("episodes") or []) or None
            row.matched_by = o.get("how")
            row.attempts = (row.attempts or 0) + 1
            row.last_attempt_at = now
            if o["status"] == "sent":
                row.sent_at = now
        db.commit()


# ── which Shoko episode ──────────────────────────────────────────────────────

def _parts(path: str) -> list[str]:
    p = posixpath.normpath((path or "").replace("\\", "/"))
    return [x for x in p.split("/") if x and x != "."]


def _location_tails(f: dict) -> list[list[str]]:
    out = []
    for loc in f.get("Locations") or []:
        for key in ("RelativePath", "AbsolutePath"):
            if (loc or {}).get(key):
                out.append([x.casefold() for x in _parts(loc[key])])
    return out


def pick_files(candidates: list[dict], path: str, size: Optional[int]) -> list[dict]:
    """The Shoko file records that are this media-server file.

    The size decides when the server reported one; otherwise the longest
    shared path tail (folder names above the file) must single one out."""
    if not candidates:
        return []
    if size:
        return [c for c in candidates if c.get("Size") == size]
    want = [x.casefold() for x in _parts(path)]
    scored = []
    for c in candidates:
        best = 0
        for tail in _location_tails(c):
            n = 0
            while n < min(len(tail), len(want)) and tail[-1 - n] == want[-1 - n]:
                n += 1
            best = max(best, n)
        scored.append((best, c))
    top = max(s for s, _ in scored)
    winners = [c for s, c in scored if s == top]
    return winners if top >= 1 and len(winners) == 1 else []


async def resolve(sess: shoko_client.ShokoSession, item) -> tuple[list[int], Optional[str]]:
    """([Shoko episode ids], "path" | "tmdb" | None) for one media-server item."""
    for f in item.files or ():
        name = (_parts(f.path) or [""])[-1]
        if not name:
            continue
        files = pick_files(await sess.files_by_name(name), f.path, f.size_bytes)
        eps: list[int] = []
        for rec in files:
            for e in shoko_client.episode_ids_of_file(rec):
                if e not in eps:
                    eps.append(e)
        if eps:
            return eps, "path"
    tmdb = getattr(item.external_ids, "tmdb_id", None)
    if tmdb:
        if item.kind == "episode":
            eps = await sess.episodes_by_tmdb_episode(tmdb)
        elif item.kind == "movie":
            eps = await sess.episodes_by_tmdb_movie(tmdb)
        else:
            eps = []
        if eps:
            return eps, "tmdb"
    return [], None


def _norm_title(s: Optional[str]) -> str:
    import re
    import unicodedata
    # "High School D×D" became "High School DxD" in Plex (2026-11-08);
    # NFKD leaves the multiplication sign alone.
    s = unicodedata.normalize("NFKD", (s or "").casefold().replace("×", "x"))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^0-9a-z]+", " ", s).strip()


class CurrentCopies:
    """The media server's current episode for (series title, season,
    episode): every series of the show libraries is listed once per run,
    a series' seasons and episodes only when a play asks for them."""

    def __init__(self, adapter):
        self._ad = adapter
        self._series: Optional[dict] = None
        self._children: dict = {}

    async def _kids(self, key: str) -> list:
        if key not in self._children:
            try:
                self._children[key] = await self._ad.children(key)
            except Exception as e:  # noqa: BLE001 - one series, logged, run goes on
                logger.info("[shoko] children of %s unreadable: %s", key, e)
                self._children[key] = []
        return self._children[key]

    async def _index(self) -> dict:
        if self._series is None:
            self._series = {}
            try:
                for lib in await self._ad.libraries():
                    if lib.kind != "show":
                        continue
                    async for it in self._ad.items(lib, kinds=("series",)):
                        self._series.setdefault(_norm_title(it.title), []).append(it.key)
            except Exception as e:  # noqa: BLE001 - no fallback this run
                logger.info("[shoko] series listing failed: %s", e)
        return self._series

    async def episodes(self, series: Optional[str], season, episode) -> list:
        """Every current episode item that fits, one per same-named series."""
        if not series or season is None or episode is None:
            return []
        out = []
        for key in (await self._index()).get(_norm_title(series), []):
            for se in await self._kids(key):
                if se.kind != "season" or se.season_number != season:
                    continue
                for ep in await self._kids(se.key):
                    end = ep.episode_number_end or ep.episode_number
                    if ep.kind == "episode" and ep.episode_number is not None                             and ep.episode_number <= episode <= end:
                        out.append(ep)
        return out


async def _pinned(sess, pin: dict, p: dict, matches: dict) -> tuple[list[int], Optional[str]]:
    """The episodes an owner's link pin names: the pin's first episode is
    regular episode 1 of the pinned AniDB anime, the next one 2, and so on
    (a film: episode 1)."""
    anidb = pin.get("anidb_id")
    cache_key = ("pin", anidb)
    if cache_key not in matches:
        series = await sess.series_by_anidb(anidb) if anidb else None
        sid = ((series or {}).get("IDs") or {}).get("ID")
        matches[cache_key] = await sess.regular_episodes(sid) if isinstance(sid, int) else {}
    number = (p.get("episode") or 1) - int(pin.get("from_episode") or 1) + 1
    ep = matches[cache_key].get(number)
    return ([ep], "pin") if ep else ([], "pin")


async def _match(sess, adapter, copies: CurrentCopies, p: dict, items: dict,
                 matches: dict, pins: Optional[dict] = None) -> tuple[list[int], Optional[str]]:
    """The Shoko episodes of one play: the owner's pin first, then its own
    item, else the current copy. ([], "skip") when a pin says skip."""
    from src.services.shoko_pins import pin_for
    pin = pin_for(pins or {}, _norm_title(p.get("series") or p.get("title")), p.get("season"),
                  p.get("episode"))
    if pin and pin.get("action") == "skip":
        return [], "skip"
    if pin and pin.get("action") == "link":
        return await _pinned(sess, pin, p, matches)
    key = str(p["item"] or "")
    if key not in matches:
        item = await _item_of(adapter, key, items) if key else None
        matches[key] = await resolve(sess, item) if item else None
    if matches[key] is not None:
        return matches[key]
    alt = ("title", _norm_title(p.get("series")), p.get("season"), p.get("episode"))
    if alt not in matches:
        found: tuple[list[int], Optional[str]] = ([], None)
        for ep in await copies.episodes(p.get("series"), p.get("season"), p.get("episode")):
            eps, _how = await resolve(sess, ep)
            if eps:
                found = (eps, "title")
                break
        matches[alt] = found
    return matches[alt]


# ── writing ──────────────────────────────────────────────────────────────────

def next_user_data(state: dict, played_at: datetime, now: datetime) -> Optional[dict]:
    """The user data to PUT for one more play at ``played_at``, or None when
    Shoko already holds this play (its last play is that late or later)."""
    last = shoko_client.parse_time(state.get("LastPlayedAt"))
    count = int(state.get("PlaybackCount") or 0)
    if count > 0 and last is not None and played_at <= last + _SAME_VIEW:
        return None
    new = dict(state)
    new["PlaybackCount"] = count + 1
    new["LastPlayedAt"] = shoko_client.format_time(
        played_at if last is None else max(played_at, last))
    new["LastUpdatedAt"] = shoko_client.format_time(now)
    return new


class _Stop(Exception):
    """Shoko failed for one play. ``fatal``: it refuses the route or the
    write, so the run ends now; otherwise only after several in a row."""

    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


async def _report(sess, eps: list[int], played_at: datetime, states: dict,
                  now: datetime, plan: bool) -> str:
    """"sent" or "already" for one play; raises _Stop when Shoko fails."""
    sent = False
    for ep in eps:
        state = states.get(ep)
        if state is None:
            state = await sess.episode_user_data(ep)
            if state is None:
                raise _Stop(f"no user data for Shoko episode {ep}")
        new = next_user_data(state, played_at, now)
        if new is None:
            states[ep] = state
            continue
        if not plan:
            code = await sess.put_episode_user_data(ep, new)
            if code in (404, 405):
                raise _Stop("this Shoko does not take episode user data (needs Shoko 6)", fatal=True)
            if not 200 <= code < 300:
                raise _Stop(f"Shoko answered HTTP {code} for episode {ep}", fatal=True)
        states[ep] = new
        sent = True
    return "sent" if sent else "already"


async def _item_of(adapter, key: str, cache: dict):
    if key not in cache:
        try:
            cache[key] = await adapter.item(key)
        except Exception as e:  # noqa: BLE001 - one item, logged, run goes on
            logger.info("[shoko] item %s unreadable on the media server: %s", key, e)
            cache[key] = None
    return cache[key]


async def sync_user(sess, adapter, user_id: int, *, limit: int = BATCH,
                    plan: bool = False, now: Optional[datetime] = None) -> dict:
    """Report one user's due plays. Returns the counts and, in a plan, the
    first plays with what would happen to them."""
    from src.database.runner import run_read, run_write
    now = now or datetime.utcnow()
    from src.services.shoko_pins import load_pins
    plays = await run_read(_due_plays, user_id, limit, now)
    pins = await run_read(load_pins)
    counts = {"sent": 0, "already": 0, "unmatched": 0, "skipped": 0, "stopped": None}
    sample: list[dict] = []
    outcomes: list[dict] = []
    items: dict = {}
    matches: dict = {}
    states: dict = {}
    copies = CurrentCopies(adapter)
    failures = 0
    for p in plays:
        eps, how = await _match(sess, adapter, copies, p, items, matches, pins)
        if how == "skip":
            status, how = "skipped", "pin"
        elif not eps:
            status = "unmatched"
        else:
            try:
                status = await _report(sess, eps, p["viewed_at"], states, now, plan)
                failures = 0
            except _Stop as e:
                failures += 1
                if e.fatal or failures >= _STOP_AFTER_FAILURES:
                    counts["stopped"] = str(e)
                    break
                continue
        counts[status] += 1
        outcomes.append({"id": p["id"], "status": status, "episodes": eps, "how": how})
        if plan and len(sample) < 30:
            sample.append({"title": p["title"], "viewed_at": str(p["viewed_at"]),
                           "status": status, "episodes": eps, "matched_by": how})
    if not plan:
        await run_write(_record, user_id, outcomes, now)
    counts["plays"] = len(plays)
    if plan:
        counts["sample"] = sample
    return counts


async def sync_watched(task=None, *, plan: bool = False, limit: int = BATCH,
                       transport=None, adapter=None) -> dict:
    """One pass for every configured key. {"ok", "skipped"?, "users": {...}}.
    ``done`` is False while a user still has a full batch waiting, so the
    custodian goes on next tick."""
    if not shoko_client._configured():
        return {"ok": True, "skipped": "no_shoko", "done": True}
    if not plan and transport is None and not shoko_client.writes_allowed():
        return {"ok": True, "skipped": "writes_off", "done": True}
    from src.database.runner import run_read
    if adapter is None:
        from src.services.media_server import MediaServerNotConfigured, get_adapter
        try:
            adapter = get_adapter()
        except MediaServerNotConfigured as e:
            return {"ok": False, "error": str(e), "done": True}
    keys = shoko_client.api_keys()
    users = await run_read(_active_users)
    report: dict = {}
    done = True
    async with shoko_client.new_client(transport) as c:
        first = shoko_client.ShokoSession(c, keys[0])
        version = await first.version()
        if not version:
            return {"ok": False, "error": "Shoko did not answer", "done": True}
        major = shoko_client.server_major(version)
        if major is not None and major < 6:
            logger.warning("[shoko] server %s: the watched sync is built for Shoko 6",
                           (version.get("Server") or {}).get("Version"))
        taken: set[int] = set()
        for n, key in enumerate(keys, 1):
            sess = shoko_client.ShokoSession(c, key)
            su = await sess.current_user()
            if not su:
                report[f"key {n}"] = {"error": "key refused"}
                continue
            uid = match_user(su, users, single_key=len(keys) == 1)
            label = su.get("Username") or f"key {n}"
            if uid is None or uid in taken:
                report[label] = {"error": "no Curatarr user for this Shoko user"
                                 if uid is None else "a second key for the same user"}
                continue
            taken.add(uid)
            res = await sync_user(sess, adapter, uid, limit=limit, plan=plan)
            res["curatarr_user"] = uid
            report[label] = res
            if res.get("plays", 0) >= limit or res.get("stopped"):
                done = False
            if task is not None:
                from src.services.task_monitor import task_monitor
                task_monitor.update(task, message=f"{label}: {res['sent']} sent, "
                                                  f"{res['already']} already there, "
                                                  f"{res['unmatched']} without a Shoko episode")
    sent = sum(r.get("sent", 0) for r in report.values() if isinstance(r, dict))
    if sent and not plan:
        logger.info("[shoko] %d plays reported as watched", sent)
    return {"ok": True, "users": report, "done": done, "plan": plan}


def _main(argv: list[str]) -> int:
    import json
    plan = "--plan" in argv
    if not plan:
        print("usage: python -m src.services.shoko_sync --plan   (writes run in the app)")
        return 2
    res = asyncio.run(sync_watched(plan=True, limit=100))
    print(json.dumps(res, indent=1, default=str, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_main(sys.argv[1:]))
