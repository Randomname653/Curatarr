"""Tests for music playlists + SoulSync neighbours (Block 6).

Functional against faked Plex/SoulSync/app_state boundaries — no network.

    python tests/test_music_playlist.py
"""
import asyncio
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.database.connection as dbc
import src.services.app_state as app_state
import src.services.plex_playlists as pp
import src.services.recommendations_engine as eng
import src.services.soulsync_client as ss
from src.database.models import Base, CachedRecommendation

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


# ── neighbours: union minus favourites, cap, cache ───────────────────────────

_state = {}
app_state.get_state = lambda key, default=None: _state.get(key, default)
app_state.set_state = lambda key, value: _state.__setitem__(key, value)

calls = []


async def fake_artist_info(name):
    calls.append(name)
    return {"similar_artists": {
        "SikTh": ["Car Bomb", "Protest the Hero", "TesseracT"],
        "Car Bomb": ["SikTh", "Meshuggah", "TesseracT"],
    }.get(name, [])}


ss.artist_info = fake_artist_info

nb = asyncio.run(eng._get_music_neighbors(7, ["SikTh", "Car Bomb"]))
check("neighbours = union minus the favourites themselves",
      nb == ["Protest the Hero", "TesseracT", "Meshuggah"])
check("cache written", "soulsync_neighbors:user_id" not in _state
      and any(k.startswith("soulsync_neighbors:") for k in _state))

calls.clear()
nb2 = asyncio.run(eng._get_music_neighbors(7, ["SikTh", "Car Bomb"]))
check("fresh cache short-circuits SoulSync", nb2 == nb and calls == [])

# ── push_user_music_playlist against faked boundaries ────────────────────────

engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)
_shared = Session()


@contextmanager
def fake_db_session():
    yield _shared


dbc.get_db_session = fake_db_session   # plex_playlists imports it lazily

for i, artist in enumerate(["SikTh", "Ion Dissonance", "Ghost Artist"]):
    _shared.add(CachedRecommendation(user_id=42, category="music", lane="library",
                                     title=artist, reason="x",
                                     confidence=0.9 - i * 0.1,
                                     cached_at=datetime(2026, 8, 13, 10, 0)))
_shared.commit()

snapshots = {}
pp._load_snapshot = lambda uid: dict(snapshots.get(uid) or {})
pp._save_snapshot = lambda uid, snap: snapshots.__setitem__(uid, snap)


async def fake_resolve(name, sections=None):
    return {"SikTh": "111", "Ion Dissonance": "222"}.get(name)   # Ghost -> None


picked = []


async def fake_pick(token, artist_key):
    picked.append(artist_key)
    return f"alb-{artist_key}"


listed = [{"title": pp.MUSIC_PLAYLIST_TITLE, "ratingKey": "756000"}]
deleted, created = [], []


async def fake_list(token, playlist_type="video"):
    assert playlist_type == "audio"
    return listed


async def fake_delete(token, key):
    deleted.append(key)


async def fake_create(token, title, keys, playlist_type="video"):
    created.append({"title": title, "keys": keys, "type": playlist_type})
    return {"ratingKey": "756999", "leafCount": 26}


pp.resolve_artist_key = fake_resolve
pp.pick_album_key = fake_pick
pp.list_playlists = fake_list
pp.delete_playlist = fake_delete
pp.create_playlist = fake_create

no_token = type("U", (), {"id": 42, "plex_username": "x", "plex_token": None})()
res = asyncio.run(pp.push_user_music_playlist(no_token))
check("token-less user skipped with reason", res.get("skipped") == "no_token")

user = type("U", (), {"id": 42, "plex_username": "don", "plex_token": "tok"})()
res = asyncio.run(pp.push_user_music_playlist(user))
check("push happened", res == {"pushed": ["music"]})
check("unresolvable artist skipped, two albums pushed",
      created and created[0]["keys"] == ["alb-111", "alb-222"])
check("playlist created as AUDIO type", created[0]["type"] == "audio")
check("existing same-title playlist deleted first (find-delete-recreate)",
      deleted == ["756000"])
check("snapshot records artists",
      snapshots[42]["music"]["titles"] == ["SikTh", "Ion Dissonance"])

created.clear()
res2 = asyncio.run(pp.push_user_music_playlist(user))
check("fresh snapshot + no newer recs -> second push skipped",
      res2 == {"pushed": []} and created == [])

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
dc = (root / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("custodian task registered WITHOUT needs_llm",
      '"plex_music_playlist"' in dc
      and "needs_llm" not in dc.split('"plex_music_playlist"')[1][:200])

src_dir = root / "src"
offenders = [p.name for p in src_dir.rglob("*.py")
             if p.name != "plex_playlists.py"
             and '"type": playlist_type' in p.read_text(encoding="utf-8")]
check("audio playlist create lives only in plex_playlists.py", offenders == [])

eng_src = (root / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
check("neighbours line is music-only and taste-gated",
      'if cat == "music":' in eng_src and "only "
      in eng_src and "Graph neighbours" in eng_src)
check("soulsync stays read-only (no request endpoint)",
      "/api/v1/request" not in eng_src
      and "/api/v1/request" not in (root / "src/services/plex_playlists.py").read_text(encoding="utf-8"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
