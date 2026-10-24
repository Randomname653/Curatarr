"""Curatarr — the credits index: who made what, queryable.

Directors, writers, cast, voice actors, studios, production companies and
networks lived only as JSON inside the raw API cache (``raw:{cat}:{id}``,
90-day TTL): not queryable, and gone for a title deleted in March by the time
anyone asks in December. A wrap-up's "most-watched director" or studio
constellation needed a JSON scan over a cache that may have expired.

This walker reads every live raw document of the video categories and keeps
what it says in three durable tables (``MediaWork``, ``MediaCredit``,
``MediaCompany`` in models.py):

* a title is filed under its best id — TMDB for films and shows; AniList,
  else MAL, else TMDB for anime. A title's data is spread over several raw
  rows (doc id, external ids, title); rows sharing any id are one title,
  merged: the credits of the richest row, the companies of all;
* newer TMDB fetches carry ids, characters, companies, networks and the
  collection (``fetch_tmdb_full``'s ``credits`` / ``companies`` /
  ``collection``); older rows only names, which still join across titles by
  ``name_key``;
* an unchanged title is skipped by its fingerprint, so the daily walk
  writes only what moved. A row that expires from the cache keeps its
  index entry.

The anime cast TMDB lists is the voice cast: it is filed as ``voice``.
"""
import hashlib
import json
import logging
import re
import unicodedata
from datetime import datetime
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

CATEGORIES = ("movie", "show", "anime")
_ID_ORDER = {"anime": (("anilist", "anilist_id"), ("mal", "mal_id"), ("tmdb", "tmdb_id"))}
_DEFAULT_IDS = (("tmdb", "tmdb_id"),)
_CHUNK = 300
_PAREN = re.compile(r"\s*\([^)]*\)")


def name_key(name: str) -> str:
    """'Hayao Miyazaki' / 'hayao  miyazaki' / 'Hayáo Miyazaki' → one key."""
    s = unicodedata.normalize("NFKD", name or "")
    s = "".join(ch for ch in s if not unicodedata.combining(ch)).lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()[:256]


def _names(value) -> list:
    """A name list from a list or an OMDb-style comma string, roles in
    parentheses ("Jane Doe (screenplay)") dropped."""
    items = value if isinstance(value, list) else str(value or "").split(",")
    out = []
    for v in items:
        n = _PAREN.sub("", str(v or "")).strip()
        if n and n.upper() != "N/A" and n not in out:
            out.append(n)
    return out


def _int(v) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def extract(category: str, doc: dict) -> Optional[dict]:
    """What one raw document says about one title, or None when it names
    no id or no people and no companies."""
    if category not in CATEGORIES or not isinstance(doc, dict):
        return None
    ids = [(source, str(doc[field])[:32]) for source, field in _ID_ORDER.get(category, _DEFAULT_IDS)
           if doc.get(field) not in (None, "", 0)]
    if not ids:
        return None
    people = []
    if isinstance(doc.get("credits"), list):
        for c in doc["credits"]:
            if isinstance(c, dict) and c.get("name") and c.get("role"):
                people.append({"role": str(c["role"])[:16], "person_id": _int(c.get("id")),
                               "name": str(c["name"])[:256], "ord": _int(c.get("ord")) or 0,
                               "character": c.get("character")})
    else:
        for role, field in (("director", "director"), ("writer", "writer"),
                            ("author", "source_author")):
            for n in _names(doc.get(field)):
                people.append({"role": role, "person_id": None, "name": n[:256], "ord": 0,
                               "character": None})
        for i, n in enumerate(_names(doc.get("cast"))):
            people.append({"role": "cast", "person_id": None, "name": n[:256], "ord": i,
                           "character": None})
    if category == "anime":
        for p in people:
            if p["role"] == "cast":
                p["role"] = "voice"
    companies = []
    if isinstance(doc.get("companies"), list):
        for c in doc["companies"]:
            if isinstance(c, dict) and c.get("name") and c.get("kind"):
                companies.append({"kind": str(c["kind"])[:16], "company_id": _int(c.get("id")),
                                  "name": str(c["name"])[:256]})
    for n in _names(doc.get("studios")):
        companies.append({"kind": "studio", "company_id": None, "name": n[:256]})
    if not people and not companies:
        return None
    coll = doc.get("collection") if isinstance(doc.get("collection"), dict) else None
    return {"category": category, "ids": ids, "id_source": ids[0][0], "work_id": ids[0][1],
            "title": (doc.get("title") or "")[:512] or None, "year": _int(doc.get("year")),
            "collection": coll, "people": people, "companies": companies,
            "structured": isinstance(doc.get("credits"), list)}


