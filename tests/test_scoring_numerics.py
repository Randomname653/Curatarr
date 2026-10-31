"""Deletion / discovery scoring: non-finite values are never "maximally
foreign", absence of evidence never shortlists a title on its own, size is
capped, the judge's whole reach gets its keep-considerations, and discovery
never re-pitches what the owner deleted or disliked.

Found in the 2026-10 vector/memory audit:
  * Python's min(1.0, nan) is 1.0 — a NaN cosine or anchor scored the full
    80 taste points; one NaN corpus row turned the stored corpus mean NaN
    and with it every candidate's cosine.
  * a neutral 0.5 mismatch was 40 points, above SHORTLIST_SCORE (30): a new
    user's whole library, and every title without an embedding, was
    shortlisted on no evidence; score_arr_items crashed (UnboundLocalError)
    for a category with genre stats but no taste blob.
  * the blanket size fallback was uncapped (2 TB → 38 points).
  * keep-considerations covered ranks 1-30, the judge walks to 60.
  * discovery only filtered owned/seen — deleted-unwatched titles came back.

    python tests/test_scoring_numerics.py
"""
import asyncio
import json
import math
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.database.connection as conn  # noqa: E402
import src.services.episodic_memory as epm  # noqa: E402
import src.services.library_memory as lm  # noqa: E402
import src.services.llm_priority as llp  # noqa: E402
import src.services.recommendations_engine as eng  # noqa: E402
import src.services.size_norms as sn  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
from src.config import settings  # noqa: E402
from src.database.models import (Base, DeletionProposal,  # noqa: E402
                                 EncryptedTasteVector, TasteVectorEntry)
from src.services.embed_service import _l2  # noqa: E402
from src.services.taste_engine import (_clean_unit_rows,  # noqa: E402
                                       _corpus_calibration,
                                       weighted_mean_embedding)

from _checks import check  # noqa: E402


NAN, INF = float("nan"), float("inf")
BAR = eng.SHORTLIST_SCORE
FLOOR = eng.NO_TASTE_MISMATCH

# ── mismatch mapping: non-finite in, never maximal out ───────────────────────

check("the trap: Python's min(1.0, nan) is 1.0", min(1.0, NAN) == 1.0)
check("NaN cosine -> no-evidence floor, under the shortlist bar",
      eng._cosine_to_mismatch(NAN, 0.1, 0.5) == FLOOR and FLOOR * 80 < BAR)
check("Inf cosine -> floor", eng._cosine_to_mismatch(INF, 0.1, 0.5) == FLOOR)
check("NaN anchors -> floor", eng._cosine_to_mismatch(0.3, NAN, NAN) == FLOOR)
check("inverted anchors -> floor", eng._cosine_to_mismatch(0.3, 0.5, 0.1) == FLOOR)
check("finite inputs map as before",
      eng._cosine_to_mismatch(0.5, 0.1, 0.5) == 0.0
      and eng._cosine_to_mismatch(0.1, 0.1, 0.5) == 1.0)

lo, hi = eng._cosine_anchors(list(np.linspace(0.5, 0.8, 30)) + [NAN])
check("one NaN in the batch leaves the anchors finite",
      math.isfinite(lo) and math.isfinite(hi) and hi > lo)
check("_normalize_vec rejects NaN, Inf and non-vectors",
      eng._normalize_vec([1.0, NAN]) is None and eng._normalize_vec([INF, 1.0]) is None
      and eng._normalize_vec([[1.0], [2.0]]) is None and eng._normalize_vec(["x"]) is None)
check("_l2 (embed ingress) rejects an Inf vector", _l2([INF, 1.0]) is None)
check("_l2 still normalises a normal vector",
      math.isclose(sum(x * x for x in _l2([3.0, 4.0])), 1.0))

# ── corpus calibration survives poisoned rows ────────────────────────────────

rng = np.random.default_rng(3)
corpus = [list(v) for v in rng.normal(0.5, 0.1, size=(120, 8))]
poisoned = corpus + [[NAN] * 8, [INF] + [0.0] * 7, [0.0] * 8]
rows = _clean_unit_rows(poisoned)
check("clean rows drop NaN, Inf and zero rows",
      rows is not None and len(rows) == 120 and np.isfinite(rows).all())
check("fewer than 50 clean rows -> None",
      _clean_unit_rows(corpus[:10] + [[NAN] * 8] * 60) is None)

cw.get_chroma_db = lambda: SimpleNamespace(
    embeddings_for_domain=lambda domain, limit=30000: poisoned)
