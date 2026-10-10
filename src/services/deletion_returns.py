"""
Curatarr — deleted items that came back, and SoulSync bans of deleted music.

Deleting is not always the end: an arr import list, a request, or SoulSync's
playlist sync can put a title back. Radarr and Sonarr deletes carry an
import-list exclusion since Pass 58; music had nothing until the SoulSync
blocklist (owner decision 2026-10-09). SoulSync's playlist sync compares
against Plex, not its own database, so a liked song of a deleted artist
lands on its wishlist at the next sync and downloads again within the hour.

Two jobs here, both in the custodian's daily deletion_returns task:

* ban_deleted_artist: the delete puts a deleted artist on SoulSync's
  blocklist and records the outcome on the proposal. retry_bans sets the
  bans that never held (a delete while SoulSync was down, a rotated API
  key, the music deleted before 2026-10-09): every day before the returns
  check (owner OK 2026-10-10), and by hand. A ban that held is never set
  again, so an artist the owner unblocks in SoulSync stays unblocked.
* check_returns (reads the libraries only): every deleted proposal is
  looked up in the library it came
  from. A match by TMDb / TVDb / MusicBrainz id counts whenever it is there
  ("still_there" when the library added it before the delete); a title
  match counts only when the title is unique and was added after the
  delete, because same-named films are common (2026-10-09: Skin Deep, The
  Enforcer and Ghost in the Shell matched other films of the same name).
  An open return shows in the Deletions view until the owner keeps it or
  deletes it again; one that leaves the library again is cleared.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

from src.database.models import DeletionProposal

logger = logging.getLogger(__name__)

# category -> the library it lives in; "lidarr" falls back to the Plex music
# index while Lidarr is switched off (src/routers/library._fetch_arr_library)
_LIBRARY_OF = {"movie": "radarr", "show": "sonarr", "anime": "sonarr", "music": "lidarr"}
_CATEGORY_OF_SERVICE = {"radarr": "movie", "sonarr": "show", "lidarr": "music", "plex": "music"}
CLOSED_STATES = ("kept", "deleted_again")


def _json(raw) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _category(p) -> Optional[str]:
    return p.category or _CATEGORY_OF_SERVICE.get(p.service or "")


# ── SoulSync bans ─────────────────────────────────────────────────────────────

def ban_held(p) -> bool:
    return bool(_json(p.soulsync_ban).get("ok"))


async def ban_deleted_artist(p) -> None:
    """Put a deleted artist on SoulSync's blocklist and record the outcome on
    the proposal (the caller commits). No SoulSync, or a test process: no
    record, nothing to retry. Never raises."""
    from src.services import soulsync_client as ss
    if _category(p) != "music" or not ss.writes_allowed():
        return
    mbid = None
    try:
        # SoulSync keeps listing the artist until its weekly deep scan, so
        # the MusicBrainz id is usually still there right after the delete.
        info = await ss.artist_info(p.title)
        mbid = (info or {}).get("musicbrainz_id") or _json(p.soulsync_ban).get("mbid")
        res = await ss.block_artist(p.title, mbid)
    except Exception as e:  # noqa: BLE001
        # The recorded error reaches the browser (ban_warning): a fixed
        # sentence, the exception only in the log (CodeQL, 2026-10-10).
        logger.warning("[deletion] SoulSync ban of %r raised: %s", p.title, e)
        res = {"ok": False, "id": None, "error": "the ban failed inside Curatarr (see the log)"}
    if res.get("skipped"):
        return
    p.soulsync_ban = json.dumps({
        "ok": bool(res.get("ok")), "id": res.get("id"),
        "at": datetime.utcnow().isoformat(timespec="seconds"),
        "error": res.get("error"), "mbid": mbid,
    })
    if not res.get("ok"):
        logger.warning("[deletion] SoulSync ban of %r failed: %s", p.title, res.get("error"))


def ban_warning(p) -> Optional[str]:
    """What the owner needs to hear when the ban did not hold, else None."""
    ban = _json(p.soulsync_ban)
    if not ban or ban.get("ok"):
        return None
    return (f'SoulSync did not block "{p.title}" ({ban.get("error") or "no answer"}), so its '
            f"playlist sync may download it again. Curatarr retries once a day; blocking "
            f"the artist in SoulSync settles it now.")


async def retry_bans(db) -> dict:
    """Ban every deleted artist whose ban never held. Returns counts."""
    from src.services import soulsync_client as ss
    if not ss.writes_allowed():
        return {"tried": 0, "held": 0, "failed": []}
    rows = (db.query(DeletionProposal)
            .filter(DeletionProposal.status == "deleted")
            .all())
    todo = [p for p in rows if _category(p) == "music" and not ban_held(p)]
    failed = []
    for p in todo:
        await ban_deleted_artist(p)
        if not ban_held(p):
            failed.append(p.title)
    db.commit()
    return {"tried": len(todo), "held": len(todo) - len(failed), "failed": failed}


# ── Returns ───────────────────────────────────────────────────────────────────

def _parse_added(raw) -> Optional[datetime]:
    """An arr's ISO "added" (with Z or offset) or the Plex index's naive UTC
    ISO, as naive UTC; None when missing or unreadable."""
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _view(service: str, it: dict) -> dict:
    """One library item, the fields the match needs, any arr's shape."""
    from src.services.soulsync_client import norm_name
    if service == "radarr":
        ext, title = it.get("tmdbId"), it.get("title")
    elif service == "sonarr":
        ext, title = it.get("tvdbId"), it.get("title")
    else:
        ext, title = it.get("foreignArtistId"), it.get("artistName")
    size = (it.get("statistics") or {}).get("sizeOnDisk") or it.get("sizeOnDisk") or 0
    return {
        "service": "plex" if it.get("_source") == "plex" else service,
        "media_id": str(it.get("id") or ""),
        "ext": str(ext) if ext else None,
        "title": title or "",
        "norm": norm_name(title or ""),
        "added": _parse_added(it.get("added")),
        "size_mb": round(float(size or 0) / 1048576, 1),
    }


