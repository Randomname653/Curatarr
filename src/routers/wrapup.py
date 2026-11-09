"""
Curatarr - Wrap-Up Router

Every household member's own wrap-up (replaces the admin Curation Report).
Facts are computed live — cheap SQL, reused for 15 minutes, frozen for a
closed year — and the curator's story is written in a background task and
replayed from the wrapups table (services/wrapup_story).
"""
import asyncio
import logging
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException

from src.database.models import User
from src.routers.auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter()

_FRESH = timedelta(minutes=15)
_POSTER_MAX = 16


def _period(scope: str, period: str, zone=None):
    """The period, its window on the member's clock (``zone``: week and
    month run midnight to midnight there); 400 for an unknown one."""
    from src.services.wrapup_facts import SCOPES, resolve_period
    if scope not in SCOPES:
        raise HTTPException(400, "unknown scope")
    try:
        return resolve_period(period, zone=zone)
    except ValueError:
        raise HTTPException(400, "unknown period")


async def _zone(user):
    from src.database.runner import run_read
    from src.services.user_time import user_zone_by_id
    return await run_read(user_zone_by_id, user.id)


def poster_wants(facts: dict) -> dict:
    """{title: (media_type, tmdb_id)} for every title a tile or chapter may
    show artwork for, most important first."""
    want: dict = {}

    def add(title, media_type, tmdb_id=None):
        if title and media_type and title not in want:
            want[title] = (media_type, tmdb_id)

    for t in facts.get("top") or []:
        add(t["title"], t["media_type"], t.get("tmdb_id"))
    for key in ("contrarian", "dismissal", "comfort"):
        x = facts.get(key) or {}
        add(x.get("title"), x.get("media_type"))
    d = (facts.get("consensus") or {}).get("duel") or {}     # the "you and the crowd" slide
    add(d.get("title"), d.get("media_type"))
    for key in ("in_48h", "deepest_sitting"):
        x = (facts.get("binge") or {}).get(key) or {}
        add(x.get("series"), x.get("media_type"))
    cu = facts.get("curatarr") or {}
    d = cu.get("dust_collector") or {}
    add(d.get("title"), d.get("media_type"), d.get("tmdb_id"))
    for c in facts.get("comebacks") or []:
        for t in facts.get("top") or []:
            if t["title"] == c["title"]:
                add(c["title"], t["media_type"], t.get("tmdb_id"))
    return dict(list(want.items())[:_POSTER_MAX])


def artist_mbids(names: list) -> dict:
    """{artist name: MusicBrainz id} from the plays' own artist_mbid, else the
    artist's raw enrichment profile. Blocking — call via ``run_read``.
    A name alone picks Deezer's most popular artist of that name; the id
    picks the one in the library."""
    from collections import Counter
    from src.database import orphaned
    from src.database.connection import get_db_session
    from src.database.models import WatchHistoryEntry as W
    out: dict = {}
    if not names:
        return out
    with get_db_session() as db:
        orphaned.include(db)        # plays of media gone from Plex count
        seen: dict = {}
        for name, mbid in (db.query(W.series_title, W.artist_mbid)
                           .filter(W.media_type == "music", W.series_title.in_(names),
                                   W.artist_mbid.isnot(None))):
            seen.setdefault(name, Counter())[mbid] += 1
        out = {n: c.most_common(1)[0][0] for n, c in seen.items()}
    missing = [n for n in names if n not in out]
    if missing:
        from src.cache.metadata_cache import MetadataCache
        mc = MetadataCache()
        try:
            for n in missing:
                raw = (mc.get_cache(f"raw:music:{n[:40]}") or {}).get("response") or {}
                if isinstance(raw, dict) and raw.get("mbid"):
                    out[n] = raw["mbid"]
        finally:
            mc.close()
    return out