calib = _corpus_calibration(corpus[0], corpus[1], "movie")
check("one NaN corpus row no longer poisons the corpus mean",
      calib is not None and all(math.isfinite(x) for x in calib["corpus_mean"]))
check("…nor the taste or drop anchors",
      all(math.isfinite(x) for x in calib["cal"] + calib["drop_cal"]))
check("a centroid of another dimension -> no calibration",
      _corpus_calibration([1.0] * 5, None, "movie") is None)
check("a NaN centroid -> no calibration",
      _corpus_calibration([NAN] * 8, None, "movie") is None)
emb, _ = weighted_mean_embedding([([1.0, 0.0], 1.0), ([0.0, 1.0, 0.0], 1.0)])
check("weighted mean skips a vector from another dimension", emb == [1.0, 0.0])

# ── end to end: generate_deletion_proposals on an in-memory DB ───────────────

from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1)
Session = sessionmaker(bind=engine)


@contextmanager
def _session():
    db = Session()
    try:
        yield db
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


conn.get_db_session = _session
eng.get_db_session = _session

DIM = 8


def unit(i):
    v = [0.0] * DIM
    v[i] = 1.0
    return v


TASTE, FAR = unit(0), unit(1)


class _FakeChroma:
    def __init__(self):
        self.vectors = {}

    def get_by_ids(self, ids, chunk_size=1000):
        return {i: {"id": i, "embedding": self.vectors[i]} for i in ids if i in self.vectors}


chroma = _FakeChroma()
cw.get_chroma_db = lambda: chroma


async def _run_chroma(fn, *a, **k):
    return fn(*a, **k)


async def _pitcher(url=None):
    return "test-model"


cw.run_chroma = _run_chroma
eng._bulk_cached_ratings = lambda items, cat: {}
sn.load_tech_index = lambda: {}
llp.resolve_pitcher_model = _pitcher
eng._judge_lane = lambda: (False, "test stops before the judge")
settings.PILLARS_ENABLED = True

# The considerations hook runs once over the candidates the judge could
# reach — the last step before judging. Reading the caller's `_consider_slice`
# there shows exactly what was shortlisted, with its score terms.
considered: list = []


async def _considerations(user_id, profiles, media_category=None, top_k=3):
    considered.extend(sys._getframe(1).f_locals["_consider_slice"])
    return [[] for _ in profiles]


epm.retrieve_considerations_many = _considerations


def seed_taste(blob=None, genres=None):
    with _session() as db:
        db.query(TasteVectorEntry).delete()
        db.query(EncryptedTasteVector).delete()
        if blob is not None or genres is not None:
            db.add(TasteVectorEntry(
                user_id=1, summary_text="[MOVIE] test taste",
                genre_affinity=json.dumps({"movie": {"genre_affinity": genres or {}}})))
        if blob is not None:
            db.add(EncryptedTasteVector(user_id=1, media_category="movie",
                                        salt="unencrypted",
                                        encrypted_blob=json.dumps(dict(blob, version=0))))


def item(title, gb, vec=None, genres="Drama"):
    key = f"radarr:{title}"
    chroma.vectors.pop(key, None)
    if vec is not None:
        chroma.vectors[key] = vec
    return {"title": title, "plex_rating_key": key, "size_mb": gb * 1024,
            "genres": genres, "service": "radarr", "arr_id": 1}


def run(items):
    considered.clear()
    asyncio.run(eng.generate_deletion_proposals(1, items, category="movie"))
    return {c["item"]["title"]: c for c in considered}


# cold start — no taste data at all
seed_taste()
eng.DELETION_RUN_SUMMARY.clear()
got = run([item("Anything", 40), item("Huge", 2000)])
check("cold start: nothing is shortlisted on size and rating alone", got == {})
check("cold start: the run banner says why",
      "no taste profile" in (eng.DELETION_RUN_SUMMARY.get("movie") or {}).get("stopped", ""))

# taste vector, global anchors, no genre stats
seed_taste(blob={"embedding": TASTE, "cal_p10": 0.0, "cal_p90": 0.9})
got = run([item("Fit", 40, TASTE), item("Far", 4, FAR),
           item("Unembedded", 1), item("Corrupt", 1, [NAN] * DIM),
           item("Huge", 2000)])
check("a far title is shortlisted on taste", "Far" in got)
check("a fitting title is not", "Fit" not in got)
check("no embedding: taste alone no longer shortlists it (was 40 pts)",
      "Unembedded" not in got)
