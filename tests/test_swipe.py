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

from sqlalchemy.orm import sessionmaker  # noqa: E402

import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
import src.database.connection as conn  # noqa: E402
import src.services.app_state as app_state  # noqa: E402
import src.services.swipe as sw  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
from src.database.models import Base, EncryptedTasteVector, SwipeRating, User, WatchHistoryEntry  # noqa: E402

from _checks import check  # noqa: E402


from _checks import memory_engine  # noqa: E402
engine = memory_engine()
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


from _checks import real_state  # noqa: E402
state = real_state()          # the real app_state table, not a dict
conn.get_db_session = fake_session

with fake_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True, is_active=True))
    db.flush()                  # foreign keys are enforced: the users first
    db.add(EncryptedTasteVector(user_id=1, media_category="show", salt="",
                                encrypted_blob=json.dumps({"embedding": [1.0, 0.0],
                                                           "cluster_centroids": [],
                                                           "genre_affinity": {"Drama": 1.0, "Comedy": 0.1},
                                                           "theme_affinity": {"island": 0.5}})))
    db.add(EncryptedTasteVector(user_id=1, media_category="movie", salt="",
                                encrypted_blob=json.dumps({"embedding": [1.0, 0.0], "cluster_centroids": [
                                    {"embedding": [1.0, 0.0], "top_titles": ["Heat", "Ronin"]},
                                    {"embedding": [0.0, 1.0], "top_titles": ["Amélie"]}]})))
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
    def get_by_id(self, doc_id):
        return {"id": doc_id, "embedding": [0.5, 0.5], "metadata": {"title": "Loved One"}} \
            if doc_id == "seed" else None

    def query(self, query_embeddings, n_results, where=None):
        queries_seen.append((query_embeddings, where))
        if len(query_embeddings) == 2:        # the movie profile: two clusters
            return {"ids": [["m1", "m2"], ["m2", "m3"]],
                    "distances": [[0.1, 0.4], [0.2, 0.3]],
                    "metadatas": [[{"title": "Crime One"}, {"title": "Whimsy"}],
                                  [{"title": "Whimsy"}, {"title": "Odd One"}]]}
        if query_embeddings == [[0.5, 0.5]]:  # a chain from a loved title
            return {"ids": [["seed", "sonarr:1", "c1", "c2"]],
                    "distances": [[0.0, 0.1, 0.15, 0.2]],
                    "metadatas": [[{"title": "Loved One"}, {"title": "Near One"},
                                   {"title": "Kin One", "genres": "Drama"}, {"title": "Kin Two"}]]}
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
sw._profile_bits = lambda cat, ids: {i: {"synopsis": f"About {i}", "tmdb_id": None,
                                         "mood": ["Tense"], "director": [], "runtime_min": None}
                                     for i in ids}

with fake_session() as db:
    db.add(SwipeRating(user_id=1, category="show", doc_id="sonarr:2", title="Answered Show", seen=False))

cards = asyncio.run(sw.next_cards(1, "show", 5))
check("nearest first, played and answered titles left out, one card per title",
      [c["title"] for c in cards] == ["Near One", "Far One"])
check("the card carries year, tags, fit and synopsis",
      cards[0]["year"] == 2020 and cards[0]["genres"] == ["Drama", "Mystery"]
      and cards[0]["themes"] == ["Island"] and cards[0]["fit"] == 0.9
      and cards[0]["synopsis"] == "About sonarr:1")
check("details carry only the facts the profile has", cards[0]["details"] == {"mood": ["Tense"]})
check("tags the profile favours are marked as yours, weak ones not",
      cards[0]["yours"] == ["Drama", "Island"] and cards[1]["yours"] == [])
check("one average taste vector names no cluster", cards[0]["because"] == [])
check("titles waiting in the client's stack are skipped",
      [c["title"] for c in asyncio.run(sw.next_cards(1, "show", 5, {"sonarr:1"}))] == ["Far One"])
mc = asyncio.run(sw.next_cards(1, "movie", 5))
check("each card names the taste cluster it sits nearest",
      [(c["title"], c["because"]) for c in mc]
      == [("Crime One", ["Heat", "Ronin"]), ("Whimsy", ["Amélie"]), ("Odd One", ["Amélie"])])
chain = asyncio.run(sw.chain_cards(1, "show", "seed", 3))
check("a loved title chains its nearest unanswered neighbours, itself and the stack's answered titles left out",
      [c["title"] for c in chain] == ["Near One", "Kin One", "Kin Two"]
      and all(c["chain_from"] == "Loved One" and c["fit"] is None for c in chain)
      and chain[1]["yours"] == ["Drama"])