async def attach_posters(facts: dict) -> None:
    """facts["posters"] = {title: url} through the recommendations poster path
    (24 h in-memory cache, id-first). Rows that already carry a poster (a
    deletion proposal, a cached recommendation) keep their own."""
    from src.routers.recommendations import _fetch_tmdb
    from src.services.enrichment_state import log_topup_failure

    from src.database.runner import run_read

    posters = dict(facts.get("posters") or {})
    want = {k: v for k, v in poster_wants(facts).items() if k not in posters}
    try:
        mbids = await run_read(artist_mbids, [t for t, (mt, _) in want.items() if mt == "music"])
    except Exception as e:  # noqa: BLE001 - a name search is the fallback
        logger.debug("[wrapup] artist ids not read: %s", e)
        mbids = {}
    sem = asyncio.Semaphore(4)

    async def one(title, media_type, tmdb_id):
        async with sem:
            try:
                poster, _ = await _fetch_tmdb(title, media_type, tmdb_id=tmdb_id,
                                              mbid=mbids.get(title))
                return title, poster
            except Exception as e:
                log_topup_failure(logger, "[wrapup] poster", title, e)
                return title, None

    for title, url in await asyncio.gather(*(one(t, mt, tid) for t, (mt, tid) in want.items())):
        if url:
            posters[title] = url
    facts["posters"] = posters


def _final(p, row: dict) -> bool:
    """Whether stored facts are the closed period's for good: the period is
    over AND they were computed after it ended. A week opened on Wednesday
    is closed by Monday, but its Wednesday facts are not the week's."""
    at = (row or {}).get("facts_at")
    return bool(p.closed and at and at >= p.end)


async def current_facts(user: User, scope: str, period: str) -> tuple:
    """(facts, stored row) — the stored facts while fresh, else recomputed,
    decorated with artwork and saved."""
    from src.database.runner import run_read, run_write
    from src.services.wrapup_facts import FACTS_VERSION, build_wrapup_facts
    from src.services.wrapup_store import load, save_facts

    p = _period(scope, period, await _zone(user))
    if period.startswith("season:"):
        # the anime season's "finished" reads the series' aired episodes
        from src.services.proactive_messages import _get_sonarr_index
        await _get_sonarr_index()
    row = await run_read(load, user.id, scope, period)
    facts = row and row.get("facts")
    if facts and facts.get("facts_version") != FACTS_VERSION:
        facts = None          # built on older watch data: a closed year thaws once
    if facts and (_final(p, row) or (row.get("facts_at") and datetime.utcnow() - row["facts_at"] < _FRESH)):
        return facts, row
    facts = await run_read(build_wrapup_facts, user.id, scope, period,
                           is_admin=bool(user.is_admin))
    if not facts.get("empty"):
        await attach_posters(facts)
        await run_write(save_facts, user.id, scope, period, facts)
    return facts, row


@router.get("")
async def get_wrapup(scope: str = "all", period: str = "30d",
                     user: User = Depends(get_current_user)):
    from src.database.runner import run_read
    from src.services.wrapup_facts import available
    from src.services.wrapup_insights import mine

    from src.services.household_compare import compare

    facts, row = await current_facts(user, scope, period)
    story = row and row.get("story")
    return {
        "facts": facts,
        # what is worth saying, ranked (services/wrapup_insights)
        "insights": mine(facts),
        "story": story,
        "story_stale": bool(story) and row.get("story_hash") != facts.get("facts_hash"),
        "story_at": row and row.get("story_at"),
        "story_source": row and row.get("story_source"),
        "generating": _running_task(user.id, scope, period),
        "quotes": await run_read(_quotes_allowed, user.id),
        # opt-in, worked out on every open and never stored
        # (services/household_compare)
        "household": await run_read(compare, user.id, scope, period, facts),
        "milestones": await run_read(_milestones, user.id, period),
        "decades_prev": await run_read(_decades_prev, user.id, scope, period),
        **(await run_read(available, user.id)),
    }


def _decades_prev(user_id: int, scope: str, period: str):
    """The decade shares of the year before, for a year's wrap-up whose
    previous year was built; None otherwise."""
    import re
    from src.services.wrapup_store import load
    m = re.fullmatch(r"year:(\d{4})", period or "")
    if not m:
        return None
    f = (load(user_id, scope, f"year:{int(m.group(1)) - 1}") or {}).get("facts") or {}
    return (f.get("eras") or {}).get("decades") or None