check("a NaN vector is no evidence, not maximal mismatch (was 80 pts)",
      "Corrupt" not in got)
check("other evidence still carries a no-taste title over the bar (2 TB)",
      "Huge" in got and got["Huge"]["score_terms"]["taste_src"] == "none")
check("blanket size fallback capped at 25 (2 TB was 38)",
      got["Huge"]["score_terms"]["size_pts"] == eng.SIZE_PTS_CAP == 25.0)

# corrupt stored corpus mean: rejected, anchors go with it, scoring stays sane
seed_taste(blob={"embedding": TASTE, "cal_p10": 0.0, "cal_p90": 0.9,
                 "corpus_mean": [NAN] * DIM})
got = run([item("Fit", 40, TASTE), item("Far", 4, FAR)])
check("NaN corpus mean no longer shortlists a fitting title", "Fit" not in got)
check("…and a far title still scores as far", "Far" in got)

# keep-considerations reach the judge's whole range: the cap, plus the
# titles reused verdicts let the run walk past (2026-10-22, verdict cache)
_reach = eng.JUDGE_CAP * (1 + eng.REUSE_FACTOR)
got = run([item(f"Far {i}", 4, FAR) for i in range(_reach + 20)])
check(f"considerations scored for the run's reach, {_reach} candidates (was 30, then 60)",
      len(considered) == _reach == 180)

# genre fallback band [0.3, 0.6]
seed_taste(genres={"Drama": 1.0})
got = run([item("Horror Night", 4, genres="Horror"), item("Drama Day", 400, genres="Drama")])
check("genre fallback: zero overlap scores 0.6 (was 0.85 → 68 pts)",
      "Horror Night" in got and got["Horror Night"]["score_terms"]["mismatch"] == 0.6)

# score_arr_items: genre stats, no taste blob (state before enrichment)
try:
    ranked = asyncio.run(eng.score_arr_items(
        1, "movie", [item("Horror Night", 4, genres="Horror"),
                     item("Drama Day", 4, genres="Drama")]))
    ok = [r["title"] for r in ranked] == ["Drama Day", "Horror Night"]
except Exception as e:
    print("   ", type(e).__name__, e)
    ok = False
check("score_arr_items ranks by genre without a blob (was UnboundLocalError)", ok)

# ── discovery never re-pitches deleted or disliked titles ────────────────────

with _session() as db:
    db.add(DeletionProposal(user_id=1, media_id="1", title="Gone Girl",
                            service="radarr", status="deleted", category="movie"))
    db.add(DeletionProposal(user_id=1, media_id="2", title="Limbo Film",
                            service="radarr", status="limbo", category="movie"))
    db.add(DeletionProposal(user_id=1, media_id="3", title="Kept Thing",
                            service="radarr", status="rejected", category="movie"))
    db.add(DeletionProposal(user_id=1, media_id="4", title="Shared Name",
                            service="sonarr", status="deleted", category="show"))
seed_taste(blob={"embedding": TASTE, "disliked_titles": ["Bad Movie"]},
           genres={"Drama": 1.0})

PITCHED = ["Gone Girl (2014)", "Limbo Film", "Bad Movie", "Kept Thing",
           "Shared Name", "Fresh Pick"]


async def _llm(prompt, max_tokens=800, **kw):
    return json.dumps([{"title": t, "reason": "r", "confidence": 0.5} for t in PITCHED])


async def _owned(category=None):
    return {"titles": set(), "tmdb_ids": set(), "tvdb_ids": set()}


async def _trending(cat):
    return []


eng._call_llm = _llm
lm.owned_index = _owned
eng._get_trending = _trending
titles = [r["title"] for r in asyncio.run(eng.generate_recommendations(1, category="movie"))]
check("a title deleted through Curatarr is not re-pitched", "Gone Girl (2014)" not in titles)
check("a delete with unknown outcome (limbo) is not re-pitched", "Limbo Film" not in titles)
check("a disliked title is not pitched (blob read inside its session)",
      "Bad Movie" not in titles)
check("a KEPT proposal (rejected) is no exclusion", "Kept Thing" in titles)
check("a deleted SHOW does not block a same-named movie", "Shared Name" in titles)
check("a genuinely new title survives", "Fresh Pick" in titles)
check("partition labels the bucket",
      lm.partition([{"title": "Gone Girl"}], {}, {}, lm.removed_index(1, "movie"))["removed"])

check.done()