def group(found: list) -> list:
    """Rows of the same title, together: rows share a title when they share
    any id — an anime row with both its AniList and TMDB id joins the AniList
    row (studios) and the TMDB row (voice cast)."""
    parent: dict = {}

    def root(k):
        while parent.setdefault(k, k) != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    for f in found:
        keys = [(f["category"], *i) for i in f["ids"]]
        for k in keys[1:]:
            parent[root(k)] = root(keys[0])
    out: dict = {}
    for f in found:
        out.setdefault(root((f["category"], *f["ids"][0])), []).append(f)
    return list(out.values())


def merge(found: list) -> dict:
    """The rows of one title folded into one, filed under its best id (the
    category's id order): the credits of the richest row (structured ids
    beat bare names, then the longer list), companies from every row, the
    first collection, title and year."""
    best = max(found, key=lambda f: (f["structured"], len(f["people"])))
    seen, companies = set(), []
    for f in found:
        for c in f["companies"]:
            k = (c["kind"], name_key(c["name"]))
            if k not in seen:
                seen.add(k)
                companies.append(c)
    out = dict(best, companies=companies)
    order = [src for src, _ in _ID_ORDER.get(best["category"], _DEFAULT_IDS)]
    all_ids = {i for f in found for i in f["ids"]}
    out["id_source"], out["work_id"] = min(all_ids, key=lambda i: (order.index(i[0]), i[1]))
    out["aliases"] = sorted(all_ids - {(out["id_source"], out["work_id"])})
    for f in found:
        out["collection"] = out.get("collection") or f.get("collection")
        out["title"] = out.get("title") or f.get("title")
        out["year"] = out.get("year") or f.get("year")
    out["fingerprint"] = hashlib.sha1(json.dumps(
        [out["title"], out["year"], out["collection"], out["people"], companies],
        sort_keys=True, default=str).encode(), usedforsecurity=False).hexdigest()
    return out


def scan_cache(cache=None) -> list:
    """Every live raw document of the video categories, merged per title.
    Blocking — the cache file has its own lock; call via ``to_thread``."""
    from src.cache.metadata_cache import MetadataCache
    owns = cache is None
    cache = cache or MetadataCache()
    found: list = []
    try:
        # The cache's own clock: expires_at is local time in ISO form
        # (MetadataCache._expires_at). SQLite's datetime('now') is UTC with a
        # space, and "T" sorts after " ": a row expiring today read as alive.
        cur = cache.conn.execute(
            "SELECT cache_key, response FROM api_cache "
            "WHERE (cache_key LIKE 'v2:raw:%' OR cache_key LIKE 'raw:%') "
            "AND expires_at > ?", (datetime.now().isoformat(),))
        for key, resp in cur:
            filed = (key[3:] if key.startswith("v2:") else key).split(":", 2)
            if len(filed) != 3 or filed[1] not in CATEGORIES:
                continue
            try:
                doc = json.loads(resp)
            except (TypeError, ValueError):
                continue
            got = extract(filed[1], doc)
            if got:
                found.append(got)
        cur.close()
    finally:
        if owns:
            cache.close()
    return [merge(g) for g in group(found)]


