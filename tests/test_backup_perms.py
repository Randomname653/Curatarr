"""What the nightly backup may carry, and who may read it.

* No tokens: users' Plex tokens are blanked in the backup copy, and their
  bytes overwritten (secure_delete) - a restore asks each member to sign in
  once, as Plex keeps its own token out of its database backups. The
  Jellyfin webhook secret (an app_state row) goes too.
* 0600 like the live database, unless the operator sets
  BACKUP_GROUP_READABLE (0640, folders 0750) to copy backups off a NAS
  share. The start-up pass used to force every backup to 0600 whatever was
  wanted; it now follows the same setting. The live database and .env stay
  0600 always.
* The cache snapshot only with BACKUP_CACHES; switched off, an old one goes.

POSIX modes are checked where they exist (CI, the NAS); on Windows chmod
sets only the read-only bit.

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
from src.services import scheduler as sched

POSIX = os.name == "posix"
TOKEN = "zQ7plexTokenAbc123xy"
HOOK_SECRET = "hK9jellyfinHookSecret42"


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


# ── tokens stay out of the backup ───────────────────────────────────────────
src_db = TMP / "live.db"
c = sqlite3.connect(src_db)
c.execute("PRAGMA journal_mode=WAL")
c.execute("create table users (id integer primary key, plex_username text, plex_token text)")
c.execute("create table sessions (id integer primary key, access_token text, refresh_token text)")
c.execute("insert into users (plex_username, plex_token) values ('owner', ?), ('member', ?)", (TOKEN, TOKEN[::-1]))
c.execute("create table app_state (key text primary key, value text, updated_at text)")
c.execute("insert into app_state (key, value) values ('jellyfin_hook_secret', ?), ('last_sync_at', '2026-11-17')",
          (HOOK_SECRET,))
c.commit()
c.close()
dest = TMP / "backup.db"
check("the backup copy is made and checks", sched._snapshot_sqlite(str(src_db), str(dest)))
check("...its tokens blanked and the copy still checks", sched._strip_secrets(str(dest)))
c = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
check("no user keeps a Plex token in the backup; the users themselves stay",
      c.execute("select count(*) from users where plex_token is not null").fetchone()[0] == 0
      and c.execute("select count(*) from users").fetchone()[0] == 2)
c.close()
blob = dest.read_bytes()
check("the token's bytes are gone from the file, not just unlinked (secure_delete)",
      TOKEN.encode() not in blob and TOKEN[::-1].encode() not in blob)
c = sqlite3.connect(f"file:{dest}?mode=ro", uri=True)
check("the Jellyfin webhook secret is not in the backup; the rest of app_state stays",
      c.execute("select key from app_state").fetchall() == [("last_sync_at",)])
c.close()
check("...nor its bytes", HOOK_SECRET.encode() not in blob)
from src.services.media_server import jellyfin_hook  # noqa: E402
check("the stripped key is the one the webhook route reads", jellyfin_hook.SECRET_KEY in sched._SECRET_STATE_KEYS)
check("the backup is one file (no -wal beside it)", not (TMP / "backup.db-wal").exists())
c = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True)
check("the live database keeps its tokens", c.execute("select count(*) from users where plex_token is not null").fetchone()[0] == 2)
c.close()

# ── modes ───────────────────────────────────────────────────────────────────
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


backup = data / "backups" / "curatarr_20261114_214712.db"
# A media-server switch's restore point: the whole database, tokens and all.
(data / "backups" / "rebind-20261110-120000").mkdir()
switch_copy = data / "backups" / "rebind-20261110-120000" / "curatarr.db"
for p in (data / "curatarr.db", data / "cache" / "enrichment.db", backup,
          data / "backups" / "caches" / "cache" / "lyrics.db", switch_copy):
    db(p)
env = root / ".env"
env.write_text("JWT_SECRET=x\n", encoding="utf-8")


def secure(group_readable):
    settings.BACKUP_GROUP_READABLE = group_readable
    _data, _env = paths.DATA_DIR, paths.ENV_FILE
    paths.DATA_DIR, paths.ENV_FILE = data, env
    try:
        connection._secure_db_files()
    finally:
        paths.DATA_DIR, paths.ENV_FILE = _data, _env


secure(False)
if POSIX:
    check("by default backups are 0600 like the database", mode(backup) == 0o600)
secure(True)
if POSIX:
    check("BACKUP_GROUP_READABLE: backups 0640, the cache snapshot too",
          mode(backup) == 0o640 and mode(data / "backups" / "caches" / "cache" / "lyrics.db") == 0o640)
    check("...while the live database, a cache and .env stay 0600",
          {mode(data / "curatarr.db"), mode(data / "cache" / "enrichment.db"), mode(env)} == {0o600})
    check("...and so does a switch's restore point: its tokens are not blanked",
          mode(switch_copy) == 0o600, oct(mode(switch_copy)))
if not POSIX:
    check("(Windows: file modes are POSIX-only; the pass ran without error)", True)

# ── the cache snapshot only when asked for ──────────────────────────────────
live = root / "live" / "cache"
live.mkdir(parents=True)
db(live / "enrichment.db")
settings.ENRICHMENT_CACHE = str(live / "enrichment.db")
settings.CHROMADB_PATH = str(root / "live" / "chromadb")
bdir = root / "nas-backups"
bdir.mkdir()
settings.BACKUP_CACHES = False
check("BACKUP_CACHES off (the default): no cache snapshot, no summary",
      asyncio.run(cs.snapshot_caches(str(bdir))) == "" and not (bdir / "caches").exists())
settings.BACKUP_CACHES = True
settings.BACKUP_GROUP_READABLE = False
msg = asyncio.run(cs.snapshot_caches(str(bdir)))
check("BACKUP_CACHES on: the snapshot is written", (bdir / "caches" / "cache" / "enrichment.db").exists(), msg)
if POSIX:
    check("...0600 and 0700 by default",
          mode(bdir / "caches" / "cache" / "enrichment.db") == 0o600 and mode(bdir / "caches") == 0o700)
settings.BACKUP_CACHES = False
msg = asyncio.run(cs.snapshot_caches(str(bdir)))
check("switched off again: the old snapshot (~2 GB on a big library) is removed",
      not (bdir / "caches").exists() and "removed" in msg, msg)

check.done()
