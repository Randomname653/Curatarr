"""Anime re-resolution through the TVDB -> AniDB -> AniList crossref.

The audit flags anime whose profile names a different entity than the arr
item (``wrong_entity:*``) or shares its AniList id with another title
(``id_conflict:anilist_id``). Sonarr knows the series by its TVDB id; the
anime-lists mapping names the AniDB entry of that TVDB series, and the
offline snapshot names the AniList id of that AniDB entry (year-checked).
When that chain gives ONE answer and it is not the profile's, the item gets
an automatic pin on it and re-enriches. Measured on the 2026-10-24 backup:
10 of 30 pending anime findings resolved this way; the rest have several
AniDB entries per TVDB series (no main one), no AniList id, no mapping, or
no known arr year (the year check is what keeps a drifted AniDB id out),
and stay for the owner.

An item that already has a pin is never touched: the owner's pin wins, and
an automatic pin that did not fix the item is not re-pinned in a loop."""
import logging
import sqlite3
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

NOTE = "auto: TVDB -> AniDB -> AniList crossref"
_KINDS_PREFIX = ("wrong_entity:",)
_KINDS = ("id_conflict:anilist_id",)


def main_anidb(tvdb_id: int, path=None) -> Optional[int]:
    """The AniDB entry a TVDB series maps to: the one primary row, or the
    only row. Several entries without a single primary one (one TVDB series
    split into seasons on AniDB) give None - which one is meant is a guess."""
    from src.database.loop_guard import sqlite_connect
    from src.services.anime_mapping import MAPPING_DB_PATH
    path = path or MAPPING_DB_PATH
    if not tvdb_id or not path.exists():
        return None
    con = sqlite_connect(path)
    try:
        try:
            rows = con.execute("SELECT anidb_id, is_primary FROM anime_mapping WHERE tvdb_id=?",
                               (int(tvdb_id),)).fetchall()
        except sqlite3.OperationalError:
            rows = [(r[0], 1) for r in con.execute(
                "SELECT anidb_id FROM anime_mapping WHERE tvdb_id=?", (int(tvdb_id),)).fetchall()]
    finally:
        con.close()
    primary = {a for a, p in rows if p}
    if len(primary) == 1:
        return primary.pop()
    return rows[0][0] if len(rows) == 1 else None


def _profile_anilist(kind: str, detail: dict):
    if kind.startswith("id_conflict:"):
        return detail.get("value")
    return detail.get("profile_anilist_id")


def candidates(db) -> list[dict]:
    """The pending anime findings worth a crossref, one per arr item: Sonarr
    items with a TVDB id, a known arr year and no pin yet."""
    import json
    from src.database.models import (ArrEnrichmentStatus as AES, EnrichmentFinding as F,
                                     EnrichmentStatus as ES, MediaMatchOverride as MO)
    from src.services import enrichment_state as _ES
    pinned = {(s, a) for s, a in db.query(MO.service, MO.arr_id).all()}
    out: dict = {}
    rows = db.query(F).filter(F.status == "pending", F.category == "anime",
                              F.service == "sonarr", F.arr_id.isnot(None)).all()
    for f in rows:
        if not (f.kind in _KINDS or f.kind.startswith(_KINDS_PREFIX)):
            continue
        if ("sonarr", f.arr_id) in pinned or f.arr_id in out:
            continue
        try:
            detail = json.loads(f.detail or "{}")
        except ValueError:
            detail = {}
        out[f.arr_id] = {"arr_id": f.arr_id, "title": f.title, "kind": f.kind,
                         "profile_anilist_id": _profile_anilist(f.kind, detail),
                         "year": detail.get("arr_year")}
    for arr_id, c in list(out.items()):
        a = db.query(AES.tvdb_id, AES.title).filter(AES.service == "sonarr", AES.arr_id == arr_id).first()
        c["tvdb_id"] = a.tvdb_id if a else None
        c["title"] = c["title"] or (a.title if a else None)
        if not c["year"]:
            e = db.query(ES.sources_state).filter(ES.plex_rating_key == f"sonarr:{arr_id}").first()
            ctx = _ES.parse_sources_state(e.sources_state).get(_ES.CONTEXT_KEY) if e else None
            c["year"] = ctx.get("arr_year") if isinstance(ctx, dict) else None
        if not (c["tvdb_id"] and c["year"]):
            del out[arr_id]
    return list(out.values())


def resolve(c: dict, mapping_path=None) -> Optional[int]:
    """The AniList id the crossref names for one candidate, if it differs
    from the profile's; None when the chain breaks or agrees."""
    from src.services.anime_offline import lookup
    anidb = main_anidb(c["tvdb_id"], mapping_path)
    if not anidb:
        return None
    hit = lookup(anidb_id=anidb, year=int(c["year"]))
    pick = (hit or {}).get("anilist_id")
    if not pick or str(pick) == str(c.get("profile_anilist_id") or ""):
        return None
    return int(pick)


def pin(c: dict, anilist_id: int) -> bool:
    """Write the automatic pin and close the item's findings (the weekly
    audit re-raises after the grace week if the pin did not fix it)."""
    from src.database.models import MediaMatchOverride as MO
    from src.services import enrichment_state as _ES
    with get_db_session() as db:
        if db.query(MO.id).filter(MO.service == "sonarr", MO.arr_id == c["arr_id"]).first():
            return False      # pinned meanwhile (the owner was faster)
        db.add(MO(service="sonarr", arr_id=c["arr_id"], category="anime", title=c.get("title"),
                  anilist_id=anilist_id, note=NOTE, created_by=None))
        _ES.resolve_item_findings(db, f"sonarr:{c['arr_id']}", note=NOTE)
        db.commit()
    return True


async def run_anime_crossref(task=None) -> dict:
    """Custodian task: pin and re-enrich every anime finding the crossref
    settles. No model, no network - two local SQLite files."""
    from src.database.runner import run_read, run_write
    from src.routers.enrichment import _purge_and_requeue_item
    from src.services import kb_overview as _kb

    def _cands():
        with get_db_session() as db:
            return candidates(db)

    cands = await run_read(_cands)
    pinned = []
    for i, c in enumerate(cands):
        if task is not None:
            try:
                from src.services.task_monitor import task_monitor
                task_monitor.update(task, processed=i, total=len(cands),
                                    message=f"{len(pinned)} pinned")
            except Exception:  # noqa: BLE001 - progress card only
                pass
        anilist = await run_read(resolve, c)
        if not anilist or not await run_write(pin, c, anilist):
            continue
        await run_write(_purge_and_requeue_item, "sonarr", c["arr_id"], "anime")
        pinned.append(c["arr_id"])
        logger.info("[anime-crossref] %s (sonarr:%s): profile AniList %s -> %s",
                    c.get("title"), c["arr_id"], c.get("profile_anilist_id"), anilist)
    if pinned:
        _kb.invalidate()
    logger.info("[anime-crossref] %d candidates, %d pinned", len(cands), len(pinned))
    return {"candidates": len(cands), "pinned": len(pinned)}
