"""The music pipeline, proactive messages, curator principles, the lyrics
profiler and the music cache helpers run their database work off the event
loop.

Each scenario drives a converted coroutine against a temp file database with
the loop guard in raise mode, then checks the side effect is unchanged:

  * match_spotify_to_plex: cursor read, section list, play scan, chunk writes
    and the cursor write go through workers; matches and cursor land
  * resolve_artist_mbids: the work list, the miss filter, progress, the MBID
    write-back, the miss record - all on workers; MBID and miss are stored
  * check_and_generate_messages / get_unread_messages / mark_message_read:
    the message is stored, the impression counted, the read flag set
  * detect_and_apply_principle_verdict / retrieve_principles: the revised
    wording, its embedding and the activation land; retrieval returns it
  * run_lyrics_profiles: the listen counts (watch history) and every cache /
    plex_music.db call run on workers; the profile is stored and attached
  * fetch_musicbrainz_artist / topup_wikidata: no cache call on the loop thread

    python tests/test_loop_free_g5.py
"""
import asyncio
import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database import loop_guard  # noqa: E402
from src.database.models import (Base, CuratorPrinciple, LibraryConfig,  # noqa: E402
                                 MusicLookupMiss, ProactiveMessage, User,
                                 WatchHistoryEntry)
from src.services import app_state  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


tmp = Path(tempfile.mkdtemp())
eng = create_engine(f"sqlite:///{(tmp / 'g5.db').as_posix()}",
                    connect_args={"check_same_thread": False})
Base.metadata.create_all(eng)
conn_mod.engine = eng
conn_mod.SessionLocal.configure(bind=eng)
loop_guard.install(eng)
loop_guard.serving(True)

import src.services.curator_principles as cp  # noqa: E402
import src.services.lyrics as ly  # noqa: E402
import src.services.music_matcher as mm  # noqa: E402
import src.services.music_metadata as mmeta  # noqa: E402
import src.services.proactive_messages as pm  # noqa: E402
import src.services.wikidata as wd  # noqa: E402

MINE = ("music_matcher.py", "proactive_messages.py", "curator_principles.py",
        "music_metadata.py", "album_dossier.py", "reception.py",
        "music_catalog_sync.py", "lyrics.py", "archive_backfill.py", "wikidata.py")


def _my_sites():
    return [s["site"] for s in loop_guard.snapshot()["sites"]
            if any(f in s["site"] for f in MINE)]


def run_guarded(coro_fn):
    """Run one coroutine in raise mode. Returns (result, error, my_sites)."""
    loop_guard.reset()
    os.environ["CURATARR_LOOP_GUARD"] = "raise"
    try:
        try:
            return asyncio.run(coro_fn()), None, _my_sites()
        except Exception as e:  # noqa: BLE001 - reported by the check
            return None, e, _my_sites()
    finally:
        os.environ.pop("CURATARR_LOOP_GUARD", None)


def ok(err, sites):
    return err is None and sites == []


with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    db.add(LibraryConfig(plex_section_key="5", plex_section_title="Music",
                         plex_section_type="artist", media_category="music"))
    db.commit()


# ── 1. Spotify -> Plex match ────────────────────────────────────────────────
print("music_matcher.match_spotify_to_plex")
with conn_mod.get_db_session() as db:
    for i in range(6):
        db.add(WatchHistoryEntry(
            user_id=1, plex_user_id="1", plex_item_id=f"spotify:{i}",
            title=f"Track {i}", series_title=f"Artist {i}", media_type="music",
            source="spotify", viewed_at=datetime(2026, 1, 1)))
    db.commit()

TRACKS = [{"ratingKey": 1000 + i, "title": f"Track {i}", "grandparentTitle": f"Artist {i}",
           "Genre": [{"tag": "rock"}]} for i in range(0, 6, 2)]


class _PlexResp:
    status_code = 200

    def json(self):
        return {"MediaContainer": {"Metadata": TRACKS}}


class _PlexClient:
    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        return _PlexResp()


saved = (mm.httpx, mm.settings)
mm.httpx = SimpleNamespace(AsyncClient=_PlexClient, HTTPError=Exception)
mm.settings = SimpleNamespace(effective_plex_url="http://plex", effective_plex_token="t",
                              PLEX_CLIENT_ID="test")
try:
    res, err, sites = run_guarded(lambda: mm.match_spotify_to_plex(1))
finally:
    mm.httpx, mm.settings = saved
check(f"no statement on the loop ({err!r}, {sites})", ok(err, sites))
with conn_mod.get_db_session() as db:
    keys = sorted(r[0] for r in db.query(WatchHistoryEntry.plex_item_id))
check("the even plays carry their Plex ratingKey, the odd ones stay",
      res == {"matched": 3, "unmatched": 3, "skipped": 0}
      and keys == ["1000", "1002", "1004", "spotify:1", "spotify:3", "spotify:5"])
check("the cursor advanced", (app_state.get_state("music_match_cursor:1") or "0") != "0")


# ── 2. MusicBrainz MBID pre-resolve ─────────────────────────────────────────
print("music_matcher.resolve_artist_mbids")


