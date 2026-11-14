"""The night's cache snapshot (services/cache_snapshot): the cache databases
and the vector store kept once in backups/caches, consistent, swapped in
whole, skipped when the disk is short, and never failing the DB backup.

    python tests/test_cache_snapshot.py
"""
from _harness import check, TMP  # noqa: I001 - first: temp stores

import asyncio
import sqlite3

from src.config import settings
from src.services import cache_snapshot as cs
from src.services import scheduler as sched

root = TMP / "snap"
cache = root / "data" / "cache"
chroma = root / "data" / "chromadb"
bdir = root / "data" / "backups"
for d in (cache, chroma / "seg-1", bdir):
    d.mkdir(parents=True, exist_ok=True)


def _db(path, rows, keep_open=False):
    c = sqlite3.connect(path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("CREATE TABLE IF NOT EXISTS t (v TEXT)")
    c.executemany("INSERT INTO t VALUES (?)", [(f"row{i}",) for i in range(rows)])
    c.commit()
    return c if keep_open else c.close()


# A writer still holds enrichment.db open with rows only in its -wal: the
# snapshot must carry them (the backup API reads through the WAL).
live = _db(cache / "enrichment.db", 40, keep_open=True)
_db(cache / "plex_music.db", 5)
(cache / "empty.db").write_bytes(b"")
_db(chroma / "chroma.sqlite3", 7)
(chroma / "seg-1" / "data_level0.bin").write_bytes(b"\x01" * 2048)
(chroma / "stray.sqlite3-wal").write_bytes(b"junk")

settings.ENRICHMENT_CACHE = str(cache / "enrichment.db")
settings.CHROMADB_PATH = str(chroma)

msg = asyncio.run(cs.snapshot_caches(str(bdir)))
out = bdir / "caches"


def rows(p):
    c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    try:
        return c.execute("select count(*) from t").fetchone()[0]
    finally:
        c.close()


check("the cache databases are kept, an empty file is not", sorted(p.name for p in (out / "cache").iterdir())
      == ["enrichment.db", "plex_music.db"], msg)
check("a database a writer still holds open is kept with what its WAL holds",
      rows(out / "cache" / "enrichment.db") == 40)
check("the vector store: its SQLite through the backup API, its index folders copied, no -wal",
      rows(out / "chromadb" / "chroma.sqlite3") == 7
      and (out / "chromadb" / "seg-1" / "data_level0.bin").read_bytes() == b"\x01" * 2048
      and not list((out / "chromadb").glob("*-wal")))
check("the summary says what was kept", msg.startswith("caches: 2 databases + vector store"), msg)

# A second night replaces it whole; nothing half-written stays behind.
live.executemany("INSERT INTO t VALUES (?)", [("more",)] * 5)
live.commit()
msg = asyncio.run(cs.snapshot_caches(str(bdir)))
check("the next snapshot replaces the last, nothing temporary left",
      rows(out / "cache" / "enrichment.db") == 45
      and sorted(p.name for p in bdir.iterdir()) == ["caches"], sorted(p.name for p in bdir.iterdir()))

# Short of room: skipped, and the last good snapshot stays.
_real_free = sched._free_bytes
sched._free_bytes = lambda path: 0
msg = asyncio.run(cs.snapshot_caches(str(bdir)))
check("too little disk: skipped, the last snapshot kept", msg == "caches: skipped (disk)"
      and rows(out / "cache" / "enrichment.db") == 45, msg)
sched._free_bytes = _real_free

# A database whose copy fails its check is named and left out.
_real_snap = sched._snapshot_sqlite


def _fails_music(src, dst):
    ok = _real_snap(src, dst)
    return ok and not src.endswith("plex_music.db")


sched._snapshot_sqlite = _fails_music
msg = asyncio.run(cs.snapshot_caches(str(bdir)))
check("a copy that fails its check is named, not kept",
      "not kept (check failed): plex_music.db" in msg and not (out / "cache" / "plex_music.db").exists(), msg)


# The vector store failing: the whole snapshot is dropped, the last one stays.
def _boom(src, dst):
    raise sqlite3.DatabaseError("disk image is malformed")


sched._snapshot_sqlite = _boom
msg = asyncio.run(cs.snapshot_caches(str(bdir)))
check("a failure says so, leaves no temp folder and keeps the last snapshot",
      msg.startswith("caches: failed") and not (bdir / "caches.tmp").exists()
      and (out / "cache" / "enrichment.db").exists(), msg)
sched._snapshot_sqlite = _real_snap
live.close()

check.done()
