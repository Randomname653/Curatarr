"""MusicBrainz / Last.fm being down is not "no such artist".

``TRANSIENT`` (an upstream that did not answer) is falsy, and
``enrich_artist`` tested ``if not mb and not lfm and not ss`` — so both
upstreams down read exactly like a miss: ``None`` cached for 7 days, and
scripts/music_enricher.py stamped "no artist record found", an error its
worker never picks up again. A profile built while Last.fm was throttled
(no tags, no bio) was cached for 30 days. The track fallback cached the
artist profile for a week after a Last.fm 429.

Fake cache and fake upstreams; the script's worker runs on in-memory SQLite.

    python tests/test_music_transient.py
"""
import asyncio
import sys
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import src.services.music_metadata as mm
import src.services.soulsync_client as ssc
from src.services.enrichment_state import TRANSIENT

from _checks import check  # noqa: E402


writes: list = []          # (key, value, days)
store: dict = {}


class FakeCache:
    def get_cache(self, key):
        return {"response": store[key]} if key in store else None

    def set_cache(self, key, value, days=7):
        writes.append((key, value, days))
        store[key] = value

    def close(self):
        pass


mm.MetadataCache = FakeCache
upstream = {"mb": None, "lfm": None}


async def fake_mb(name, mbid=None):
    v = upstream["mb"]
    if isinstance(v, Exception):
        raise v
    return v


async def fake_lfm(name):
    v = upstream["lfm"]
    if isinstance(v, Exception):
        raise v
    return v


async def no_soulsync(name):
    return None

mm.fetch_musicbrainz_artist = fake_mb
mm.fetch_lastfm_artist = fake_lfm
ssc.artist_info = no_soulsync

MB_HIT = {"mbid": "m1", "type": "Group", "country": "GB", "genres": ["rock"], "tags": ["rock"]}
LFM_HIT = {"genres": ["indie"], "tags": ["indie"], "bio": "A band.", "similar_artists": ["X"]}


def enrich(mb, lfm, **kw):
    writes.clear()
    store.clear()
    upstream.update(mb=mb, lfm=lfm)
    return asyncio.run(mm.enrich_artist("Some Band", **kw))


print("\n-- enrich_artist --")
r = enrich(TRANSIENT, TRANSIENT)
check("both upstreams down -> TRANSIENT, nothing cached", r is TRANSIENT and writes == [])
r = enrich(TimeoutError("mb"), None)
check("MB raising + Last.fm miss -> TRANSIENT (no verdict without MB)", r is TRANSIENT and writes == [])
r = enrich(TRANSIENT, None)
check("MB throttled + Last.fm miss -> TRANSIENT, nothing cached", r is TRANSIENT and writes == [])
r = enrich(None, None)
check("a genuine miss is still a 7-day negative entry",
      r is None and writes == [("artist_profile:some band", None, 7)])
r = enrich(MB_HIT, TRANSIENT)
check("MB hit + Last.fm down -> profile, cached for 1 day only",
      r and r["mbid"] == "m1" and writes and writes[-1][2] == 1)
r = enrich(MB_HIT, LFM_HIT)
check("both answered -> cached for 30 days", r and writes and writes[-1][2] == 30)
r = enrich(None, TRANSIENT, skip_mb=True)
check("fast mode, Last.fm down -> TRANSIENT", r is TRANSIENT and writes == [])

print("\n-- enrich_track fallback --")
mm.settings = types.SimpleNamespace(LASTFM_API_KEY="k")
track_status = {"code": 429}


class FakeResp:
    def __init__(self, code):
        self.status_code = code

    def json(self):
        return {}


class FakeClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return FakeResp(track_status["code"])

mm.httpx = types.SimpleNamespace(AsyncClient=FakeClient)


def track(code, mb, lfm):
    writes.clear()
    store.clear()
    track_status["code"] = code
    upstream.update(mb=mb, lfm=lfm)
    return asyncio.run(mm.enrich_track("Song", "Some Band"))

r = track(429, MB_HIT, LFM_HIT)
check("Last.fm 429 on the track: fallback served, NOT cached under the track key",
      r and not any(k.startswith("track:") for k, _, _ in writes))
r = track(404, MB_HIT, LFM_HIT)
check("Last.fm 404 (no such track): fallback cached under the track key",
      any(k.startswith("track:") for k, _, _ in writes))
r = track(404, TRANSIENT, TRANSIENT)
check("a TRANSIENT fallback is never cached", r is TRANSIENT
      and not any(k.startswith("track:") for k, _, _ in writes))

# ── the script's worker ─────────────────────────────────────────────────────
print("\n-- scripts/music_enricher.py worker --")
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import music_enricher as me
from src.database.models import Base, EnrichmentStatus

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, autoflush=False)


@contextmanager
def fake_db_session():
    db = Session()
    try:
        yield db
        db.commit()
    finally:
        db.close()

me.get_db_session = fake_db_session
me.get_state = lambda k: None
me.state_lease = types.SimpleNamespace(heartbeat=lambda k: True)
answers = {"Down Band": TRANSIENT, "Real Band": {"tags": ["rock"]}, "Nobody": None}


async def fake_enrich(name):
    return answers[name]

me.enrich_artist = fake_enrich

with fake_db_session() as db:
    for i, name in enumerate(("Down Band", "Real Band", "Nobody"), start=1):
        db.add(EnrichmentStatus(id=i, plex_rating_key=f"music:{i}", title=name,
                                media_category="music", enriched=False))

n = asyncio.run(me.worker(limit=None, final=False, stop_event=asyncio.Event()))
with fake_db_session() as db:
    rows = {r.title: (r.enriched, r.error) for r in db.query(EnrichmentStatus).all()}
check("upstream-down row is left untouched for the next run",
      rows["Down Band"] == (False, None))
check("the run moved past it instead of spinning on it", rows["Real Band"][0] is True)
check("a genuine miss is still marked", rows["Nobody"][1] and "no artist record" in rows["Nobody"][1])
check("worker counted only the rows it decided", n == 2)

me._MAX_OUTAGES = 2
with fake_db_session() as db:
    db.query(EnrichmentStatus).delete()
    for i in range(1, 6):
        db.add(EnrichmentStatus(id=i, plex_rating_key=f"music:{i}", title="Down Band",
                                media_category="music", enriched=False))
asyncio.run(me.worker(limit=None, final=False, stop_event=asyncio.Event()))
with fake_db_session() as db:
    untouched = db.query(EnrichmentStatus).filter(EnrichmentStatus.error.is_(None),
                                                  EnrichmentStatus.enriched == False).count()  # noqa: E712
check("upstreams down for a run of artists: stops, marks nothing", untouched == 5)

check.done()
