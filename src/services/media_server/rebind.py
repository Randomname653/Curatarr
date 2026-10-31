"""Switching an install between Plex and Jellyfin (docs/JELLYFIN_DESIGN.md §8).

``build_plan`` reads both sides and matches every server key Curatarr holds
to the other server's key: first from ``server_id_map``, then by the
canonical keys of both servers' library walks (external ids, then the file
below the library root, never a title alone). Users go by an explicit
``--map-user``, then the id memory, then their name. Nothing is written.

``apply_plan`` (``--write``) backs the database up, writes a manifest, then
re-keys every column that holds a server key (design Appendix A.1) in ONE
transaction, copies the Chroma vectors under their new ids (upsert, so a
resumed run is harmless), renames the facet parents, drops the app state
that names server objects (playlist snapshots and the like: the next push
recreates them on the new server) and flips ``MEDIA_SERVER_TYPE``. Rows
whose new key is already taken in a unique column stay on the old key and
are counted ("collisions"): they behave like orphans and come back with a
later rebind.

``undo`` replays the manifest backwards by VALUE (new -> old), so plays
recorded after the switch are carried back too; the Chroma copies under the
new ids are deleted, so no twin vectors remain.

The metadata cache is left alone except ``raw_prefetch:`` (keyed by the
server key alone): ``raw:{cat}:{id}`` keys mix server keys with TMDB ids in
one number space, and renaming them could clobber a TMDB entry. They expire
and are fetched again.
"""
from __future__ import annotations

import json
import logging
import os
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from sqlalchemy import text

from src.services.media_server.base import (
    ServerKind, ServerUser, _norm_name, is_jellyfin_key, is_plex_key,
)
from src.services.media_server.id_map import item_rows, key_rank, library_rows, match_method

logger = logging.getLogger(__name__)

# Table and column names reach SQL only from these constants (and a
# manifest's column names are checked against them before use).
#
# Every column whose value is a server ITEM key (Appendix A.1). The values
# are filtered by the source server's key shape, so *arr ids, Spotify URIs,
# Netflix refs and ext-script keys in the same columns are never touched.
ITEM_COLUMNS = (
    ("watch_history", "plex_item_id"), ("plex_ratings", "plex_item_id"),
    ("enrichment_status", "plex_rating_key"), ("enrichment_findings", "plex_rating_key"),
    ("media_identities", "plex_rating_key"), ("media_tech_profiles", "plex_rating_key"),
    ("media_subtitle_profiles", "plex_rating_key"), ("library_items", "plex_rating_key"),
    ("library_events", "plex_rating_key"), ("cached_recommendations", "plex_rating_key"),
    ("music_tags", "item_key"),
)
# Vector-store doc ids: ``plex_rating_key or tmdb_id or anilist_id or title``
# (media_enricher.store_profile_vector). A TMDB id can look exactly like a
# Plex key, so here - as for the Chroma docs and the facet parents - only a
# value that IS an enrichment key (enrichment_status) is a server key.
DOC_COLUMNS = (("swipe_ratings", "doc_id"), ("title_nanogenres", "parent"))
USER_COLUMNS = (("users", "plex_user_id"), ("watch_history", "plex_user_id"))
LIBRARY_COLUMNS = (("library_config", "plex_section_key"), ("music_tags", "section_key"))
# App state naming server objects of the old server; the next push or walk
# rebuilds them on the new one.
DROP_STATE_PREFIXES = ("rec_playlist_push:", "plex_section_missing:", "curatarr_collections",
                       "lib_default:")
OPEN_PROPOSALS_EXCLUDE = ("deleted", "rejected")


def is_key_of(kind: ServerKind, value) -> bool:
    return is_plex_key(value) if kind == "plex" else is_jellyfin_key(value)