async def _fake_mb(name, mbid=None, rejected=None):
    return {"mbid": f"mbid-{name}"} if name == "Artist 1" else None


saved_mb = mmeta.fetch_musicbrainz_artist
mmeta.fetch_musicbrainz_artist = _fake_mb
try:
    res, err, sites = run_guarded(lambda: mm.resolve_artist_mbids(1, batch=10))
finally:
    mmeta.fetch_musicbrainz_artist = saved_mb
check(f"no statement on the loop ({err!r}, {sites})", ok(err, sites))
with conn_mod.get_db_session() as db:
    mbid = db.query(WatchHistoryEntry.artist_mbid).filter(
        WatchHistoryEntry.series_title == "Artist 1").scalar()
    misses = {r[0] for r in db.query(MusicLookupMiss.name)}
check("the MBID is written back, a no-match is parked as a miss",
      res and res["resolved"] == 1 and mbid == "mbid-Artist 1"
      and "Artist 3" in misses and "Artist 1" not in misses)
check("the progress panel saw the phase finish",
      json.loads(app_state.get_state("music_pipeline_progress") or "{}").get("phase")
      == "mbid_resolve_done")


# ── 3. proactive messages ───────────────────────────────────────────────────
print("proactive_messages")
with conn_mod.get_db_session() as db:
    now = datetime.utcnow()
    for d in range(4):        # the same film four times: a rewatch
        db.add(WatchHistoryEntry(
            user_id=1, plex_user_id="1", plex_item_id=f"m{d}", title="Alien",
            media_type="movie", viewed_at=now - timedelta(days=3 + d * 20),
            duration_ms=7_000_000, view_offset_ms=7_000_000, completed=True,
            genres="Horror"))
    db.commit()


async def _fake_generate(trigger, taste_blurb, lang_directive=""):
    return f"About {trigger['type']}?"


async def _no_sonarr():
    return {}


saved_pm = (pm.generate_proactive_message, pm._get_sonarr_index)
pm.generate_proactive_message, pm._get_sonarr_index = _fake_generate, _no_sonarr
try:
    res, err, sites = run_guarded(lambda: pm.check_and_generate_messages(1))
finally:
    pm.generate_proactive_message, pm._get_sonarr_index = saved_pm
check(f"generation: no statement on the loop ({err!r}, {sites})", ok(err, sites))
with conn_mod.get_db_session() as db:
    stored = db.query(ProactiveMessage).filter(ProactiveMessage.user_id == 1).count()
check("at least one message is generated and stored", res and res >= 1 and stored == res)

res, err, sites = run_guarded(lambda: pm.get_unread_messages(1, seen=True))
check(f"unread: no statement on the loop ({err!r}, {sites})", ok(err, sites))
first = (res or {}).get("message") or {}
with conn_mod.get_db_session() as db:
    imp = db.query(ProactiveMessage.impressions).filter(
        ProactiveMessage.id == first.get("id")).scalar()
check("the head of the queue comes back as plain data, its impression counted",
      res and res["total"] == stored and first.get("message", "").startswith("About ")
      and imp == 1)

res, err, sites = run_guarded(lambda: pm.mark_message_read(first.get("id"), 1))
check(f"mark read: no statement on the loop ({err!r}, {sites})", ok(err, sites))
with conn_mod.get_db_session() as db:
    read = db.query(ProactiveMessage.read).filter(ProactiveMessage.id == first.get("id")).scalar()
check("the message is marked read", res is True and read is True)


# ── 4. curator principles ───────────────────────────────────────────────────
print("curator_principles")
with conn_mod.get_db_session() as db:
    p = CuratorPrinciple(user_id=1, text="Keep slow films with technical mastery.",
                         status="shadow", novelty="new", created_at=datetime.utcnow())
    db.add(p)
    db.commit()
    pid = p.id
REVISED = "Keep slow films only when their craft is exceptional."


async def _verdict(system, user, schema, num_predict=800):
    return {"decision": "adopt_revised", "revised_text": REVISED}


async def _emb(text):
    return [0.5, 0.25]


saved_cp = (cp._curator_json, cp._embed)
cp._curator_json, cp._embed = _verdict, _emb
try:
    res, err, sites = run_guarded(lambda: cp.detect_and_apply_principle_verdict(
        1, pid, "yes, but reword it", "agreed"))
    with conn_mod.get_db_session() as db:
        row = db.get(CuratorPrinciple, pid)
        got = (row.status, row.text, row.embedding_json)
    check(f"verdict: no statement on the loop ({err!r}, {sites})", ok(err, sites))
    check("the owner's wording, its embedding and the activation land",
          got == ("active", REVISED, json.dumps([0.5, 0.25])))
    res, err, sites = run_guarded(lambda: cp.retrieve_principles(1))
    check(f"retrieve: no statement on the loop ({err!r}, {sites})", ok(err, sites))
    check("and retrieval returns it", res == [REVISED])
finally:
    cp._curator_json, cp._embed = saved_cp


