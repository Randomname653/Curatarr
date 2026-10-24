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


def _period(scope: str, period: str):
    from src.services.wrapup_facts import SCOPES, resolve_period
    if scope not in SCOPES:
        raise HTTPException(400, "unknown scope")
    try:
        return resolve_period(period)
    except ValueError:
        raise HTTPException(400, "unknown period")


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


async def attach_posters(facts: dict) -> None:
    """facts["posters"] = {title: url} through the recommendations poster path
    (24 h in-memory cache, id-first). Rows that already carry a poster (a
    deletion proposal, a cached recommendation) keep their own."""
    from src.routers.recommendations import _fetch_tmdb
    from src.services.enrichment_state import log_topup_failure

    posters = dict(facts.get("posters") or {})
    want = {k: v for k, v in poster_wants(facts).items() if k not in posters}
    sem = asyncio.Semaphore(4)

    async def one(title, media_type, tmdb_id):
        async with sem:
            try:
                poster, _ = await _fetch_tmdb(title, media_type, tmdb_id=tmdb_id)
                return title, poster
            except Exception as e:
                log_topup_failure(logger, "[wrapup] poster", title, e)
                return title, None

    for title, url in await asyncio.gather(*(one(t, mt, tid) for t, (mt, tid) in want.items())):
        if url:
            posters[title] = url
    facts["posters"] = posters


async def current_facts(user: User, scope: str, period: str) -> tuple:
    """(facts, stored row) — the stored facts while fresh, else recomputed,
    decorated with artwork and saved."""
    from src.database.runner import run_read, run_write
    from src.services.wrapup_facts import FACTS_VERSION, build_wrapup_facts
    from src.services.wrapup_store import load, save_facts

    p = _period(scope, period)
    row = await run_read(load, user.id, scope, period)
    facts = row and row.get("facts")
    if facts and facts.get("facts_version") != FACTS_VERSION:
        facts = None          # built on older watch data: a closed year thaws once
    if facts and (p.closed or (row.get("facts_at") and datetime.utcnow() - row["facts_at"] < _FRESH)):
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

    facts, row = await current_facts(user, scope, period)
    story = row and row.get("story")
    return {
        "facts": facts,
        "story": story,
        "story_stale": bool(story) and row.get("story_hash") != facts.get("facts_hash"),
        "story_at": row and row.get("story_at"),
        "story_source": row and row.get("story_source"),
        "generating": _running_task(user.id, scope, period),
        **(await run_read(available, user.id)),
    }


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
    _period(scope, period)
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


@router.post("/playlist")
async def push_playlist(body: dict, user: User = Depends(get_current_user)):
    """Put this wrap-up's titles into a Plex playlist on the user's own account."""
    from src.database.runner import run_read
    from src.services.plex_playlists import push_wrapup_playlist
    from src.services.wrapup_store import load

    scope = str(body.get("scope") or "all")
    period = str(body.get("period") or "30d")
    p = _period(scope, period)
    row = await run_read(load, user.id, scope, period)
    facts = (row or {}).get("facts") or {}
    if not facts or facts.get("empty"):
        raise HTTPException(404, "Open the wrap-up first")
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
    try:
        out = await push_wrapup_playlist(user, p.label, videos, tracks)
    except Exception as e:
        logger.warning("[wrapup] playlist push for %s failed: %s", user.plex_username, e)
        raise HTTPException(502, "Plex did not take the playlist — try again in a moment")
    if out.get("skipped") == "no_token":
        raise HTTPException(409, "Sign out and in again once, so Curatarr can write to your own Plex account")
    if not out.get("pushed"):
        raise HTTPException(404, "None of these titles were found in Plex")
    return out