@dataclass
class Plan:
    source: ServerKind
    target: ServerKind
    items: dict = field(default_factory=dict)          # source key -> target key
    methods: Counter = field(default_factory=Counter)
    unmatched: list = field(default_factory=list)      # (source key, title)
    conflicts: list = field(default_factory=list)      # (target key, [source keys])
    suggestions: list = field(default_factory=list)    # (source key, title, target key)
    users: dict = field(default_factory=dict)          # curatarr id -> (source id, target id, how)
    users_unmatched: list = field(default_factory=list)
    users_skipped: list = field(default_factory=list)
    libraries: dict = field(default_factory=dict)      # source section key -> (target key, title)
    libraries_unmatched: list = field(default_factory=list)
    tables: dict = field(default_factory=dict)         # "table.column" -> {"matched": n, "unmatched": n}
    source_walked: bool = False
    target_rows: list = field(default_factory=list)    # the target walk's id-map rows, filed on --write
    source_rows: list = field(default_factory=list)

    @property
    def blocking(self) -> list:
        out = []
        if self.users_unmatched:
            out.append("users without a match on the new server: "
                       + ", ".join(n for _, n in self.users_unmatched)
                       + " (use --map-user or --skip-user)")
        return out

    def report(self) -> dict:
        return {
            "from": self.source, "to": self.target, "source_walked": self.source_walked,
            "items": {"matched": len(self.items), "unmatched": len(self.unmatched),
                      "conflicts": len(self.conflicts), "suggestions": len(self.suggestions),
                      "by_method": dict(self.methods)},
            "tables": self.tables,
            "users": {str(u): {"from": s, "to": t, "how": h} for u, (s, t, h) in self.users.items()},
            "users_unmatched": [n for _, n in self.users_unmatched],
            "users_skipped": self.users_skipped,
            "libraries": {k: v[1] for k, v in self.libraries.items()},
            "libraries_unmatched": self.libraries_unmatched,
            "unmatched_sample": [{"key": k, "title": t} for k, t in self.unmatched[:25]],
            "conflicts": [{"target": t, "sources": s} for t, s in self.conflicts[:25]],
            "suggestions": [{"key": k, "title": t, "target": g} for k, t, g in self.suggestions[:25]],
            "blocking": self.blocking,
        }


# ── reading ──────────────────────────────────────────────────────────────────

def _engine():
    from src.database.connection import engine
    return engine


def _enrichment_keys() -> set:
    with _engine().connect() as c:
        return {k for (k,) in c.execute(text("SELECT plex_rating_key FROM enrichment_status")).fetchall()}


def _source_keys(kind: ServerKind) -> tuple[set, dict, dict]:
    """Blocking: every source-shaped key in the item columns (and the doc-id
    columns, where it is an enrichment key), with a title and year where
    Curatarr knows one, and the row counts per column."""
    keys, titles, per_col = set(), {}, {}
    enriched = _enrichment_keys()
    with _engine().connect() as c:
        for table, col in ITEM_COLUMNS + DOC_COLUMNS:
            rows = c.execute(text(f"SELECT {col}, COUNT(*) FROM {table} GROUP BY {col}")).fetchall()  # nosec B608 - table/col from the module's constants
            mine = {v: n for v, n in rows if is_key_of(kind, v)
                    and ((table, col) not in DOC_COLUMNS or v in enriched)}
            keys |= set(mine)
            per_col[f"{table}.{col}"] = mine
        for table, tcol, ycol in (("media_tech_profiles", "title", None), ("library_items", "title", None),
                                  ("media_identities", "title", "year")):
            sel = f"SELECT plex_rating_key, {tcol}" + (f", {ycol}" if ycol else ", NULL") + f" FROM {table}"
            for k, t, y in c.execute(text(sel)).fetchall():
                if k in keys and t and k not in titles:
                    titles[k] = (t, y)
        for k, t in c.execute(text("SELECT plex_item_id, title FROM watch_history GROUP BY plex_item_id")).fetchall():
            if k in keys and t and k not in titles:
                titles[k] = (t, None)
        excl = ", ".join(f"'{s}'" for s in OPEN_PROPOSALS_EXCLUDE)
        rows = c.execute(text(f"SELECT media_id, title FROM deletion_proposals WHERE service = :s "  # nosec B608 - the excluded statuses are a constant tuple
                              f"AND status NOT IN ({excl})"), {"s": kind}).fetchall()
        per_col["deletion_proposals.media_id"] = Counter(m for m, _ in rows if is_key_of(kind, m))
        for m, t in rows:
            if is_key_of(kind, m):
                keys.add(m)
                titles.setdefault(m, (t, None))
    return keys, titles, per_col


