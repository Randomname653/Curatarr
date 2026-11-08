"""Post-watch verdicts (stage 2 of docs/UX_RETENTION_DESIGN.md, the owner's
decision): the follow-up card's one-tap "how did it land?" is stored in
``post_watch_verdicts`` and weighs the play row that is already in the
history - never a row of its own, so nothing counts twice. A film matches
by its play's key, a series by its title; "Not for me" pushes.

Every title here is invented.

    python tests/test_post_watch_verdicts.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

conn = app_db()

from fastapi import HTTPException  # noqa: E402

from src.database.models import (  # noqa: E402
    PostWatchVerdict, ProactiveMessage, User, WatchHistoryEntry)
from src.routers import messages as messages_router  # noqa: E402
from src.services import post_watch as pw  # noqa: E402
from src.services.taste_engine import _series_entries, recency_weight  # noqa: E402

NOW = datetime.utcnow()
HOUR = 3_600_000

with conn.get_db_session() as db:
    me, other = User(plex_user_id="pw-1", plex_username="Member"), User(plex_user_id="pw-2", plex_username="Other")
    db.add_all([me, other])
    db.commit()
    UID, OTHER = me.id, other.id
    for i, days in enumerate((9, 2)):           # watched twice, newest play key 'film-1'
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="pw-1", plex_item_id=f"film-{i}",
                                 title="The Lantern Room", media_type="movie",
                                 duration_ms=2 * HOUR, view_offset_ms=2 * HOUR, completed=True,
                                 viewed_at=NOW - timedelta(days=days)))
    for n in (1, 2, 3):
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="pw-1", plex_item_id=f"sa-{n}",
                                 title=f"Episode {n}", series_title="Starfall Archive",
                                 media_type="anime", season=1, episode=n, duration_ms=HOUR,
                                 view_offset_ms=HOUR, completed=True,
                                 viewed_at=NOW - timedelta(hours=10 - n)))
    db.commit()


def message(trigger_type, data, user=None):
    with conn.get_db_session() as db:
        m = ProactiveMessage(user_id=user or UID, trigger_type=trigger_type,
                             trigger_data=json.dumps(data), message="How did it land?",
                             read=False, created_at=NOW, expires_at=NOW + timedelta(days=7))
        db.add(m)
        db.commit()
        return m.id


def rows(user=UID):
    with conn.get_db_session() as db:
        return [(r.category, r.item_key, r.title, r.verdict, r.source)
                for r in db.query(PostWatchVerdict).filter(PostWatchVerdict.user_id == user)
                .order_by(PostWatchVerdict.id)]


def is_read(mid):
    with conn.get_db_session() as db:
        return db.get(ProactiveMessage, mid).read


# ── what a message is about ─────────────────────────────────────────────────

check("a watched recommendation names its title and category",
      pw.subject_of("recommendation_followup", json.dumps({"title": "X", "category": "movie"}))
      == {"title": "X", "category": "movie"})
check("a morning line about exactly one title names it; about more, or a music minute, nothing",
      pw.subject_of("last_night", json.dumps({"parts": 1, "titles": 1, "subject": "S",
                                               "subject_type": "anime"}))
      == {"title": "S", "category": "anime"}
      and pw.subject_of("last_night", json.dumps({"parts": 2, "titles": 2})) is None
      and pw.subject_of("last_night", json.dumps({"parts": 1, "titles": 0})) is None)
check("music, other triggers and unreadable data take no verdict",
      pw.subject_of("recommendation_followup", json.dumps({"title": "X", "category": "music"})) is None
      and pw.subject_of("rewatch", json.dumps({"title": "X", "category": "movie"})) is None
      and pw.subject_of("recommendation_followup", "{not json") is None)

# ── recording: one row per work, on the play that is already there ─────────

film = message("recommendation_followup", {"title": "the lantern room", "category": "movie"})
out = pw.record_from_message(UID, film, "liked")
check("a film's verdict lands on its newest play, under the play's own title",
      rows() == [("movie", "film-1", "The Lantern Room", "liked", "followup_card")]
      and out == {"title": "The Lantern Room", "category": "movie", "verdict": "liked"}, rows())
check("the answered message is retired", is_read(film))
again = message("recommendation_followup", {"title": "The Lantern Room", "category": "movie"})
pw.record_from_message(UID, again, "loved")
check("a second answer on the same film replaces the first", rows() == [
    ("movie", "film-1", "The Lantern Room", "loved", "followup_card")], rows())

series = message("last_night", {"parts": 1, "titles": 1, "subject": "Starfall Archive",
                                "subject_type": "anime"})
pw.record_from_message(UID, series, "not_for_me")
check("a series' verdict is keyed by its title, the newest episode as its play key",
      rows()[-1] == ("anime", "sa-3", "Starfall Archive", "not_for_me", "followup_card"), rows())
later = message("recommendation_followup", {"title": "Starfall Archive", "category": "anime"})
pw.record_from_message(UID, later, "okay")
check("one row per series: a later answer replaces it",
      [r for r in rows() if r[0] == "anime"] == [("anime", "sa-3", "Starfall Archive", "okay",
                                                  "followup_card")], rows())


def raises(fn, exc):
    try:
        fn()
    except exc:
        return True
    except Exception:  # noqa: BLE001 - any other exception is a wrong answer here
        return False
    return False


nothing = message("recommendation_followup", {"title": "Gull Point", "category": "movie"})
theirs = message("recommendation_followup", {"title": "The Lantern Room", "category": "movie"}, OTHER)
check("an unknown verdict is refused", raises(lambda: pw.record_from_message(UID, film, "meh"), ValueError))
check("a title the member never played has no row to weigh",
      raises(lambda: pw.record_from_message(UID, nothing, "loved"), pw.NoPlay))
check("another member's message is not found", raises(
    lambda: pw.record_from_message(UID, theirs, "loved"), LookupError) and rows(OTHER) == [])

# ── the endpoint maps those to HTTP answers ─────────────────────────────────


import src.services.taste_engine as _te  # noqa: E402

_rebuilds = []


async def _rebuild(uid, categories=None, skip_summaries=False):
    _rebuilds.append((uid, categories, skip_summaries))


def status(mid, verdict):
    async def call():
        await messages_router.post_verdict(mid, messages_router.VerdictBody(verdict=verdict),
                                           user=SimpleNamespace(id=UID))
        await asyncio.sleep(0)              # let the tracked rebuild run
    try:
        asyncio.run(call())
    except HTTPException as e:
        return e.status_code
    return 200


_real_rebuild, _te.compute_all_taste_vectors = _te.compute_all_taste_vectors, _rebuild
try:
    ok = status(message("recommendation_followup", {"title": "The Lantern Room", "category": "movie"}),
                "liked")
    refused = [status(film, "meh"), status(nothing, "loved"), status(theirs, "loved")]
finally:
    _te.compute_all_taste_vectors = _real_rebuild
check("POST /api/messages/{id}/verdict: 200, 422 for a bad verdict, 404 for no play / not theirs",
      ok == 200 and refused == [422, 404, 404], (ok, refused))
check("a stored verdict rebuilds that category's taste without the curator's summaries; "
      "a refused one rebuilds nothing", _rebuilds == [(UID, ["movie"], True)], _rebuilds)

# ── the rebuild's view ──────────────────────────────────────────────────────

by_key, by_series = pw.factors_for(UID)
check("factors: the film by its play key, the series by (category, title)",
      by_key == {"film-1": pw.FACTORS["liked"]}
      and by_series == {("anime", "starfall archive"): pw.FACTORS["okay"]}, (by_key, by_series))
check("factor_of: a film row by its key (only the judged play), an episode by its series",
      pw.factor_of({"media_type": "movie", "plex_item_id": "film-1"}, by_key, by_series) == pw.FACTORS["liked"]
      and pw.factor_of({"media_type": "movie", "plex_item_id": "film-0"}, by_key, by_series) is None
      and pw.factor_of({"media_type": "anime", "series_title": "Starfall Archive"}, by_key, by_series)
      == pw.FACTORS["okay"]
      and pw.factor_of({"media_type": "music", "plex_item_id": "film-1"}, by_key, by_series) is None)
check("the scale: loved and liked pull harder than a plain play, okay a little less, "
      "not for me pushes",
      pw.FACTORS["loved"] > pw.FACTORS["liked"] > 1.0 > pw.FACTORS["okay"] > 0 > pw.FACTORS["not_for_me"])
check("only that category when asked", pw.factors_for(UID, "anime")[0] == {})

# ── the series weight takes the verdict ─────────────────────────────────────


def ep(series, n, days_ago, done=True, pct=1.0):
    return {"plex_item_id": f"{series}-{n}", "title": f"Episode {n}", "series_title": series,
            "media_type": "show", "duration_ms": HOUR, "view_offset_ms": int(HOUR * pct),
            "completed": done, "viewed_at": NOW - timedelta(days=days_ago)}


def weights(entries, verdicts=None):
    return {rep["series_title"]: w for w, rep in
            _series_entries(sorted(entries, key=lambda e: e["viewed_at"], reverse=True), NOW, {},
                            lambda e: True, verdicts)}


plays = [ep("Gull Point", n, 1) for n in range(1, 4)] + [ep("Harbour Lights", n, 1) for n in range(1, 4)]
plain = weights(plays)
judged = weights(plays, {"gull point": pw.FACTORS["loved"], "harbour lights": pw.FACTORS["not_for_me"]})
check("loved scales the series' weight up, not for me turns it into a push",
      abs(judged["Gull Point"] - plain["Gull Point"] * pw.FACTORS["loved"]) < 1e-9
      and judged["Harbour Lights"] < 0, judged)
started = [ep("Gull Point", 1, 1, done=False, pct=0.6)]
check("a series in progress, nothing finished yet, counts once it has a verdict",
      weights(started) == {} and abs(weights(started, {"gull point": pw.FACTORS["liked"]})["Gull Point"]
                                     - recency_weight(NOW - timedelta(days=1), NOW) * pw.FACTORS["liked"]) < 1e-9)
check("no verdicts: the weights are the old ones", weights(plays, {}) == plain)

# ── end to end: the film rebuild weighs the judged play ─────────────────────

import numpy as np  # noqa: E402

import src.cache.metadata_cache as mc_mod  # noqa: E402
import src.services.embed_service as es  # noqa: E402
import src.services.taste_engine as te  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
from src.database.models import EnrichmentStatus  # noqa: E402

LANTERN, GULL = [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]
with conn.get_db_session() as db:
    viewer = User(plex_user_id="pw-3", plex_username="Viewer")
    db.add(viewer)
    db.commit()
    VID = viewer.id
    for key, title in (("lr-9", "The Lantern Room"), ("gp-9", "Gull Point")):
        db.add(WatchHistoryEntry(user_id=VID, plex_user_id="pw-3", plex_item_id=key, title=title,
                                 media_type="movie", duration_ms=2 * HOUR, view_offset_ms=2 * HOUR,
                                 completed=True, viewed_at=NOW - timedelta(days=1)))
        db.add(EnrichmentStatus(plex_rating_key=key, title=title, media_category="movie",
                                enriched=True, enriched_at=NOW))
    db.commit()


class _FakeCache:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): pass
    def set_cache(self, *a, **k): pass
    def get_cache(self, key):
        if key.startswith("enriched:movie:lr-9"):
            return {"response": {"title": "The Lantern Room", "embedding_text": "LANTERN"}}
        if key.startswith("enriched:movie:gp-9"):
            return {"response": {"title": "Gull Point", "embedding_text": "GULL"}}
        return None
    def get_cache_many(self, keys):
        return {k: self.get_cache(k) for k in keys if self.get_cache(k)}


async def _fake_embed(texts, profile=None):
    return [list(LANTERN) if "LANTERN" in t else list(GULL) for t in texts]

te.MetadataCache = _FakeCache
mc_mod.MetadataCache = _FakeCache
es.embed_documents = _fake_embed
cw.get_chroma_db = lambda: type("C", (), {"get_by_id": lambda s, i: None,
                                         "embeddings_for_domain": lambda s, d, limit=30000: []})()


def lean():
    """How far the film vector leans to The Lantern Room over Gull Point."""
    emb = np.asarray(asyncio.run(te.compute_taste_vector_for_user(VID, category="movie"))["embedding"])
    return float(emb @ np.asarray(LANTERN)) - float(emb @ np.asarray(GULL))


before = lean()
judge = message("recommendation_followup", {"title": "The Lantern Room", "category": "movie"}, VID)
pw.record_from_message(VID, judge, "not_for_me")
after = lean()
check(f"end to end: two films played alike lean nowhere ({before:.2f}); after \"Not for me\" on one, "
      f"the vector turns away from it ({after:.2f})", abs(before) < 1e-6 and after < -0.5)
pw.record_from_message(VID, message("recommendation_followup",
                                    {"title": "The Lantern Room", "category": "movie"}, VID), "loved")
check("end to end: \"Loved it\" makes it lead", lean() > 0.1)

check.done()