class _Index:
    def __init__(self, service: str, items: list):
        self.by_ext: dict = {}
        self.by_norm: dict = {}
        for it in items or []:
            v = _view(service, it)
            if v["ext"]:
                self.by_ext.setdefault(v["ext"], v)
            if v["norm"]:
                self.by_norm.setdefault(v["norm"], []).append(v)


def _ext_of(p) -> Optional[str]:
    cat = _category(p)
    if cat == "movie":
        return str(p.tmdb_id) if p.tmdb_id else None
    if cat in ("show", "anime"):
        return str(p.tvdb_id) if p.tvdb_id else None
    if cat == "music":
        return _json(p.soulsync_ban).get("mbid") or None
    return None


def match(p, index: _Index) -> Optional[tuple]:
    """(library view, "id" | "title") when this deleted proposal is in the
    library again, else None. See the module docstring for the rules."""
    from src.services.soulsync_client import norm_name
    ext = _ext_of(p)
    if ext and ext in index.by_ext:
        return index.by_ext[ext], "id"
    same = index.by_norm.get(norm_name(p.title or "")) or []
    if len(same) == 1:
        v = same[0]
        if v["added"] and p.resolved_at and v["added"] > p.resolved_at:
            if not (ext and v["ext"] and v["ext"] != ext):   # a different film of that name
                return v, "title"
    return None


async def _library_index(service: str) -> Optional[_Index]:
    """The library a category lives in, None when it cannot be read now (not
    set up, or no copy at all): its open returns then stay as they are."""
    from src.routers.library import _fetch_arr_library
    try:
        items, _tags, _info = await _fetch_arr_library(service)
    except Exception as e:
        logger.info("[returns] %s library not readable: %s", service, getattr(e, "detail", e))
        return None
    return _Index(service, items)


def apply_matches(rows: list, indexes: dict) -> list:
    """Record or clear the returns of these deleted proposals against the
    library indexes ({library service: _Index or None}); returns the titles
    that came back since the last check."""
    new = []
    for p in rows:
        info = _json(p.returned_info)
        if info.get("state") in CLOSED_STATES:
            continue
        index = indexes.get(_LIBRARY_OF.get(_category(p) or ""))
        if index is None:
            continue
        hit = match(p, index)
        if hit is None:
            if info:                       # it left the library again
                p.returned_at, p.returned_info = None, None
            continue
        v, how = hit
        kind = ("returned" if v["added"] and p.resolved_at and v["added"] > p.resolved_at
                else "still_there")
        if not info:
            new.append(p.title)
        p.returned_at = v["added"] or p.returned_at or datetime.utcnow()
        p.returned_info = json.dumps({
            "kind": kind, "service": v["service"], "media_id": v["media_id"],
            "title": v["title"], "size_mb": v["size_mb"], "match": how,
            "state": "open",
        })
    return new