def _map_rows(kind: ServerKind) -> list:
    from src.database.connection import get_db_session
    from src.database.models import ServerIdMap as M
    col = M.plex_id if kind == "plex" else M.jellyfin_id
    with get_db_session() as db:
        return [(m.entity_type, m.canonical_key, getattr(m, col.key))
                for m in db.query(M).filter(col.isnot(None)).all()]


def _curatarr_users() -> list:
    from src.database.connection import get_db_session
    from src.database.models import ServerIdMap as M, User
    with get_db_session() as db:
        remembered = {m.canonical_key: (m.plex_id, m.jellyfin_id)
                      for m in db.query(M).filter(M.entity_type == "user").all()}
        return [(u.id, u.plex_user_id, u.plex_username, remembered.get(f"user:{u.id}"))
                for u in db.query(User).all()]


def _library_configs() -> list:
    from src.database.connection import get_db_session
    from src.database.models import LibraryConfig
    with get_db_session() as db:
        return [(c.plex_section_key, c.plex_section_title, c.plex_section_type) for c in db.query(LibraryConfig).all()]


async def _walk(adapter) -> tuple[list, list]:
    libs = await adapter.libraries()
    items = []
    for lib in libs:
        items += [it async for it in adapter.items(lib)]
    return libs, items


_LIB_KIND = {"movie": "movie", "show": "show", "artist": "music", "music": "music"}


