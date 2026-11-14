"""The night's restore point for what the database backup does not cover.

The nightly DB backup (scheduler.job_db_backup) snapshots curatarr.db; the
enrichment work lives beside it: the enrichment cache, the music index, the
lyrics, the anime maps (data/cache/*.db) and the vector store
(data/chromadb). Weeks of API calls and GPU time, with no restore point but
a hand-made copy. After the DB backup, this keeps ONE consistent snapshot of
them in backups/caches/ (the newest only: they are large and rebuilt from
the same sources):

- each cache database through SQLite's online backup API, checked like the
  main snapshot, and left as one file (out of WAL mode);
- the vector store on the Chroma thread, where every Chroma call runs, so
  nothing reads or writes it meanwhile: its SQLite files through the backup
  API, its index folders copied.

It is written to backups/caches.tmp and swapped in only when complete, and
skipped (the DB backup still counts) when the disk has less room than the
snapshot twice over. A copy of a running install can be taken from
backups/ at any time: the demo builder (scripts/demo/build_demo.py) reads
it, because a live SQLite file cannot be copied safely while the app runs.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)

DIR = "caches"


def _cache_dir() -> Path:
    from src.config import settings
    return Path(settings.ENRICHMENT_CACHE).parent


def _chroma_dir() -> Path:
    from src.config import settings
    return Path(settings.CHROMADB_PATH)


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else 0


def _cache_dbs() -> list[Path]:
    d = _cache_dir()
    return sorted(p for p in d.glob("*.db") if p.is_file() and p.stat().st_size > 0) if d.is_dir() else []


def _one_file(path: Path) -> None:
    """A snapshot is a single file: out of WAL mode, so it is copied (over a
    share, too) and opened without a -wal or -shm beside it."""
    from src.database.loop_guard import sqlite_connect
    c = sqlite_connect(str(path))
    try:
        c.execute("PRAGMA journal_mode=DELETE")
    finally:
        c.close()


def _snapshot_one(src: str, dst: str) -> bool:
    from src.services.scheduler import _snapshot_sqlite
    if not _snapshot_sqlite(src, dst):
        return False
    _one_file(Path(dst))
    return True


def _snapshot_chroma(src: Path, dst: Path) -> int:
    """Runs ON the Chroma thread: the SQLite files by the backup API, the
    index folders copied. Returns the files written."""
    n = 0
    for root, _dirs, files in os.walk(src):
        rel = Path(root).relative_to(src)
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for f in files:
            if f.endswith(("-wal", "-shm", "-journal")):
                continue
            s, d = Path(root) / f, dst / rel / f
            if f.endswith(".sqlite3"):
                if not _snapshot_one(str(s), str(d)):
                    raise RuntimeError(f"vector store {f}: snapshot failed its check")
            else:
                shutil.copy2(s, d)
            n += 1
    return n


async def snapshot_caches(bdir: str) -> str:
    """Write backups/caches; a one-line summary for the backup task. Never
    raises: the DB backup already succeeded and stands on its own."""
    from src.services.scheduler import _free_bytes
    from src.vector_store.chromadb_wrapper import run_chroma
    base = Path(bdir)
    tmp, final, old = base / f"{DIR}.tmp", base / DIR, base / f"{DIR}.old"
    dbs, chroma = _cache_dbs(), _chroma_dir()
    need = sum(p.stat().st_size for p in dbs) + _size(chroma)
    if not need:
        return "caches: nothing to keep"
    # room for this snapshot twice over, counting the one it replaces as free
    if _free_bytes(str(base)) + _size(final) < 2 * need:
        logger.warning("[backup] cache snapshot skipped: %d MB free, it needs %d MB",
                       _free_bytes(str(base)) // 2**20, 2 * need // 2**20)
        return "caches: skipped (disk)"
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        (tmp / "cache").mkdir(parents=True)
        kept, bad = 0, []
        for p in dbs:
            if await asyncio.to_thread(_snapshot_one, str(p), str(tmp / "cache" / p.name)):
                kept += 1
            else:
                bad.append(p.name)
                (tmp / "cache" / p.name).unlink(missing_ok=True)
        files = await run_chroma(_snapshot_chroma, chroma, tmp / "chromadb") if chroma.is_dir() else 0
        shutil.rmtree(old, ignore_errors=True)
        if final.exists():
            final.rename(old)
        tmp.rename(final)
        shutil.rmtree(old, ignore_errors=True)
    except Exception as e:  # noqa: BLE001 - OSError, sqlite3.Error, a failed check: the DB backup stands, say why
        shutil.rmtree(tmp, ignore_errors=True)
        logger.error("[backup] cache snapshot failed: %s: %s", type(e).__name__, e)
        return f"caches: failed ({type(e).__name__})"
    msg = f"caches: {kept} databases + vector store ({files} files, {need // 2**20} MB)"
    if bad:
        msg += f"; not kept (check failed): {', '.join(bad)}"
        logger.warning("[backup] %s", msg)
    return msg