def _milestones(user_id: int, period: str) -> dict:
    """The member's milestones (services/milestones): those reached in this
    period (the story's slide), the whole history (the page's timeline),
    with each one's phrase, and the kinds and the ones switched off."""
    from src.services import milestones as ms
    from src.services.user_time import user_zone_by_id
    from src.services.wrapup_facts import resolve_period
    p = resolve_period(period, zone=user_zone_by_id(user_id))

    def dress(m):
        return {**m, "phrase": ms.phrase(m)}
    return {"period": [dress(m) for m in ms.in_period(user_id, p.start, p.end)],
            "history": [dress(m) for m in ms.history(user_id)],
            "kinds": list(ms.KINDS), "off": sorted(ms.kinds_off(user_id))}


@router.post("/milestones/kinds")
async def set_milestone_kind(body: dict, user: User = Depends(get_current_user)):
    """Switch one kind of milestone on or off for this member."""
    from src.database.runner import run_write
    from src.services import milestones as ms
    kind = str(body.get("kind") or "")
    if kind not in ms.KINDS:
        raise HTTPException(400, "unknown milestone kind")
    off = await run_write(ms.set_kind, user.id, kind, bool(body.get("on")))
    return {"off": sorted(off)}


def _quotes_allowed(user_id: int) -> bool:
    from src.services.wrapup_writer import quotes_allowed
    return quotes_allowed(user_id)


def _task_id(user_id: int, scope: str, period: str) -> str:
    return f"wrapup-{user_id}-{scope}-{period}"


def _running_task(user_id: int, scope: str, period: str):
    from src.services.task_monitor import task_monitor
    tid = _task_id(user_id, scope, period)
    return next((t["id"] for t in task_monitor.get_running() if t["id"] == tid), None)


@router.post("/generate")
async def start_wrapup(body: dict, user: User = Depends(get_current_user)):
    """Have the curator write (or rewrite) this wrap-up in the background.
    One generation per (user, scope, period) at a time; the page follows it
    through /status/{task_id}."""
    from src.services.bg_tasks import track_task
    from src.services.task_monitor import task_monitor
    from src.services.wrapup_story import PHASES, generate_wrapup

    scope = str(body.get("scope") or "all")
    period = str(body.get("period") or "30d")
    _period(scope, period, await _zone(user))
    # No await between this check and create(): on the one event loop the
    # check-and-claim is atomic, and the task registry dies with the process,
    # so a crash leaves no stale claim behind (an app_state lock would).
    running = _running_task(user.id, scope, period)
    if running:
        return {"task_id": running}
    tid = _task_id(user.id, scope, period)
    task = task_monitor.create(f"Wrap-up: {scope} · {period}", "wrapup",
                               total=len(PHASES), task_id=tid)
    task_monitor.start(task)

    track_task(generate_wrapup(user.id, scope, period, bool(user.is_admin), task), name=tid)
    return {"task_id": tid}


@router.get("/status/{task_id}")
def status(task_id: str, user: User = Depends(get_current_user)):
    """Where the generation is: the phase index the ticker lights up."""
    from src.services.task_monitor import task_monitor
    if not task_id.startswith(f"wrapup-{user.id}-"):
        raise HTTPException(404, "No such wrap-up task")
    t = next((x for x in task_monitor.get_all() if x["id"] == task_id), None)
    if t is None:
        raise HTTPException(404, "No such wrap-up task")
    logs = t.get("logs") or []
    return {"status": t["status"], "phase": t["processed"], "total": t["total"],
            "message": logs[-1]["msg"] if logs else None, "error": t["error"]}


@router.get("/prefs")
async def get_prefs(user: User = Depends(get_current_user)):
    """The user's wrap-up choices: may the curator quote their own debate
    messages back to them (opt-out — a quote only reaches its author), and
    may they be compared with the household (opt-in — that reaches others)."""
    from src.database.runner import run_read
    from src.services.household_compare import consented
    from src.services.wrapup_writer import quotes_allowed
    return {"quotes": await run_read(quotes_allowed, user.id),
            "household": await run_read(consented, user.id)}