async def build_plan(source: ServerKind, target: ServerKind, target_adapter, *, source_adapter=None,
                     map_user: Optional[dict] = None, skip_users=(), accept_suggestions: bool = False) -> Plan:
    from src.database.runner import run_read
    plan = Plan(source=source, target=target)
    keys, titles, per_col = await run_read(_source_keys, source)

    # The target side: its walk, as canonical keys.
    t_libs, t_items = await _walk(target_adapter)
    t_roots = {lib.key: lib.roots for lib in t_libs}
    plan.target_rows = item_rows(t_items, t_roots) + library_rows(t_libs)
    tgt_by_ck = {}
    for etype, ck, tid in plan.target_rows:
        tgt_by_ck.setdefault((etype, ck), tid)
    # How many source items one target item may stand for: its identities
    # (a double episode has two), not its aliases.
    cks_per_tid = Counter(tid for _e, _ck, tid in item_rows(t_items, t_roots, aliases=False))

    # The source side: the id memory, plus the source's own walk if it answers.
    src_cks = defaultdict(set)
    for etype, ck, sid in await run_read(_map_rows, source):
        src_cks[sid].add((etype, ck))
    s_libs = []
    if source_adapter is not None:
        try:
            s_libs, s_items = await _walk(source_adapter)
            plan.source_rows = item_rows(s_items, {lib.key: lib.roots for lib in s_libs}) + library_rows(s_libs)
            for etype, ck, sid in plan.source_rows:
                src_cks[sid].add((etype, ck))
            plan.source_walked = True
        except Exception as e:  # noqa: BLE001 - the id memory alone still matches; the report says the walk failed
            logger.warning("[rebind] %s did not answer, matching from the id memory only: %s", source, e)

    # Items.
    claimed = defaultdict(list)
    for key in sorted(keys):
        # The strongest key both sides share: an id before a chain before a file.
        hits = sorted(((ck, tgt_by_ck[(e, ck)]) for e, ck in src_cks.get(key, ()) if (e, ck) in tgt_by_ck),
                      key=lambda h: (key_rank(h[0]), h[0]))
        if hits:
            plan.items[key] = hits[0][1]
            plan.methods[match_method(hits[0][0])] += 1
            claimed[hits[0][1]].append(key)
    for tid, srcs in claimed.items():
        # One target item may stand for as many source items as it has
        # canonical keys (a Jellyfin double episode is two Plex episodes).
        if len(srcs) > max(1, cks_per_tid[tid]):
            plan.conflicts.append((tid, srcs))
            for s in srcs:
                plan.items.pop(s, None)
    unmatched = [k for k in sorted(keys) if k not in plan.items and k not in
                 {s for _, srcs in plan.conflicts for s in srcs}]
    by_title = defaultdict(set)
    for it in t_items:
        if it.kind in ("movie", "series", "artist"):
            by_title[(_norm_name(it.title), it.year)].add(it.key)
    for k in unmatched:
        title, year = titles.get(k, (None, None))
        plan.unmatched.append((k, title))
        cand = by_title.get((_norm_name(title), year)) if title else None
        if cand and len(cand) == 1:
            plan.suggestions.append((k, title, next(iter(cand))))
    if accept_suggestions:
        for k, _t, tid in plan.suggestions:
            plan.items[k] = tid
            plan.methods["suggestion"] += 1
        accepted = {k for k, _, _ in plan.suggestions}
        plan.unmatched = [(k, t) for k, t in plan.unmatched if k not in accepted]
    for colname, counts in per_col.items():
        plan.tables[colname] = {"matched": sum(n for v, n in counts.items() if v in plan.items),
                                "unmatched": sum(n for v, n in counts.items() if v not in plan.items)}

    # Users. Curatarr's Plex users are plex.tv accounts (the PIN login), not
    # the server's local account ids: towards Plex, the plex.tv list.
    t_users = (await target_adapter.account_users() if hasattr(target_adapter, "account_users")
               else await target_adapter.users())
    by_name = {"".join(u.name.casefold().split()): u for u in t_users}
    by_key = {u.key: u for u in t_users}
    explicit = {"".join(a.casefold().split()): "".join(b.casefold().split()) for a, b in (map_user or {}).items()}
    skip = {"".join(s.casefold().split()) for s in skip_users}
    for uid, sid, name, remembered in await run_read(_curatarr_users):
        if not is_key_of(source, sid):
            continue
        nm = "".join((name or "").casefold().split())
        hit, how = None, None
        if nm in explicit and explicit[nm] in by_name:
            hit, how = by_name[explicit[nm]], "user_flag"
        elif remembered and (remembered[0] if target == "plex" else remembered[1]):
            # The id this user had on the target before: the one they log in
            # with there, listed or not (Plex's server lists local ids).
            rid = remembered[0] if target == "plex" else remembered[1]
            hit, how = by_key.get(rid) or ServerUser(key=rid, name=name or ""), "id_memory"
        elif nm in by_name:
            hit, how = by_name[nm], "name"
        if hit:
            plan.users[uid] = (sid, hit.key, how)
        elif nm in skip:
            plan.users_skipped.append(name)
        else:
            plan.users_unmatched.append((uid, name))

    # Libraries.
    t_by_lib = {(lib.kind, _norm_name(lib.title)): lib for lib in t_libs}
    for skey, stitle, stype in await run_read(_library_configs):
        if not is_key_of(source, skey):
            continue
        kind = _LIB_KIND.get(stype or "", stype)
        lib = t_by_lib.get((kind, _norm_name(stitle)))
        same_kind = [l for l in t_libs if l.kind == kind]
        if lib is None and len(same_kind) == 1:
            lib = same_kind[0]
        if lib:
            plan.libraries[skey] = (lib.key, lib.title)
        else:
            plan.libraries_unmatched.append(stitle)
    return plan


# ── writing ──────────────────────────────────────────────────────────────────

def _db_path() -> str:
    from src.config import settings
    return str(settings.DATABASE_URL).replace("sqlite:///", "")


def default_backup_root() -> Path:
    return Path(_db_path()).resolve().parent / "backups"


def running_manifest(root: Path) -> Optional[Path]:
    for m in sorted(root.glob("rebind-*/manifest.json")):
        try:
            if json.loads(m.read_text(encoding="utf-8")).get("status") == "running":
                return m
        except (OSError, ValueError):
            continue
    return None