# ── 5. lyrics profiler ──────────────────────────────────────────────────────
print("lyrics.run_lyrics_profiles")
lyr_db = tmp / "plex_music.db"
_tracks, _texts = [], {}
for i in range(10):
    key, skey = f"t{i}", f"/library/streams/t{i}"
    _tracks.append({"ratingKey": key, "grandparentRatingKey": "a1", "parentRatingKey": "al1",
                    "grandparentTitle": "The Band", "parentTitle": "First", "title": f"Song {i}",
                    "duration": 200000,
                    "Media": [{"Part": [{"Stream": [{"streamType": 4, "codec": "txt", "key": skey}]}]}]})
    _texts[skey] = f"Line one {i}\nRiver runs cold tonight\nChorus {i}\n".encode()


async def _list_tracks(sec):
    return _tracks


async def _fetch_text(k):
    return _texts.get(k)


asyncio.run(ly.sync_lyrics([("14", "Music")], _list_tracks, _fetch_text, db_path=lyr_db))
with conn_mod.get_db_session() as db:
    for _ in range(3):        # Song 7 is the most played: shown first
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="t7", title="Song 7",
                                 media_type="music", viewed_at=datetime(2026, 2, 1)))
    db.commit()

loop_threads: list = []


class _ThreadCache:
    """A MetadataCache stand-in that notes the thread of every call."""

    def __init__(self):
        self.store, self.threads = {}, []

    def _t(self):
        self.threads.append(threading.get_ident())

    def get_cache(self, key):
        self._t()
        return {"response": self.store[key]} if key in self.store else None

    def set_cache(self, key, value, days=7):
        self._t()
        if days is not None and days < 0:
            self.store.pop(key, None)
        else:
            self.store[key] = value

    def patch_cache(self, key, updates, *, drop=(), days=7):
        self._t()
        if key not in self.store:
            return False
        self.store[key] = {**self.store[key], **updates}
        return True

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


GOOD = {"lyrical_profile": "Leaving songs; river imagery.",
        "themes": ["leaving home", "rivers as time"],
        "languages": ["English"], "explicit": False, "explicit_note": "", "motifs": ["rivers"],
        "tone": ["melancholic"], "quotes": ["River runs cold tonight"]}
prompts, repolished = [], []


async def _llm(prompt):
    prompts.append(prompt)
    return GOOD


async def _repolish(artist, artist_key, mbid):
    loop_threads.append(threading.get_ident())
    repolished.append((artist, mbid))


lcache = _ThreadCache()
lcache.store["raw:music:The Band"] = {"title": "The Band", "mbid": "mb-1"}
lcache.store["enriched:music:The Band"] = {"title": "The Band", "source": "llm"}
res, err, sites = run_guarded(lambda: ly.run_lyrics_profiles(
    db_path=lyr_db, call_llm=_llm, repolish=_repolish, cache=lcache))
check(f"no statement on the loop ({err!r}, {sites})", ok(err, sites))
check("one profile made, attached, the summary expired and re-polished, all off the loop",
      ok(err, sites) and res is True and len(prompts) == 1 and repolished == [("The Band", "mb-1")]
      and lcache.store["raw:music:The Band"].get("lyrics_v") == ly._LYRICS_PROMPT_VERSION
      and "enriched:music:The Band" not in lcache.store
      and ly.stored_profile("a1", lyr_db) is not None)
check("the listen counts reached the prompt (the most played song first)",
      prompts and prompts[0].index("Song 7") < prompts[0].index("Song 0"))
check("no cache call ran on the loop thread",
      lcache.threads and loop_threads and loop_threads[0] not in lcache.threads)


# ── 6. music / wikidata cache helpers ───────────────────────────────────────
print("cache helpers")
loop_threads.clear()
mcache = _ThreadCache()
mcache.store["mb:artist:solstice"] = {"mbid": "good", "name": "Solstice"}


async def _mb_hit():
    loop_threads.append(threading.get_ident())
    return await mmeta.fetch_musicbrainz_artist("Solstice")


saved_mc = mmeta.MetadataCache
mmeta.MetadataCache = lambda *a, **k: mcache
try:
    out = asyncio.run(_mb_hit())
finally:
    mmeta.MetadataCache = saved_mc
check("musicbrainz: the cache hit is served, no cache call on the loop thread",
      out == {"mbid": "good", "name": "Solstice"} and mcache.threads
      and loop_threads[0] not in mcache.threads)

loop_threads.clear()
wcache = _ThreadCache()
wcache.store["raw:movie:Alien"] = {"title": "Alien", "imdb_id": "tt1",
                                   "wikidata_checked": True, "wikidata_v": wd._LOGIC_VERSION}


async def _wd_settled():
    loop_threads.append(threading.get_ident())
    return await wd.topup_wikidata("Alien", "movie")


saved_wc = wd.MetadataCache
wd.MetadataCache = lambda *a, **k: wcache
try:
    out = asyncio.run(_wd_settled())
finally:
    wd.MetadataCache = saved_wc
check("wikidata: a settled title is left alone, its lookup off the loop thread",
      out is False and wcache.threads and loop_threads[0] not in wcache.threads)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
