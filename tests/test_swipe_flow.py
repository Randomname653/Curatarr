"""Swipe as a flow (stage 2 of docs/UX_RETENTION_DESIGN.md, S1-S4 / §2.2):

* variety: the stack no longer runs three of a genre in a row, or one taste
  cluster for ever;
* the hook: "Because you loved X" names ONE title the member loved (a
  "Loved it" verdict, a Rediscover title) or watched recently (the because
  lane's anchors), the one nearest the card, instead of a cluster's three;
* the card's one line: the synopsis' first sentence;
* a calibration deck for a member with no profile in the category - central
  library titles, best-rated first, no other member's history - whose fifth
  seen answer builds the first profile, which a member with no plays here
  can now have.

Every title here is invented.

    python tests/test_swipe_flow.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import json
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np

conn = app_db()

from src.database.models import (  # noqa: E402
    EncryptedTasteVector, EnrichmentStatus, PostWatchVerdict, SwipeRating, User, WatchHistoryEntry)
import src.services.swipe as sw  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

NOW = datetime.utcnow()


def hit(doc_id, genre="", qi=0, dist=0.1, title=None):
    return (dist, doc_id, {"title": title or doc_id, "genres": genre}, qi)


# ── variety ─────────────────────────────────────────────────────────────────

rut = [hit(f"d{i}", "Drama") for i in range(5)] + [hit("c1", "Comedy"), hit("c2", "Comedy")]
order = [h[1] for h in sw.diverse(rut, 7)]
check("no three of one genre in a row while another is near",
      order[:4] == ["d0", "d1", "c1", "d2"], order)
one_cluster = [hit(f"a{i}", f"G{i}", qi=0) for i in range(6)] + [hit("b1", "Gx", qi=1)]
order = [h[1] for h in sw.diverse(one_cluster, 6)]
check("after four from one taste cluster, the next comes from another",
      order[:5] == ["a0", "a1", "a2", "a3", "b1"], order)
check("nothing to vary: nearest first, as before",
      [h[1] for h in sw.diverse([hit("x"), hit("y"), hit("z")], 3)] == ["x", "y", "z"]
      and sw.diverse([], 3) == [])

# ── the one line ────────────────────────────────────────────────────────────

check("the pitch is the first sentence when it is short",
      sw.pitch("A keeper finds a letter.  Then the tide turns.") == "A keeper finds a letter.")
long = "A keeper on a far island finds letters in bottles that seem to answer questions he never asked aloud " * 2
p = sw.pitch(long)
check("a long first sentence is cut at a word, with an ellipsis",
      len(p) <= 140 and p.endswith("…") and not p[:-1].endswith(" "), p)
check("no synopsis, no pitch", sw.pitch("") == "" and sw.pitch(None) == "")

# ── the hook: the nearest anchor, by centred cosine ─────────────────────────

mean = np.array([0.0, 0.0, 1.0])
anchors = [("Gull Point", "loved", [1.0, 0.0, 1.0]), ("Tide Clock", "watched", [0.0, 1.0, 1.0])]
check("the anchor nearest the card is named, with its kind",
      sw.nearest_anchor([0.9, 0.1, 1.0], anchors, mean)[:2] == ("Gull Point", "loved")
      and sw.nearest_anchor([0.1, 0.9, 1.0], anchors, mean)[:2] == ("Tide Clock", "watched"))
check("nothing close enough: no anchor (the card keeps its cluster line)",
      sw.nearest_anchor([-1.0, -1.0, 1.0], anchors, mean) is None)

with conn.get_db_session() as db:
    me = User(plex_user_id="sf-1", plex_username="Member")
    db.add(me)
    db.commit()
    UID = me.id

    def play(title, days, key):
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="sf-1", plex_item_id=key, title=title,
                                 media_type="movie", completed=True, duration_ms=1, view_offset_ms=1,
                                 viewed_at=NOW - timedelta(days=days)))

    play("Tide Clock", 5, "p-tide")                       # recent: a because-lane anchor
    play("The Quiet Harbour", 1000, "p-harbour")          # finished long ago: Rediscover's loved
    db.add(PostWatchVerdict(user_id=UID, item_key="p-gull", title="Gull Point", category="movie",
                            verdict="loved", source="followup_card"))
    db.add(PostWatchVerdict(user_id=UID, item_key="p-x", title="Salt Road", category="movie",
                            verdict="okay", source="followup_card"))
    for key, title in (("e-gull", "Gull Point"), ("e-tide", "Tide Clock"), ("e-harbour", "The Quiet Harbour")):
        db.add(EnrichmentStatus(plex_rating_key=key, title=title, media_category="movie",
                                enriched=True, enriched_at=NOW))
    db.commit()

got = dict(sw.anchor_titles(UID, "movie"))
check("anchors: a 'Loved it' verdict and a Rediscover title read as loved, a recent finish as "
      "watched; an 'okay' verdict is no anchor",
      got == {"Gull Point": "loved", "The Quiet Harbour": "loved", "Tide Clock": "watched"}, got)
check("an anchor's vector is its enrichment key's, by title",
      sw._anchor_keys("movie", ["gull point", "Tide Clock", "Nobody"])
      == {"gull point": "e-gull", "tide clock": "e-tide"})

VEC = {"e-gull": [1.0, 0.0, 1.0], "e-tide": [0.0, 1.0, 1.0], "e-harbour": [-1.0, 0.0, 1.0],
       "n1": [0.95, 0.05, 1.0], "n2": [0.05, 0.95, 1.0], "n3": [0.0, -1.0, 1.0]}


class FakeChroma:
    def get_by_ids(self, ids):
        return {i: {"id": i, "embedding": VEC[i]} for i in ids if i in VEC}

    def query(self, query_embeddings, n_results, where=None):
        if query_embeddings == [[0.0, 0.0, 1.0]]:          # calibration: the corpus mean
            return {"ids": [["k1", "k2", "k3", "p-tide", "k4"]], "distances": [[0.1, 0.2, 0.3, 0.05, 0.4]],
                    "metadatas": [[{"title": "Mid One", "genres": "Drama"}, {"title": "Mid Two", "genres": "Drama"},
                                   {"title": "Mid Three", "genres": "Comedy"}, {"title": "Tide Clock"},
                                   {"title": "Mid Four", "genres": "Drama"}]]}
        return {"ids": [["n1", "n2", "n3"]], "distances": [[0.1, 0.2, 0.3]],
                "metadatas": [[{"title": "North Signal"}, {"title": "Paper Moon Hotel"},
                               {"title": "Far Away Store"}]]}


async def _run_chroma(fn, *a, **kw):
    return fn(*a, **kw)

cw.get_chroma_db = lambda: FakeChroma()
cw.run_chroma = _run_chroma
sw._mean_cache["movie"] = (time.monotonic(), mean)
SCORES = {"k1": 6.1, "k2": 8.4, "k3": 7.9, "k4": 9.0}
sw._profile_bits = lambda cat, ids: {i: {"synopsis": f"About {i}. More.", "score": SCORES.get(i)} for i in ids}

with conn.get_db_session() as db:
    db.add(EncryptedTasteVector(user_id=UID, media_category="movie", salt="",
                                encrypted_blob=json.dumps({"embedding": [1.0, 0.0, 1.0]})))
    db.commit()
cards = {c["title"]: c for c in asyncio.run(sw.next_cards(UID, "movie", 5))}
check("a card near a loved title says so, one near a recent watch says that",
      cards["North Signal"]["anchor"]["title"] == "Gull Point"
      and cards["North Signal"]["anchor"]["kind"] == "loved"
      and cards["Paper Moon Hotel"]["anchor"] == {"title": "Tide Clock", "kind": "watched",
                                                  "fit": cards["Paper Moon Hotel"]["anchor"]["fit"]}, cards)
check("a card near nothing the member knows keeps no anchor", "anchor" not in cards["Far Away Store"])

# ── calibration ─────────────────────────────────────────────────────────────

cal = asyncio.run(sw.calibration_cards(UID, "movie", 4))
check("calibration: titles nearest the category's centre, best-rated first, genres mixed, "
      "a title the member played left out",
      [c["title"] for c in cal] == ["Mid Four", "Mid Two", "Mid Three", "Mid One"]
      and all(c["calibration"] and c["fit"] is None and c["because"] == [] for c in cal),
      [c["title"] for c in cal])
sw._mean_cache["anime"] = (time.monotonic(), None)
check("no corpus to centre on: no calibration deck", asyncio.run(sw.calibration_cards(UID, "anime", 4)) == [])

import src.routers.swipe as rs  # noqa: E402
import src.services.bg_tasks as bg  # noqa: E402
import src.services.taste_engine as te  # noqa: E402


async def _no_posters(cards, cat):
    for c in cards:
        c["pitch"] = sw.pitch(c.get("synopsis"))

rs._posters = _no_posters
sw._mean_cache["show"] = (time.monotonic(), mean)
r = asyncio.run(rs.next_cards(category="show", n=4, seed=None, skip="", user=SimpleNamespace(id=UID)))
check("/next without a profile: a calibration deck, its first build after five",
      r["reason"] == "calibration" and r["first_build_after"] == sw.CALIBRATION_FIRST_BUILD
      and r["cards"] and r["cards"][0]["pitch"] == "About " + r["cards"][0]["doc_id"] + ".", r)
r = asyncio.run(rs.next_cards(category="anime", n=4, seed=None, skip="", user=SimpleNamespace(id=UID)))
check("/next with neither profile nor corpus: no_taste, as before", r == {"cards": [], "reason": "no_taste"})

rebuilds = []


async def _fake_rebuild(uid, cats, skip_summaries=False):
    rebuilds.append((uid, cats, skip_summaries))

_saved = (te.compute_all_taste_vectors, bg.track_task)
te.compute_all_taste_vectors = _fake_rebuild
bg.track_task = lambda coro, name=None: asyncio.ensure_future(coro)


async def _answer(doc, seen=True, stars=8, cat="show"):
    out = await rs.answer(rs.SwipeAnswer(category=cat, doc_id=doc, title=doc, seen=seen, stars=stars),
                          user=SimpleNamespace(id=UID))
    await asyncio.sleep(0)
    return out

try:
    outs = [asyncio.run(_answer(f"s{i}")) for i in range(sw.CALIBRATION_FIRST_BUILD)]
    asyncio.run(_answer("u1", seen=False, stars=None))
    movie = [asyncio.run(_answer(f"m{i}", cat="movie")) for i in range(sw.CALIBRATION_FIRST_BUILD)]
finally:
    te.compute_all_taste_vectors, bg.track_task = _saved
check("calibrating (no profile): the fifth seen answer builds the first profile",
      [o["taste_rebuild"] for o in outs] == [False] * 4 + [True]
      and outs[0]["rebuild_every"] == sw.CALIBRATION_FIRST_BUILD and rebuilds[0] == (UID, ["show"], True))
check("with a profile the old pace holds: no rebuild at five",
      not any(o["taste_rebuild"] for o in movie) and movie[0]["rebuild_every"] == sw.REBUILD_EVERY)

# ── a profile from Swipe answers alone ──────────────────────────────────────

import src.cache.metadata_cache as mc_mod  # noqa: E402
import src.services.embed_service as es  # noqa: E402

with conn.get_db_session() as db:
    fresh = User(plex_user_id="sf-2", plex_username="New")
    db.add(fresh)
    db.commit()
    NEW = fresh.id
    for i in range(5):
        db.add(SwipeRating(user_id=NEW, category="anime", doc_id=f"an-{i}", title=f"Seen Elsewhere {i}",
                           seen=True, stars=9))
    db.commit()


class _FakeCache:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): pass
    def set_cache(self, *a, **k): pass
    def get_cache(self, key):
        return {"response": {"title": key, "embedding_text": "ANIME"}} if ":an-" in key else None
    def get_cache_many(self, keys):
        return {k: self.get_cache(k) for k in keys if self.get_cache(k)}


async def _embed(texts, profile=None):
    return [[0.0, 1.0, 0.0] for _ in texts]

te.MetadataCache = _FakeCache
mc_mod.MetadataCache = _FakeCache
es.embed_documents = _embed
cw.get_chroma_db = lambda: type("C", (), {"get_by_id": lambda s, i: None,
                                         "embeddings_for_domain": lambda s, d, limit=30000: []})()
res = asyncio.run(te.compute_taste_vector_for_user(NEW, category="anime"))
check("no plays in the category, five seen-it answers: a taste vector all the same, counted as swipes",
      res.get("embedding") and res["watch_count"] == 0 and res["swipe_count"] == 5, res.get("swipe_count"))

check.done()