check("a seed without a vector chains nothing", asyncio.run(sw.chain_cards(1, "show", "nope", 3)) == [])
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
check("the counter is readable per category", sw.since_rebuild(1) == {"movie": 0, "show": 3, "anime": 0})
st = sw.stats(1)["show"]
check("counts: seen, rated, not seen", st == {"seen": 2, "not_seen": 1, "rated": 1, "want": 0})

entries = sw.load_swipe_entries(1, "show")
check("seen answers become weighted taste entries keyed by the doc id",
      sorted((e["plex_item_id"], round(w, 2)) for w, e in entries)
      == [("sonarr:1", round(sw.star_weight(True, 10), 2)), ("sonarr:3", sw.SWIPE_SEEN_UNRATED_WEIGHT)])
check("...shaped like a watch entry of the category",
      all(e["media_type"] == "show" and e["series_title"] is None for _, e in entries))

sw.save_answer(1, "movie", "m9", "Wanted", False, None, want=True)
sw.save_answer(1, "movie", "m8", "Seen Anyway", True, 8, want=True)   # want never rides on seen
with fake_session() as db:
    wants = {r.doc_id: r.want for r in db.query(SwipeRating).filter_by(user_id=1, category="movie")}
check("'want to watch' is stored on a not-seen answer only", wants == {"m9": True, "m8": False})
check("only seen answers bring the next taste rebuild closer", sw.since_rebuild(1)["movie"] == 1)
check("want counts as not seen, and on its own",
      sw.stats(1)["movie"] == {"seen": 1, "not_seen": 1, "rated": 1, "want": 1})
check("want carries no taste signal",
      [e["plex_item_id"] for _, e in sw.load_swipe_entries(1, "movie")] == ["m8"])
u = sw.undo_last(1, "movie")
check("undo reports the answer it took back", u["doc_id"] == "m8" and u["want"] is False)
check("...and the want answer before it", sw.undo_last(1, "movie")["want"] is True)

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
    return await asyncio.to_thread(fn, *a, **kw)    # off the loop, like the real writer

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
      and r["since_rebuild"] == 0 and r["rebuild_every"] == sw.REBUILD_EVERY
      and state["swipe_since_rebuild:1:show"] == "0")
import src.services.plex_watchlist as pw  # noqa: E402
added = []


async def _fake_watchlist(uid, title, year=None, media_type=None):
    added.append((uid, title, year, media_type))
    return {"ok": True, "matched": f"{title} ({year})"} if title != "Nowhere" else \
        {"ok": False, "error": "no unambiguous Discover match for 'Nowhere'"}

pw.add_to_watchlist = _fake_watchlist
r = asyncio.run(rs.answer(rs.SwipeAnswer(category="anime", doc_id="a1", title="Frieren", year=2023,
                                         seen=False, want=True), user=user))
check("want to watch puts the title on the user's plex.tv watchlist",
      added == [(1, "Frieren", 2023, "anime")] and r["watchlist"]["ok"] is True)
r = asyncio.run(rs.answer(rs.SwipeAnswer(category="anime", doc_id="a2", title="Nowhere",
                                         seen=False, want=True), user=user))
check("a failed watchlist write is reported, the answer still stands",
      r["ok"] is True and r["watchlist"]["ok"] is False and "Discover" in r["watchlist"]["error"])
r = asyncio.run(rs.answer(rs.SwipeAnswer(category="anime", doc_id="a3", title="Plain",
                                         seen=False), user=user))
check("a plain 'not seen' touches no watchlist", "watchlist" not in r and len(added) == 2)
try:
    asyncio.run(rs.answer(rs.SwipeAnswer(category="show", doc_id="x", seen=True, want=True), user=user))
    check("'want to watch' with 'seen' is refused", False)
except Exception as e:
    check("'want to watch' with 'seen' is refused", getattr(e, "status_code", None) == 400)
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

# ── the card drags from anywhere on it ──────────────────────────────────────
import re  # noqa: E402
css = (ROOT / "frontend/css/app.css").read_text(encoding="utf-8")
info = re.search(r"^\.swipe-info \{[^}]*\}", css, re.M)
check("the scrollable text panel keeps touch-action none (a touch on the text used to "
      "pan it and cancel the card drag)",
      info is not None and "overflow-y: auto" in info.group(0) and "touch-action: none" in info.group(0))

check.done()