def _save(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest, indent=1, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _shared_targets(pairs: dict) -> set:
    """Target keys more than one source key maps to (a Jellyfin double
    episode is two Plex episodes): undoing those needs the rows, not values."""
    seen = Counter(pairs.values())
    return {t for t, n in seen.items() if n > 1}


def _rekey(conn, table: str, col: str, pairs: dict, *, where: str = "", rows_of: Optional[dict] = None) -> dict:
    """UPDATE OR IGNORE per pair: a row whose new value is taken in a unique
    column stays where it is and counts as a collision. ``rows_of`` collects
    the ids moved for pairs into a shared target (see _shared_targets)."""
    moved = stuck = 0
    shared = _shared_targets(pairs) if rows_of is not None else set()
    for old, new in pairs.items():
        if new in shared:
            ids = [i for (i,) in conn.execute(text(f"SELECT id FROM {table} WHERE {col} = :old{where}"),  # nosec B608 - table/col are the module's constants
                                              {"old": old}).fetchall()]
            if ids:
                rows_of.setdefault(old, []).extend(ids)
        r = conn.execute(text(f"UPDATE OR IGNORE {table} SET {col} = :new WHERE {col} = :old{where}"),  # nosec B608 - table/col are the module's constants
                         {"new": new, "old": old})
        moved += r.rowcount or 0
        stuck += conn.execute(text(f"SELECT COUNT(*) FROM {table} WHERE {col} = :old{where}"),  # nosec B608 - table/col are the module's constants
                              {"old": old}).scalar() or 0
    return {"moved": moved, "collisions": stuck}


class ChromaStore:
    """The main vector collection and the facet map, as the rebind needs them."""

    def __init__(self, wrapper=None):
        if wrapper is None:
            from src.vector_store.chromadb_wrapper import get_chroma_db
            wrapper = get_chroma_db()
        self.w = wrapper

    def copy(self, pairs: list) -> list:
        """Upsert each existing ``old`` doc under ``new``; returns the pairs copied."""
        done = []
        olds = [o for o, _ in pairs]
        for i in range(0, len(olds), 500):
            got = self.w.collection.get(ids=olds[i:i + 500], include=["embeddings", "metadatas", "documents"])
            have = {gid: j for j, gid in enumerate(got.get("ids") or [])}
            for old, new in pairs[i:i + 500]:
                j = have.get(old)
                if j is None:
                    continue
                self.w.upsert_documents(documents=[got["documents"][j]], embeddings=[got["embeddings"][j]],
                                        metadatas=[got["metadatas"][j]], ids=[new])
                done.append((old, new))
        return done

    def delete(self, ids: list) -> None:
        for i in range(0, len(ids), 500):
            self.w.collection.delete(ids=ids[i:i + 500])

    def rekey_facets(self, pairs: list) -> int:
        return self.w.facets_rekey(pairs)


async def _on_chroma(fn, *args):
    """Chroma and its facet map are blocking SQLite underneath: on the
    Chroma thread, never on the event loop."""
    from src.vector_store.chromadb_wrapper import run_chroma
    return await run_chroma(fn, *args)


def _write_settings(target: ServerKind) -> Optional[str]:
    """Flip MEDIA_SERVER_TYPE in .env; returns the old value."""
    from src.config import settings
    from src.services.setup_wizard import current_env_config, merge_env_config, write_env
    old = settings.MEDIA_SERVER_TYPE or ""
    write_env(merge_env_config(current_env_config(), {"media_server_type": "" if target == "plex" else target}))
    return old


async def apply_plan(plan: Plan, *, target_adapter, chroma=None, backup_root: Optional[Path] = None,
                     settings_writer=_write_settings, sync_state: bool = False) -> Path:
    from src.database.runner import run_read, run_write
    from src.services import scheduler
    if plan.blocking:
        raise RuntimeError("; ".join(plan.blocking))
    root = backup_root or default_backup_root()
    root.mkdir(parents=True, exist_ok=True)
    if (m := running_manifest(root)) is not None:
        raise RuntimeError(f"an unfinished rebind is on record ({m}) - finish it with --undo first")
    folder = root / f"rebind-{datetime.utcnow():%Y%m%d-%H%M%S}"
    folder.mkdir()
    snap = str(folder / "curatarr.db")
    if not await run_read(scheduler._snapshot_sqlite, _db_path(), snap):
        raise RuntimeError("the database backup failed its integrity check - nothing was changed")
    pairs = dict(plan.items)
    users = {sid: tid for _, (sid, tid, _) in plan.users.items()}
    libs = {s: t for s, (t, _) in plan.libraries.items()}
    manifest = {"status": "running", "from": plan.source, "to": plan.target,
                "created": datetime.utcnow().isoformat(), "backup": snap,
                "pairs": pairs, "users": users, "libraries": libs,
                "library_titles": {s: t for s, (_, t) in plan.libraries.items()},
                "tables": {}, "proposals": [], "chroma": [], "facets": 0,
                "app_state": {}, "cache": [], "settings": None}
    mpath = folder / "manifest.json"
    _save(mpath, manifest)

    # Vectors first, under the new ids: harmless if anything below fails.
    chroma = chroma or await _on_chroma(ChromaStore)

    enriched = await run_read(_enrichment_keys)
    doc_pairs = {o: n for o, n in pairs.items() if o in enriched}
    vec_pairs = list(doc_pairs.items())
    manifest["doc_pairs"] = doc_pairs
    manifest["chroma"] = await _on_chroma(chroma.copy, vec_pairs)
    _save(mpath, manifest)

    def _db() -> dict:
        from src.services.media_server import jellyfin_adapter  # noqa: F401 - deep links below
        out = {"tables": {}, "proposals": [], "app_state": {}, "cache": [], "shared_rows": {}}
        with _engine().begin() as c:
            out["shared_rows"] = {}
            for table, col in ITEM_COLUMNS:
                rows_of = {}
                out["tables"][f"{table}.{col}"] = _rekey(c, table, col, pairs, rows_of=rows_of)
                if rows_of:
                    out["shared_rows"][f"{table}.{col}"] = rows_of
            for table, col in DOC_COLUMNS:
                out["tables"][f"{table}.{col}"] = _rekey(c, table, col, doc_pairs)
            for table, col in USER_COLUMNS:
                out["tables"][f"{table}.{col}"] = _rekey(c, table, col, users)
            for table, col in LIBRARY_COLUMNS:
                out["tables"][f"{table}.{col}"] = _rekey(c, table, col, libs)
            for skey, title in manifest["library_titles"].items():
                c.execute(text("UPDATE library_config SET plex_section_title = :t WHERE plex_section_key = :k"),
                          {"t": title, "k": libs[skey]})
            excl = ", ".join(f"'{s}'" for s in OPEN_PROPOSALS_EXCLUDE)
            for pid, mid, url in c.execute(text(
                    f"SELECT id, media_id, arr_url FROM deletion_proposals WHERE service = :s AND status NOT IN ({excl})"),  # nosec B608 - the excluded statuses are a constant tuple
                    {"s": plan.source}).fetchall():
                if mid in pairs:
                    link = target_adapter.deep_link(pairs[mid])
                    c.execute(text("UPDATE deletion_proposals SET media_id = :m, service = :sv, arr_url = :u WHERE id = :i"),
                              {"m": pairs[mid], "sv": plan.target, "u": link, "i": pid})
                    out["proposals"].append({"id": pid, "media_id": mid, "arr_url": url})
            for key, value in c.execute(text("SELECT key, value FROM app_state")).fetchall():
                if key.startswith(DROP_STATE_PREFIXES):
                    out["app_state"][key] = value
                    c.execute(text("DELETE FROM app_state WHERE key = :k"), {"k": key})
        return out

    res = await run_write(_db)
    manifest.update(res)
    _save(mpath, manifest)

    # File both walks and the users in the id memory (additive).
    from src.database.connection import get_db_session
    from src.services.media_server import id_map

    def _file():
        with get_db_session() as db:
            # The source's ids too, users included: the way back needs them.
            id_map.upsert(db, plan.source, plan.source_rows + [
                ("user", f"user:{uid}", sid) for uid, (sid, _, _) in plan.users.items()])
            id_map.upsert(db, plan.target, plan.target_rows + [
                ("user", f"user:{uid}", tid) for uid, (_, tid, _) in plan.users.items()])
    await run_write(_file)

    manifest["facets"] = await _on_chroma(chroma.rekey_facets, vec_pairs)
    await _on_chroma(chroma.delete, [o for o, _ in manifest["chroma"]])
    manifest["cache"] = await run_write(_rename_prefetch, pairs)
    manifest["settings"] = settings_writer(plan.target)
    manifest["status"] = "completed"
    manifest["finished"] = datetime.utcnow().isoformat()
    _save(mpath, manifest)
    if sync_state:
        manifest["sync_state"] = await sync_watched_state(plan, target_adapter)
        _save(mpath, manifest)
    return mpath


def _rename_prefetch(pairs: dict) -> list:
    """``raw_prefetch:{key}`` is keyed by the server key alone - the one
    cache family safe to rename."""
    from src.cache.metadata_cache import MetadataCache
    done = []
    with MetadataCache() as mc:
        for old, new in pairs.items():
            r = mc.conn.execute("UPDATE OR IGNORE api_cache SET cache_key = ? WHERE cache_key = ?",
                                (f"raw_prefetch:{new}", f"raw_prefetch:{old}"))
            if r.rowcount:
                done.append([old, new])
        mc.conn.commit()
    return done


async def sync_watched_state(plan: Plan, target_adapter) -> dict:
    """``--sync-state``: Curatarr's finished plays become played marks on the
    target, Plex ratings of 8/10 and up become Jellyfin likes. Only towards
    Jellyfin: Plex marks per account token, which Curatarr does not hold
    for every member."""
    from src.database.runner import run_read
    if plan.target != "jellyfin":
        return {"skipped": "only towards Jellyfin"}

    def _load():
        with _engine().connect() as c:
            watched = c.execute(text("SELECT DISTINCT user_id, plex_item_id FROM watch_history "
                                     "WHERE completed = 1 AND orphaned_at IS NULL")).fetchall()
            liked = c.execute(text("SELECT user_id, plex_item_id FROM plex_ratings WHERE rating >= 8")).fetchall()
        return watched, liked
    watched, liked = await run_read(_load)
    marked = likes = failed = 0
    for uid, (_, tid, _) in plan.users.items():
        who = ServerUser(key=tid, name="")
        already = {s.item_key for s in await target_adapter.user_states(who)}
        for u, key in watched:
            if u == uid and is_jellyfin_key(key) and key not in already:
                try:
                    await target_adapter.mark_played(who, key)
                    marked += 1
                except Exception as e:  # noqa: BLE001 - one item's failure must not stop the rest; counted
                    failed += 1
                    logger.warning("[rebind] %s not marked played: %s", key, e)
        for u, key in liked:
            if u == uid and is_jellyfin_key(key):
                try:
                    await target_adapter.set_like(who, key)
                    likes += 1
                except Exception as e:  # noqa: BLE001 - counted, the rest goes on
                    failed += 1
                    logger.warning("[rebind] %s not liked: %s", key, e)
    return {"marked_played": marked, "liked": likes, "failed": failed}


async def undo(mpath: Path, *, chroma=None, settings_writer=None) -> dict:
    """Replay a manifest backwards, by value, so rows written after the
    switch go back too; delete the Chroma copies under the new ids."""
    from src.database.runner import run_write
    manifest = json.loads(Path(mpath).read_text(encoding="utf-8"))
    if manifest.get("status") == "undone":
        return {"skipped": "already undone"}
    shared = _shared_targets(manifest["pairs"])
    back = {n: o for o, n in manifest["pairs"].items() if n not in shared}
    doc_back = {n: o for o, n in (manifest.get("doc_pairs") or {}).items()}
    users = {n: o for o, n in manifest["users"].items()}
    libs = {n: o for o, n in manifest["libraries"].items()}

    def _db() -> dict:
        out = {}
        with _engine().begin() as c:
            # Rows that went into a shared target go back to their own source
            # key by id; what arrived there after the switch goes to the first.
            for colname, rows_of in (manifest.get("shared_rows") or {}).items():
                table, _, col = colname.partition(".")
                if (table, col) not in ITEM_COLUMNS:     # a manifest names only our own columns
                    raise ValueError(f"manifest names an unknown column: {colname!r}")
                for old, ids in rows_of.items():
                    for i in range(0, len(ids), 500):
                        chunk = ids[i:i + 500]
                        c.execute(text(f"UPDATE OR IGNORE {table} SET {col} = :old WHERE id IN "  # nosec B608 - (table, col) checked against ITEM_COLUMNS; ids cast to int
                                       f"({','.join(str(int(x)) for x in chunk)})"), {"old": old})
            first = {}
            for o, n in sorted(manifest["pairs"].items()):
                if n in shared:
                    first.setdefault(n, o)
            for table, col in ITEM_COLUMNS:
                out[f"{table}.{col}"] = _rekey(c, table, col, {**back, **first})
            for table, col in DOC_COLUMNS:
                out[f"{table}.{col}"] = _rekey(c, table, col, doc_back)
            for table, col in USER_COLUMNS:
                out[f"{table}.{col}"] = _rekey(c, table, col, users)
            for table, col in LIBRARY_COLUMNS:
                out[f"{table}.{col}"] = _rekey(c, table, col, libs)
            for p in manifest.get("proposals") or []:
                c.execute(text("UPDATE deletion_proposals SET media_id = :m, service = :s, arr_url = :u WHERE id = :i"),
                          {"m": p["media_id"], "s": manifest["from"], "u": p["arr_url"], "i": p["id"]})
            for key, value in (manifest.get("app_state") or {}).items():
                c.execute(text("INSERT OR REPLACE INTO app_state (key, value) VALUES (:k, :v)"), {"k": key, "v": value})
        return out

    res = await run_write(_db)
    chroma = chroma or await _on_chroma(ChromaStore)
    copies = [(n, o) for o, n in manifest.get("chroma") or []]
    await _on_chroma(chroma.copy, copies)                  # back under the old ids
    await _on_chroma(chroma.rekey_facets, list(doc_back.items()))
    await _on_chroma(chroma.delete, [n for n, _ in copies])  # no twin vectors left behind
    await run_write(_rename_prefetch, back)
    if settings_writer is None:
        settings_writer = _write_settings
    settings_writer(manifest["from"])
    manifest["status"] = "undone"
    manifest["undone"] = datetime.utcnow().isoformat()
    _save(Path(mpath), manifest)
    return {"tables": res, "vectors": len(copies)}


def server_adapters(source: ServerKind, target: ServerKind, settings=None) -> tuple:
    """``(source adapter or None, target adapter)`` from the settings: both
    servers must be configured for a rebind; the source may be down."""
    if settings is None:
        from src.config import settings
    from src.services.media_server.jellyfin_adapter import JellyfinAdapter
    from src.services.media_server.plex_adapter import PlexAdapter

    def build(kind):
        if kind == "plex":
            if settings.effective_plex_url and settings.effective_plex_token:
                return PlexAdapter(settings.effective_plex_url, settings.effective_plex_token)
            return None
        if settings.JELLYFIN_URL and settings.JELLYFIN_API_KEY:
            return JellyfinAdapter(str(settings.JELLYFIN_URL), settings.JELLYFIN_API_KEY.get_secret_value())
        return None
    tgt = build(target)
    if tgt is None:
        raise RuntimeError(f"{target} is not configured - set its URL and key first")
    return build(source), tgt


def normalize_flag(value: str) -> str:
    return unicodedata.normalize("NFC", value or "").strip()
