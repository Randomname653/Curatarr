"""Swipe: rate library titles near your taste (2026-10-21).

The taste vector only knew what Plex saw. The Swipe view walks the library
titles closest to the user's taste that they have neither played in Plex nor
answered yet; "seen" answers with stars join the next taste rebuild.

Throwaway in-memory DB, a fake vector store.

    python tests/test_swipe.py
"""
import asyncio
import json
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.database.connection as conn  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
import src.services.swipe as sw  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
from src.database.models import Base, EncryptedTasteVector, SwipeRating, User, WatchHistoryEntry  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = Session()
    try:
        yield db
        db.commit()
    finally:
        db.close()


state: dict = {}
conn.get_db_session = fake_session
app_state.get_state = lambda k: state.get(k)
app_state.set_state = lambda k, v: state.__setitem__(k, v)

with fake_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True, is_active=True))
    db.add(EncryptedTasteVector(user_id=1, media_category="show", salt="",
                                encrypted_blob=json.dumps({"embedding": [1.0, 0.0],
                                                           "cluster_centroids": []})))
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="555", title="Pilot",
                             series_title="Watched Show", media_type="show",
                             viewed_at=__import__("datetime").datetime(2026, 10, 1)))

# ── weights ─────────────────────────────────────────────────────────────────
check("not seen carries no taste signal", sw.star_weight(False, None) == 0.0)
check("10 stars pull hardest, 1 star pushes as hard",
      sw.star_weight(True, 10) == sw.SWIPE_MAX_WEIGHT and sw.star_weight(True, 1) == -sw.SWIPE_MAX_WEIGHT)
check("seen without stars counts mildly in favour", sw.star_weight(True, None) == sw.SWIPE_SEEN_UNRATED_WEIGHT)
check("the middle of the scale is close to neutral", abs(sw.star_weight(True, 5)) < 0.2 and abs(sw.star_weight(True, 6)) < 0.2)

# ── the stack ───────────────────────────────────────────────────────────────
queries_seen = []


class FakeChroma:
    def query(self, query_embeddings, n_results, where=None):
        queries_seen.append((query_embeddings, where))
        return {"ids": [["sonarr:1", "555", "sonarr:2", "sonarr:3", "9001"]],
                "distances": [[0.10, 0.05, 0.20, 0.30, 0.25]],
                "metadatas": [[{"title": "Near One", "year": 2020, "genres": "Drama, Mystery", "themes": "Island"},
                               {"title": "Watched Show"},
                               {"title": "Answered Show"},
                               {"title": "Far One", "genres": "Comedy"},
                               {"title": "Near One"}]]}


async def _run_chroma(fn, *a, **kw):
    return fn(*a, **kw)

cw.get_chroma_db = lambda: FakeChroma()
cw.run_chroma = _run_chroma
sw._profile_bits = lambda cat, ids: {i: (f"About {i}", None) for i in ids}

with fake_session() as db:
    db.add(SwipeRating(user_id=1, category="show", doc_id="sonarr:2", title="Answered Show", seen=False))

cards = asyncio.run(sw.next_cards(1, "show", 5))
check("nearest first, played and answered titles left out, one card per title",
      [c["title"] for c in cards] == ["Near One", "Far One"])
check("the card carries year, tags, fit and synopsis",
      cards[0]["year"] == 2020 and cards[0]["genres"] == ["Drama", "Mystery"]
      and cards[0]["themes"] == ["Island"] and cards[0]["fit"] == 0.9
      and cards[0]["synopsis"] == "About sonarr:1")
check("the vector store is asked with the user's taste, filtered to the category",
      queries_seen and queries_seen[0][0] == [[1.0, 0.0]] and queries_seen[0][1] == cw.domain_where("show"))
check("no taste profile, no cards", asyncio.run(sw.next_cards(1, "anime", 5)) == [])

# ── answers, undo, counts ───────────────────────────────────────────────────
n1 = sw.save_answer(1, "show", "sonarr:1", "Near One", True, 9)
n2 = sw.save_answer(1, "show", "sonarr:3", "Far One", True, None)
n3 = sw.save_answer(1, "show", "sonarr:1", "Near One", True, 10)      # a second answer replaces
with fake_session() as db:
    rows = {r.doc_id: (r.seen, r.stars) for r in db.query(SwipeRating).filter_by(user_id=1).all()}
check("answers are stored per title, a new answer replaces the old",
      rows == {"sonarr:2": (False, None), "sonarr:1": (True, 10), "sonarr:3": (True, None)})
check("the counter towards the next taste rebuild grows per answer", (n1, n2, n3) == (1, 2, 3))
st = sw.stats(1)["show"]
check("counts: seen, rated, not seen", st == {"seen": 2, "not_seen": 1, "rated": 1})

entries = sw.load_swipe_entries(1, "show")
check("seen answers become weighted taste entries keyed by the doc id",
      sorted((e["plex_item_id"], round(w, 2)) for w, e in entries)
      == [("sonarr:1", round(sw.star_weight(True, 10), 2)), ("sonarr:3", sw.SWIPE_SEEN_UNRATED_WEIGHT)])
check("...shaped like a watch entry of the category",
      all(e["media_type"] == "show" and e["series_title"] is None for _, e in entries))

last = sw.undo_last(1, "show")
check("undo takes back the latest answer", last and last["doc_id"] == "sonarr:1"
      and "sonarr:1" not in {e["plex_item_id"] for _, e in sw.load_swipe_entries(1, "show")})

# ── the router rebuilds the taste profile every REBUILD_EVERY answers ───────
import src.routers.swipe as rs  # noqa: E402
import src.services.bg_tasks as bg  # noqa: E402
import src.services.taste_engine as te  # noqa: E402

rebuilds = []


async def _fake_rebuild(uid, cats, skip_summaries=False):
    rebuilds.append((uid, cats, skip_summaries))


async def _run_write(fn, *a, **kw):
    return fn(*a, **kw)

rs.run_write = _run_write
te.compute_all_taste_vectors = _fake_rebuild
bg.track_task = lambda coro, name=None: asyncio.ensure_future(coro)
user = SimpleNamespace(id=1)
state["swipe_since_rebuild:1:show"] = str(sw.REBUILD_EVERY - 1)


async def _answer():
    r = await rs.answer(rs.SwipeAnswer(category="show", doc_id="sonarr:7", title="Seven", seen=True, stars=8),
                        user=user)
    await asyncio.sleep(0)
    return r

r = asyncio.run(_answer())
check("the tenth answer starts a background taste rebuild without LLM summaries",
      r["taste_rebuild"] is True and rebuilds == [(1, ["show"], True)]
      and state["swipe_since_rebuild:1:show"] == "0")
try:
    asyncio.run(rs.answer(rs.SwipeAnswer(category="show", doc_id="x", seen=False, stars=5), user=user))
    check("stars without 'seen' are refused", False)
except Exception as e:
    check("stars without 'seen' are refused", getattr(e, "status_code", None) == 400)
try:
    rs._category("music")
    check("music is not a swipe category", False)
except Exception as e:
    check("music is not a swipe category", getattr(e, "status_code", None) == 400)

# ── the taste builder reads the answers ─────────────────────────────────────
tsrc = (ROOT / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("the taste rebuild adds swipe answers before embedding",
      tsrc.index("load_swipe_entries, user_id, category") < tsrc.index("# Count how many need fresh embeddings"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