async def check_returns(db) -> dict:
    """Look every deleted proposal up in its library. Returns counts and the
    titles that came back since the last check."""
    rows = db.query(DeletionProposal).filter(DeletionProposal.status == "deleted").all()
    wanted = {_LIBRARY_OF.get(_category(p) or "") for p in rows} - {None}
    indexes = {svc: await _library_index(svc) for svc in sorted(wanted)}
    new = apply_matches(rows, indexes)
    db.commit()
    open_n = sum(1 for p in rows if _json(p.returned_info).get("state") == "open")
    return {"checked": len(rows), "open": open_n, "new": new,
            "unreadable": sorted(s for s, i in indexes.items() if i is None)}


async def run_daily(task=None) -> dict:
    """The custodian's deletion_returns task: SoulSync bans that never held
    first (a fresh ban can stop a download the check would only report
    later), then the returns check."""
    from src.database.connection import get_db_session
    from src.services.task_monitor import task_monitor
    with get_db_session() as db:
        bans = await retry_bans(db)
        res = await check_returns(db)
    parts = []
    if bans["tried"]:
        parts.append(f"SoulSync bans: {bans['held']}/{bans['tried']} set")
        if bans["failed"]:
            parts.append("not blocked: " + ", ".join(bans["failed"][:5])
                         + (" ..." if len(bans["failed"]) > 5 else ""))
    parts.append(f"{res['open']} deleted title(s) back in the library")
    if res["new"]:
        parts.append("new: " + ", ".join(res["new"][:5]) + (" ..." if len(res["new"]) > 5 else ""))
    if res["unreadable"]:
        parts.append("not checked: " + ", ".join(res["unreadable"]))
    msg = "; ".join(parts)
    logger.info("[returns] %s", msg)
    if task is not None:
        task_monitor.update(task, message=msg,
                            level="warn" if (res["new"] or bans["failed"]) else "info")
    return {"ok": True, "bans": bans, **res}


# ── The Deletions view ────────────────────────────────────────────────────────

def open_returns(db, user_id: int, category: Optional[str] = None) -> list:
    """The owner's open returns, newest first, as the view renders them."""
    rows = (db.query(DeletionProposal)
            .filter(DeletionProposal.user_id == user_id,
                    DeletionProposal.status == "deleted",
                    DeletionProposal.returned_info.isnot(None))
            .order_by(DeletionProposal.returned_at.desc())
            .all())
    out = []
    for p in rows:
        info = _json(p.returned_info)
        if info.get("state") != "open":
            continue
        cat = _category(p)
        if category and cat != category:
            continue
        out.append({
            "id": p.id, "title": p.title, "category": cat,
            "service": info.get("service"), "kind": info.get("kind"),
            "match": info.get("match"),
            "deleted_at": p.resolved_at.isoformat() if p.resolved_at else None,
            "back_since": p.returned_at.isoformat() if p.returned_at else None,
            "size_gb": round((info.get("size_mb") or 0) / 1024, 1),
        })
    return out


def close_return(p, state: str, new_proposal_id: Optional[int] = None) -> None:
    """Keep (no more reports) or deleted again (a new proposal took over)."""
    info = _json(p.returned_info)
    info["state"] = state
    if new_proposal_id:
        info["new_proposal_id"] = new_proposal_id
    p.returned_info = json.dumps(info)


def _cli(argv: list) -> int:
    """By hand, when the owner asks:
        python -m src.services.deletion_returns --retry-bans   (bans that never held)
        python -m src.services.deletion_returns --check        (the daily returns check)"""
    import asyncio
    from src.database.connection import get_db_session, init_db
    if argv[:1] in (["--retry-bans"], ["--check"]):
        init_db()            # the new columns, when the app has not started on this code yet
    if argv[:1] == ["--retry-bans"]:
        with get_db_session() as db:
            out = asyncio.run(retry_bans(db))
        print(f"SoulSync bans: {out['held']}/{out['tried']} set"
              + (f"; not blocked: {', '.join(out['failed'])}" if out["failed"] else ""))
        return 0 if not out["failed"] else 1
    if argv[:1] == ["--check"]:
        out = asyncio.run(run_daily())
        print(f"{out['open']} deleted title(s) back in the library"
              + (f"; new: {', '.join(out['new'])}" if out["new"] else ""))
        return 0
    print(_cli.__doc__)
    return 2


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
