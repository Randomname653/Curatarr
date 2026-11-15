"""Backups are copyable by the app's group; the live database and .env are not.

The start-up pass (connection._secure_db_files) set every SQLite file under
data/ to 0600, the backups included: a backup written at 22:47 could be
copied off the NAS share until the 23:43 restart (2026-11-14). Backups and
the cache snapshot are 0640 (folders 0750) now; the live database, its
sidecars and .env stay 0600. POSIX only: on Windows chmod sets nothing but
the read-only bit, so the checks run where the modes exist (CI, the NAS).

    python tests/test_backup_perms.py
"""
from _harness import check, TMP  # noqa: I001 - first: temp stores

import asyncio
import os
import sqlite3
import stat

import src.paths as paths
from src.config import settings
from src.database import connection
from src.services import cache_snapshot as cs

POSIX = os.name == "posix"


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


root = TMP / "perms"
data = root / "data"
for d in (data / "backups" / "caches" / "cache", data / "chromadb", data / "cache"):
    d.mkdir(parents=True, exist_ok=True)


def db(p):
    c = sqlite3.connect(p)
    c.execute("create table if not exists t (v)")
    c.commit()
    c.close()
    os.chmod(p, 0o644)


for p in (data / "curatarr.db", data / "cache" / "enrichment.db", data / "chromadb" / "chroma.sqlite3",
          data / "backups" / "curatarr_20261114_214712.db", data / "backups" / "caches" / "cache" / "lyrics.db"):
    db(p)
env = root / ".env"
env.write_text("JWT_SECRET=x\n", encoding="utf-8")

_data, _env = paths.DATA_DIR, paths.ENV_FILE
paths.DATA_DIR, paths.ENV_FILE = data, env
try:
    connection._secure_db_files()
finally:
    paths.DATA_DIR, paths.ENV_FILE = _data, _env

if POSIX:
    check("the live database, a cache and the vector store stay 0600",
          {mode(data / "curatarr.db"), mode(data / "cache" / "enrichment.db"),
           mode(data / "chromadb" / "chroma.sqlite3")} == {0o600})
    check(".env stays 0600", mode(env) == 0o600)
    check("a backup and the cache snapshot are 0640: the group may copy them",
          mode(data / "backups" / "curatarr_20261114_214712.db") == 0o640
          and mode(data / "backups" / "caches" / "cache" / "lyrics.db") == 0o640)
else:
    check("(Windows: file modes are POSIX-only; the pass ran without error)", True)

# The cache snapshot is written group-readable from the start.
src_cache = root / "live" / "cache"
src_cache.mkdir(parents=True)
db(src_cache / "enrichment.db")
settings.ENRICHMENT_CACHE = str(src_cache / "enrichment.db")
settings.CHROMADB_PATH = str(root / "live" / "chromadb")
bdir = root / "nas-backups"
bdir.mkdir()
msg = asyncio.run(cs.snapshot_caches(str(bdir)))
check("the cache snapshot is written", (bdir / "caches" / "cache" / "enrichment.db").exists(), msg)
if POSIX:
    check("...its files 0640, its folders 0750",
          mode(bdir / "caches" / "cache" / "enrichment.db") == 0o640
          and mode(bdir / "caches") == 0o750 and mode(bdir / "caches" / "cache") == 0o750)

check.done()