def persist(works: list) -> dict:
    """Upsert merged titles; a title whose fingerprint is unchanged is left
    alone. Blocking — call via ``run_write``."""
    from src.database.models import MediaCompany, MediaCredit, MediaWork
    counts = {"new": 0, "updated": 0, "unchanged": 0, "folded": 0}
    with get_db_session() as db:
        for w in works:
            # Filed under another of its ids by an earlier walk (before the
            # row that bridges the two existed): one title, one entry.
            for src, wid in w.get("aliases") or ():
                old = (db.query(MediaWork).filter_by(category=w["category"], id_source=src,
                                                     work_id=wid).first())
                if old is not None:
                    db.query(MediaCredit).filter(MediaCredit.work_ref == old.id).delete(synchronize_session=False)
                    db.query(MediaCompany).filter(MediaCompany.work_ref == old.id).delete(synchronize_session=False)
                    db.delete(old)
                    counts["folded"] += 1
            row = (db.query(MediaWork).filter_by(category=w["category"], id_source=w["id_source"],
                                                 work_id=w["work_id"]).first())
            if row is not None and row.fingerprint == w["fingerprint"]:
                counts["unchanged"] += 1
                continue
            if row is None:
                row = MediaWork(category=w["category"], id_source=w["id_source"], work_id=w["work_id"])
                db.add(row)
                db.flush()
                counts["new"] += 1
            else:
                db.query(MediaCredit).filter(MediaCredit.work_ref == row.id).delete(synchronize_session=False)
                db.query(MediaCompany).filter(MediaCompany.work_ref == row.id).delete(synchronize_session=False)
                counts["updated"] += 1
            coll = w.get("collection") or {}
            row.title, row.year = w["title"], w["year"]
            row.collection_id, row.collection_name = _int(coll.get("id")), (coll.get("name") or None)
            row.fingerprint, row.indexed_at = w["fingerprint"], datetime.utcnow()
            for p in w["people"]:
                db.add(MediaCredit(work_ref=row.id, role=p["role"], person_id=p["person_id"],
                                   name=p["name"], name_key=name_key(p["name"]), ord=p["ord"],
                                   character=(p.get("character") or None)))
            for c in w["companies"]:
                db.add(MediaCompany(work_ref=row.id, kind=c["kind"], company_id=c["company_id"],
                                    name=c["name"], name_key=name_key(c["name"])))
        db.commit()
    return counts


async def run_credits_index(task=None) -> bool:
    """The custodian job: scan the raw cache, write what changed in chunks
    (short write transactions on the single writer)."""
    import asyncio
    from src.database.runner import run_write
    from src.services.task_monitor import task_monitor
    works = await asyncio.to_thread(scan_cache)
    total = {"new": 0, "updated": 0, "unchanged": 0, "folded": 0}
    for i in range(0, len(works), _CHUNK):
        got = await run_write(persist, works[i:i + _CHUNK])
        for k, v in got.items():
            total[k] += v
        if task is not None:
            task_monitor.update(task, processed=min(i + _CHUNK, len(works)), total=len(works),
                                message=f"{total['new']} new, {total['updated']} updated")
    logger.info("[credits] %d titles: %d new, %d updated, %d unchanged, %d folded",
                len(works), total["new"], total["updated"], total["unchanged"], total["folded"])
    return True


def credits_of(db, category: str, id_source: str, work_id) -> dict:
    """{role: [names in billing order]} and {kind: [names]} for one title."""
    from src.database.models import MediaCompany, MediaCredit, MediaWork
    row = (db.query(MediaWork).filter_by(category=category, id_source=id_source,
                                         work_id=str(work_id)).first())
    if row is None:
        return {}
    people: dict = {}
    for c in db.query(MediaCredit).filter(MediaCredit.work_ref == row.id).order_by(MediaCredit.ord):
        people.setdefault(c.role, []).append(c.name)
    companies: dict = {}
    for c in db.query(MediaCompany).filter(MediaCompany.work_ref == row.id).order_by(MediaCompany.id):
        companies.setdefault(c.kind, []).append(c.name)
    return {"title": row.title, "year": row.year, "collection": row.collection_name,
            "people": people, "companies": companies}
