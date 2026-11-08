"""A "Loved" moves its nearest picks up (stage 2 of docs/UX_RETENTION_DESIGN.md,
S5 / L3): a Loved answer in Swipe, or "Loved it" on the landing card, lifts
the member's stored picks nearest the loved title - first in their lane, in
the next playlist push, marked on the card - and the answer says how many
moved, only ever what did.

Every title here is invented.

    python tests/test_rec_lift.py
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
    CachedRecommendation, EnrichmentStatus, ProactiveMessage, User, WatchHistoryEntry)
import src.services.rec_lift as rl  # noqa: E402
import src.services.swipe as sw  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

NOW = datetime(2026, 11, 10, 20, 0)
MEAN = np.array([0.0, 0.0, 1.0])

# ── the pure part ───────────────────────────────────────────────────────────

picks = [([1.0, 0.1, 1.0], "near"), ([0.8, 0.3, 1.0], "nearish"), ([-1.0, 0.0, 1.0], "far"),
         ([0.9, 0.0, 1.0], "nearest")]
check("nearest: the closest picks first, at most LIFT_MAX, none below LIFT_MIN",
      [x for _, x in rl.nearest([1.0, 0.0, 1.0], picks, MEAN)] == ["nearest", "near", "nearish"]
      and [x for _, x in rl.nearest([1.0, 0.0, 1.0], picks, MEAN, n=5)] == ["nearest", "near", "nearish"])
check("nothing near: nothing", rl.nearest([0.0, 1.0, 1.0], picks[:1], MEAN, min_fit=0.5) == [])

# ── lifting the stored picks ────────────────────────────────────────────────

with conn.get_db_session() as db:
    u = User(plex_user_id="rl-1", plex_username="Member")
    db.add(u)
    db.commit()
    UID = u.id

    def rec(title, lane="library", conf=0.8, key=None):
        db.add(CachedRecommendation(user_id=UID, category="movie", lane=lane, title=title,
                                    reason="r", confidence=conf, plex_rating_key=key or f"k-{title}",
                                    cached_at=NOW - timedelta(days=3)))

    rec("Top Pick", conf=0.95)
    rec("North Signal", conf=0.6)
    rec("Paper Moon Hotel", lane="because", conf=0.5)
    rec("Far Away Store", conf=0.7)
    rec("Shop Window", lane="discovery", conf=0.99)          # not owned: never lifted
    rec("Gull Point", conf=0.4)                               # the loved title itself
    for key, title in (("e-gull", "Gull Point"), ("e-north", "North Signal"), ("e-paper", "Paper Moon Hotel"),
                       ("e-far", "Far Away Store"), ("e-top", "Top Pick"), ("e-shop", "Shop Window")):
        db.add(EnrichmentStatus(plex_rating_key=key, title=title, media_category="movie",
                                enriched=True, enriched_at=NOW))
    db.commit()

VEC = {"e-gull": [1.0, 0.0, 1.0], "e-north": [0.9, 0.1, 1.0], "e-paper": [0.8, 0.2, 1.0],
       "e-far": [-1.0, 0.0, 1.0], "e-top": [0.0, 1.0, 1.0], "e-shop": [1.0, 0.0, 1.0],
       "swipe-doc": [1.0, 0.05, 1.0]}


class FakeChroma:
    def get_by_ids(self, ids):
        return {i: {"id": i, "embedding": VEC[i]} for i in ids if i in VEC}


async def _run_chroma(fn, *a, **kw):
    return fn(*a, **kw)

cw.get_chroma_db = lambda: FakeChroma()
cw.run_chroma = _run_chroma
sw._mean_cache["movie"] = (time.monotonic(), MEAN)

lifted = asyncio.run(rl.lift_near(UID, "movie", "Gull Point", now=NOW))
check("a Loved lifts the owned picks nearest it, nearest first - not the far one, not the "
      "discovery lane, not the loved title itself", lifted == ["North Signal", "Paper Moon Hotel"], lifted)
with conn.get_db_session() as db:
    marks = {r.title: (r.lifted_at, r.lifted_by) for r in db.query(CachedRecommendation)}
check("the lifted rows carry when and which title",
      marks["North Signal"] == (NOW, "Gull Point") and marks["Paper Moon Hotel"] == (NOW, "Gull Point")
      and marks["Far Away Store"] == (None, None) and marks["Shop Window"] == (None, None), marks)
check("an unknown title, or no corpus mean, lifts nothing and raises nothing",
      asyncio.run(rl.lift_near(UID, "movie", "Nobody Knows", now=NOW)) == []
      and asyncio.run(rl.lift_near(UID, "show", "Gull Point", now=NOW)) == [])

# ── where the lift shows ────────────────────────────────────────────────────

from src.routers import recommendations as recs_router  # noqa: E402
from src.services import plex_playlists  # noqa: E402

out = asyncio.run(recs_router._get_recommendations_impl("movie", 8, False, "cache", None,
                                                        SimpleNamespace(id=UID, is_admin=False)))
lib = [r["title"] for r in out["recommendations"] if r["lane"] == "library"]
check("the Recommendations page: a lifted pick leads its lane, ahead of a higher match",
      lib[:2] == ["North Signal", "Top Pick"], lib)
check("...and carries the loved title for its 'moved up' badge",
      next(r for r in out["recommendations"] if r["title"] == "North Signal")["lifted_by"] == "Gull Point"
      and next(r for r in out["recommendations"] if r["title"] == "Top Pick")["lifted_by"] is None)
rows = plex_playlists._library_recs(UID, "movie", 10)
check("the playlist push: lifted picks first, and the lift makes the playlist due again",
      [r["title"] for r in rows][:2] == ["North Signal", "Top Pick"]
      and rows[0]["cached_at"] == NOW and rows[1]["cached_at"] == NOW - timedelta(days=3), rows)
LATER = NOW + timedelta(hours=1)
check("a Swipe card's own doc id is its vector, and a later Loved lifts anew",
      asyncio.run(rl.lift_near(UID, "movie", "Some Swiped Film", seed_doc_id="swipe-doc", now=LATER))
      == ["Gull Point", "North Signal", "Paper Moon Hotel"]
      # one lift, one moment: among its picks the match order holds
      and [r["title"] for r in plex_playlists._library_recs(UID, "movie", 10)][:2] == ["North Signal", "Gull Point"])

# ── the answers report it ───────────────────────────────────────────────────

import src.routers.messages as messages_router  # noqa: E402
import src.routers.swipe as swipe_router  # noqa: E402
import src.services.bg_tasks as bg  # noqa: E402
import src.services.taste_engine as te  # noqa: E402

asked = []


async def _fake_lift(uid, category, title, seed_doc_id=None, now=None):
    asked.append((uid, category, title, seed_doc_id))
    return ["North Signal"]


async def _no_rebuild(*a, **k):
    return None

_saved = (rl.lift_near, te.compute_all_taste_vectors, bg.track_task)
rl.lift_near, te.compute_all_taste_vectors = _fake_lift, _no_rebuild
bg.track_task = lambda coro, name=None: asyncio.ensure_future(coro)
try:
    user = SimpleNamespace(id=UID)
    loved = asyncio.run(swipe_router.answer(swipe_router.SwipeAnswer(
        category="movie", doc_id="swipe-doc", title="Some Swiped Film", seen=True, stars=10), user=user))
    liked = asyncio.run(swipe_router.answer(swipe_router.SwipeAnswer(
        category="movie", doc_id="d2", title="Liked Film", seen=True, stars=7), user=user))
    with conn.get_db_session() as db:
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="rl-1", plex_item_id="p-gull", title="Gull Point",
                                 media_type="movie", completed=True, viewed_at=NOW))
        m = ProactiveMessage(user_id=UID, trigger_type="recommendation_followup", message="How was it?",
                             trigger_data=json.dumps({"title": "Gull Point", "category": "movie"}),
                             read=False, created_at=NOW)
        m2 = ProactiveMessage(user_id=UID, trigger_type="recommendation_followup", message="And this?",
                              trigger_data=json.dumps({"title": "Gull Point", "category": "movie"}),
                              read=False, created_at=NOW)
        db.add_all([m, m2])
        db.commit()
        mid, mid2 = m.id, m2.id

    async def verdict(mid, v):
        r = await messages_router.post_verdict(mid, messages_router.VerdictBody(verdict=v), user=user)
        await asyncio.sleep(0)
        return r
    v_loved = asyncio.run(verdict(mid, "loved"))
    v_okay = asyncio.run(verdict(mid2, "okay"))
finally:
    rl.lift_near, te.compute_all_taste_vectors, bg.track_task = _saved
check("a Loved swipe lifts by its own doc id and reports what moved; a Liked lifts nothing",
      loved["lifted"] == ["North Signal"] and "lifted" not in liked
      and asked[0] == (UID, "movie", "Some Swiped Film", "swipe-doc"), (loved, liked, asked))
check("a 'Loved it' verdict lifts by the play's title; 'okay' lifts nothing",
      v_loved["lifted"] == ["North Signal"] and "lifted" not in v_okay
      and asked[1] == (UID, "movie", "Gull Point", None), (v_loved, v_okay, asked))

# ── an undone Loved takes its lift back ─────────────────────────────────────

import src.services.swipe as swsvc  # noqa: E402

with conn.get_db_session() as db:
    db.query(CachedRecommendation).filter(CachedRecommendation.title == "Far Away Store").update(
        {"lifted_at": NOW, "lifted_by": "Another Loved Film"})
    db.commit()
swsvc.save_answer(UID, "movie", "swipe-doc", "Some Swiped Film", True, 10)
undone = asyncio.run(swipe_router.undo(category="movie", user=SimpleNamespace(id=UID)))
with conn.get_db_session() as db:
    left = {r.title: r.lifted_by for r in db.query(CachedRecommendation)
            .filter(CachedRecommendation.lifted_by.isnot(None))}
check("undoing a Loved swipe clears the lifts it gave, and only those",
      undone["unlifted"] == 3 and left == {"Far Away Store": "Another Loved Film"}, (undone, left))

check.done()