@router.post("/prefs")
async def set_prefs(body: dict, user: User = Depends(get_current_user)):
    from src.database.runner import run_read, run_write
    from src.services.household_compare import consented, set_consent
    from src.services.wrapup_writer import quotes_allowed, set_quotes_allowed
    if "quotes" in body:
        await run_write(set_quotes_allowed, user.id, bool(body["quotes"]))
    if "household" in body:
        await run_write(set_consent, user.id, bool(body["household"]))
    return {"quotes": await run_read(quotes_allowed, user.id),
            "household": await run_read(consented, user.id)}


@router.post("/playlist")
async def push_playlist(body: dict, user: User = Depends(get_current_user)):
    """Put this wrap-up's titles into a playlist on the user's own account,
    on whichever media server this install runs (Plex or Jellyfin), and
    answer a link that plays it. A week's and a month's playlist is one
    rolling playlist each, named in the member's language
    (services/playlist_names), so a tap on the "Your week" card replaces last
    week's instead of piling up."""
    from src.database.runner import run_read
    from src.services.media_server import active_kind
    from src.services.playlist_names import name as playlist_name, names as playlist_names
    from src.services.plex_playlists import push_wrapup_playlist
    from src.services.server_links import attach_links
    from src.services.user_language import user_language

    scope = str(body.get("scope") or "all")
    period = str(body.get("period") or "30d")
    p = _period(scope, period, await _zone(user))
    # The stored facts, or fresh ones: a card's tap can come before the page
    # was ever opened for that week.
    facts, _row = await current_facts(user, scope, period)
    if not facts or facts.get("empty"):
        raise HTTPException(404, "Nothing played in this period yet")
    videos, seen = [], set()
    for t in facts.get("top") or []:
        if t["media_type"] != "music" and t["title"] not in seen:
            seen.add(t["title"])
            videos.append({"title": t["title"], "media_type": t["media_type"]})
    for key in ("comfort", "contrarian"):
        x = facts.get(key) or {}
        if x.get("title") and x.get("media_type") and x["title"] not in seen:
            seen.add(x["title"])
            videos.append({"title": x["title"], "media_type": x["media_type"]})
    tracks = [{"track": t["track"], "artist": t["artist"]}
              for t in (facts.get("music") or {}).get("top_tracks") or []]
    if not videos and not tracks:
        raise HTTPException(404, "Nothing in this wrap-up to put in a playlist")
    server = "Jellyfin" if active_kind() == "jellyfin" else "Plex"
    slot = {"week:": "wrapup_week", "month:": "wrapup_month"}.get(period[:period.find(":") + 1])
    name, aliases = None, ()
    if slot:
        name = playlist_name(slot, await run_read(user_language, user.id))
        aliases = tuple(sorted(playlist_names(slot) - {name}))
    try:
        out = await push_wrapup_playlist(user, p.label, videos, tracks, name=name, aliases=aliases)
    except Exception as e:
        logger.warning("[wrapup] playlist push for %s failed: %s", user.plex_username, e)
        raise HTTPException(502, f"{server} did not take the playlist — try again in a moment")
    if out.get("skipped") == "no_token":
        raise HTTPException(409, "Sign out and in again once, so Curatarr can write to your own Plex account")
    if out.get("skipped") == "no_jellyfin_account":
        # It fell through to "not found in Plex" (UX audit 2026-11-08, W4).
        raise HTTPException(409, "Your Curatarr account is not linked to a Jellyfin user yet - "
                                 "sign out and in again with Jellyfin once")
    if out.get("skipped") == "no_server_access":
        raise HTTPException(409, f"{server} does not let Curatarr write to your account on this server yet")
    if not out.get("pushed"):
        raise HTTPException(404, f"None of these titles were found in {server}")
    # A link that plays it (services/server_links); the key is the client's no longer.
    await attach_links(out["pushed"], playlist=True)
    for pl in out["pushed"]:
        pl.pop("key", None)
    return out
