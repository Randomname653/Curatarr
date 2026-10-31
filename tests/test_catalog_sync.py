"""Tests for catalog mode (Modell B): SoulSync→Lidarr sync job, disarmed
adds, freshness guard + mass-drift breaker in the deletion path.

    python tests/test_catalog_sync.py
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.routers.library as lib
import src.routers.recommendations as recs
import src.services.app_state as app_state
import src.services.music_catalog_sync as mcs
import src.services.soulsync_client as sc

from _checks import check  # noqa: E402


# ── fakes ────────────────────────────────────────────────────────────────────

_state = {}
app_state.set_state = lambda k, v: _state.__setitem__(k, v)

PAGES = [
    {"artists": [
        {"name": "Existing Artist", "musicbrainz_id": "mb-1", "album_count": 3},
        {"name": "New Artist", "musicbrainz_id": "mb-2", "album_count": 2},
        {"name": "No MBID Artist", "musicbrainz_id": None, "album_count": 1},
    ], "pagination": {"has_next": True}},
    {"artists": [
        {"name": "Ghost Folder Artist", "musicbrainz_id": "mb-3", "album_count": 4},
        {"name": "Grown Artist", "musicbrainz_id": "mb-4", "album_count": 5},
    ], "pagination": {"has_next": False}},
]


async def fake_pages(page=1, limit=100):
    return PAGES[page - 1] if page <= len(PAGES) else None


sc._configured = lambda: True
sc.list_artists_page = fake_pages

added = []
refreshed = []


class FakeLidarr:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False

    async def get_artists(self):
        return [
            {"id": 11, "artistName": "Existing Artist", "foreignArtistId": "mb-1",
             "statistics": {"trackFileCount": 40, "albumCount": 3}},
            {"id": 22, "artistName": "Ghost Folder Artist", "foreignArtistId": "mb-3",
             "statistics": {"trackFileCount": 0, "albumCount": 4}},   # SS: 4 albums
            {"id": 33, "artistName": "Grown Artist", "foreignArtistId": "mb-4",
             "statistics": {"trackFileCount": 10, "albumCount": 2}},  # SS: 5 albums
        ]

    async def add_artist(self, **kw):
        added.append(kw)
        return {"id": 999}

    async def refresh_artist(self, artist_id):
        refreshed.append(artist_id)
        return {"id": 1}


lib._get_arr_url_key = lambda svc: ("http://lidarr", "key")
lib._make_client = lambda svc, url, key: FakeLidarr()
lib._read_defaults = lambda svc: {"root_folder_path": "/music",
                                  "quality_profile_id": 1,
                                  "metadata_profile_id": 1}

# No deletions on record for the main scenario (the real DB stays untouched).
_real_tombstones = mcs.music_tombstones
mcs.music_tombstones = lambda: (set(), set())

res = asyncio.run(mcs.sync_soulsync_to_lidarr())
check("sync paginates the full catalogue",
      res["soulsync_artists"] == 5 and res["lidarr_artists"] == 3)
check("only the missing artist WITH an mbid is added",
      len(added) == 1 and added[0]["mbid"] == "mb-2")
check("the add is fully DISARMED (monitored/monitor/search/new-items)",
      added[0]["monitored"] is False
      and added[0]["monitor_option"] == "none"
      and added[0]["search_for_missing"] is False
      and added[0]["monitor_new_items"] == "none")
check("mbid-less artists counted, not added", res["no_mbid"] == 1)
check("file-less artist with SoulSync albums lands on the mismatch list",
      res["path_mismatches"] == ["Ghost Folder Artist"])
# the healing queue (owner cases 1+2): new add scans first, then the
# SoulSync-filled empty artist, then the album-count-behind artist
check("surgical refreshes queued: added + file-less + grown",
      refreshed == [999, 22, 33] and res["refreshes_queued"] == 3)
check("grown artist reported", res["grown_artists"] == ["Grown Artist"])
check("healthy artist NOT refreshed", 11 not in refreshed)
check("state stamped", mcs._STATE_KEY in _state)

# A deleted artist SoulSync still lists (until its weekly deep scan) is never
# re-added: a direct Lidarr add ignores the delete's import-list exclusion,
# so the nightly sync resurrected every artist the owner had just deleted.
added.clear()
refreshed.clear()
mcs.music_tombstones = lambda: ({"mb-2"}, set())
res_t = asyncio.run(mcs.sync_soulsync_to_lidarr())
check("a deleted artist (by MBID) is not re-added to Lidarr",
      added == [] and res_t["skipped_deleted"] == 1)
added.clear()
mcs.music_tombstones = lambda: (set(), {sc.norm_name("new artist")})
res_t = asyncio.run(mcs.sync_soulsync_to_lidarr())
check("...nor one whose ban has no MBID, matched by normalised name",
      added == [] and res_t["skipped_deleted"] == 1)
mcs.music_tombstones = _real_tombstones

walked = []
real_pages, real_key = sc.list_artists_page, lib._get_arr_url_key
sc.list_artists_page = lambda page=1, limit=100: walked.append(page) or fake_pages(page, limit)
lib._get_arr_url_key = lambda svc: (None, None)
res_nl = asyncio.run(mcs.sync_soulsync_to_lidarr())
check("no Lidarr -> clean skip before the SoulSync walk",
      res_nl == {"ok": True, "skipped": "no_lidarr"} and walked == [])
sc.list_artists_page, lib._get_arr_url_key = real_pages, real_key

sc._configured = lambda: False
res2 = asyncio.run(mcs.sync_soulsync_to_lidarr())
check("no SoulSync -> clean skip (task never stays due)",
      res2 == {"ok": True, "skipped": "no_soulsync"})

# ── freshness guard integration in _delete_one_and_log ───────────────────────

from sqlalchemy.orm import sessionmaker

from src.database.models import Base, DeletionProposal

from contextlib import contextmanager
from sqlalchemy.pool import StaticPool

# StaticPool + recs' sessions on it: the delete path opens its own sessions
# on the DB worker threads
from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1)
db = sessionmaker(bind=engine)()
db.add(DeletionProposal(id=1, user_id=1, title="Stale Band", service="lidarr",
                        media_id="77", reason="r", confidence=0.8,
                        storage_mb=1000, status="pending", category="music"))
db.commit()


@contextmanager
def _test_session():
    s = sessionmaker(bind=engine)()
    try:
        yield s
        s.commit()
    finally:
        s.close()


_real_session = recs.get_db_session
recs.get_db_session = _test_session

exec_calls = []


async def fake_exec(p):
    exec_calls.append(p.title)
    return True


def fake_stance(dbx, uid, pid, fallback_pitch=None):
    return (fallback_pitch or "s", "CONFIRMED")


recs._execute_arr_delete = fake_exec
recs._latest_curator_stance_for_proposal = fake_stance


async def guard_drift(p):
    return "no track files after refresh"


recs._lidarr_freshness_guard = guard_drift
p = recs._load_proposal(1, 1)
ok = asyncio.run(recs._delete_one_and_log(1, p))
check("drift -> delete BLOCKED, parked in limbo, arr never called",
      ok is False and p.status == "limbo" and exec_calls == [])


async def guard_ok(p):
    p.storage_mb = 512.0   # trued-up from per-file sizes
    return None


recs._lidarr_freshness_guard = guard_ok
p.status = "pending"
ok = asyncio.run(recs._delete_one_and_log(1, p))
check("clean guard -> delete proceeds with trued-up size",
      ok is True and p.status == "deleted" and exec_calls == ["Stale Band"]
      and p.storage_mb == 512.0)
recs.get_db_session = _real_session

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
ac = (root / "src/services/arr_client.py").read_text(encoding="utf-8")
check("lidarr client gained refresh/command/trackfiles helpers",
      "def refresh_artist" in ac and "def get_trackfiles" in ac
      and "monitor_new_items" in ac)
rec_src = (root / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("single-approve response reports guard-limbo",
      '"limbo": p.status == "limbo"' in rec_src)
check("bulk has the mass-drift breaker",
      "drift_hits >= 3" in rec_src and "mass drift" in rec_src)
dc = (root / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("nightly catalog-sync task registered (24h, no LLM)",
      '"music_catalog_sync", "SoulSync→Lidarr catalog sync", 24.0' in dc)
ss = (root / "src/services/soulsync_client.py").read_text(encoding="utf-8")
# Read-only but for one write the owner agreed to on 2026-10-09: a deleted
# artist goes on SoulSync's blocklist. Never the download trigger.
check("soulsync: the one POST is the blocklist ban, never the download trigger",
      ss.count(".post(") == 1 and 'c.post(_root() + "/api/blocklist"' in ss
      and '"/request"' not in ss and "'/request'" not in ss
      and ".put(" not in ss and ".patch(" not in ss and ".delete(" not in ss)

check.done()
