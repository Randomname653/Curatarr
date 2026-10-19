"""Unknown is not "none" on the deletion and recommendation paths.

  * an arr /history that can't be read was {} — "nothing imported": with
    Just-arrived on the owner read "No items imported in the last N days",
    and every saved proposal lost its known activity stamp (NULL = old).
    Now None: the response says the history couldn't be read, and a save
    keeps the last known stamp
  * a DB error on the enrichment-coverage query faked "never run" (a red
    "Library enrichment has never run" banner); now an explicit unknown
  * a corrupt taste blob / genre-affinity row in a deletion run was {}
    silently — scored without taste, or reported as a cold start ("no taste
    profile yet"); now logged and named on the run banner
  * the recommendation ranking lost the taste vector to one odd
    disliked_titles entry
  * a cosine that raised was dropped silently per item; now one WARNING per
    run with the count

Each check fails on the code before this change.

    python tests/test_fail_closed_s2_g1.py
"""
import asyncio
import json
import logging
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import httpx  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.config import settings  # noqa: E402
from src.database.models import (Base, DeletionProposal,  # noqa: E402
                                 EncryptedTasteVector, TasteVectorEntry, User)

Base.metadata.create_all(conn_mod.engine)

import src.routers.recommendations as recs  # noqa: E402
import src.services.recommendations_engine as eng  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def warnings(self, needle):
        return [r for r in self.records
                if r.levelno >= logging.WARNING and needle in r.getMessage()]


def capture(logger):
    h = _Capture()
    logger.addHandler(h)
    return h


_RealClient = httpx.AsyncClient


def mock_http(handler):
    class _Client(_RealClient):
        def __init__(self, *a, **k):
            k.pop("verify", None)
            super().__init__(*a, transport=httpx.MockTransport(handler), **k)
    httpx.AsyncClient = _Client


with conn_mod.get_db_session() as db:
    db.add(User(id=1, plex_user_id="p1", plex_username="owner", is_admin=True))
    db.commit()
_U = SimpleNamespace(id=1)

# ── 1. arr history: unreadable is None, not {} ──────────────────────────────
print("arr history")
settings.SONARR_URL, settings.SONARR_API_KEY = "http://sonarr.test", "k"
settings.RADARR_URL, settings.RADARR_API_KEY = "http://radarr.test", "k"
log = capture(recs.logger)
try:
    mock_http(lambda req: httpx.Response(500))
    got_500 = asyncio.run(recs._fetch_arr_recent_imports("sonarr"))

    def _down(req):
        raise httpx.ConnectError("refused", request=req)
    mock_http(_down)
    got_down = asyncio.run(recs._fetch_arr_recent_imports("sonarr"))
    mock_http(lambda req: httpx.Response(200, json={"records": []}))
    got_empty = asyncio.run(recs._fetch_arr_recent_imports("sonarr"))
finally:
    httpx.AsyncClient = _RealClient
check("HTTP 500 history is unknown (None), not 'no imports'", got_500 is None)
check("an unreachable arr is unknown too", got_down is None)
check("...and both are logged at WARNING", len(log.warnings("history")) >= 2)
check("a readable, empty history is still {}", got_empty == {})

# ── 2. Just-arrived with an unreadable history ──────────────────────────────
print("just-arrived")
import src.services.recommendations_engine as engine_mod  # noqa: E402

_real_gen = engine_mod.generate_deletion_proposals


async def _candidates(category=None):
    return [{"title": "Old Show", "service": "sonarr", "arr_id": 7, "category": "show",
             "size_mb": 2048}]


async def _unreadable(svc):
    return None


async def _gen(user_id, items, category, monitor_task=None):
    return [{"title": i["title"], "pitch": "p", "confidence": 0.7, "arr_id": i["arr_id"],
             "service": i["service"], "category": category, "size_mb": i["size_mb"],
             "latest_activity_at": i.get("latest_activity_at")} for i in items]


async def _enrich(p, item_map, category):
    return dict(p)


recs._fetch_arr_candidates = _candidates
recs._fetch_arr_recent_imports = _unreadable
recs._enrich_proposal = _enrich
engine_mod.generate_deletion_proposals = _gen

out = asyncio.run(recs.get_deletion_proposals(
    category="show", refresh=True, recent_only=True, recent_days=7, user=_U))
msg = (out or {}).get("message", "")
check(f"an unreadable history says so, not 'No items imported' ({msg[:60]!r})",
      "Couldn't read" in msg and "Sonarr" in msg and "No items imported" not in msg)
check("...and carries the unavailable service",
      out.get("activity_unavailable") == ["sonarr"])

# A known stamp survives a run that couldn't read the history.
known = datetime.utcnow() - timedelta(days=2)
with conn_mod.get_db_session() as db:
    db.add(DeletionProposal(user_id=1, media_id="7", title="Old Show", service="sonarr",
                            reason="old", confidence=0.5, storage_mb=2048,
                            status="pending", category="show",
                            latest_activity_at=known))
    db.commit()
out = asyncio.run(recs.get_deletion_proposals(
    category="show", refresh=True, recent_only=False, recent_days=7, user=_U))
with conn_mod.get_db_session() as db:
    row = (db.query(DeletionProposal)
           .filter_by(status="pending", media_id="7").one())
    stamp = row.latest_activity_at
check("an unreadable history keeps the last known activity stamp (not NULL)",
      stamp is not None and abs((stamp - known).total_seconds()) < 1)
out = asyncio.run(recs.get_deletion_proposals(
    category="show", refresh=False, recent_only=True, recent_days=7, user=_U))
check("the cached Just-arrived read keeps the row and says the history was unreadable",
      [p["title"] for p in out["proposals"]] == ["Old Show"]
      and out.get("activity_unavailable") == ["sonarr"]
      and "Couldn't read" in (out.get("activity_note") or ""))


async def _readable(svc):
    return {}
recs._fetch_arr_recent_imports = _readable
out = asyncio.run(recs.get_deletion_proposals(
    category="show", refresh=True, recent_only=True, recent_days=7, user=_U))
check("once readable, an empty history is the real 'No items imported' answer",
      "No items imported" in out.get("message", "") and "activity_unavailable" not in out)
engine_mod.generate_deletion_proposals = _real_gen

# ── 3. enrichment coverage: unreadable is unknown, not "never run" ─────────
print("enrichment coverage")
_real_session = conn_mod.get_db_session


def _locked():
    raise RuntimeError("database is locked")


conn_mod.get_db_session = _locked
log = capture(recs.logger)
try:
    cov = recs._arr_enrichment_coverage("movie")
finally:
    conn_mod.get_db_session = _real_session
check(f"a failed coverage query is unknown, not never_run ({cov})",
      cov.get("unknown") is True and not cov.get("never_run") and cov.get("error"))
check("...logged at WARNING", log.warnings("coverage"))
check("a readable empty table still reports never_run",
      recs._arr_enrichment_coverage("movie").get("never_run") is True)

# ── 4. frontend: the banners say "unknown" / "couldn't read" ─────────────────
print("frontend")
js = (ROOT / "frontend/js/deletions.js").read_text(encoding="utf-8")
cov_fn = js.split("export function _renderEnrichmentCoverageBanner")[1].split("\n}\n")[0]
check("the coverage banner handles unknown before never_run",
      "cov.unknown" in cov_fn and cov_fn.index("cov.unknown") < cov_fn.index("cov.never_run")
      and "coverage unknown" in cov_fn)
load_fn = js.split("export async function loadDeletions")[1].split("\n}\n")[0]
check("loadDeletions renders the history-unreadable note",
      "_renderActivityBanner(" in load_fn and "activity_note" in load_fn)
run_fn = js.split("export function _renderRunBanner")[1].split("\n}\n")[0]
check("the run banner shows a degraded run", "run.degraded" in run_fn
      and "esc(run.degraded)" in run_fn)

# ── 5. deletion run: a corrupt taste profile degrades LOUDLY ────────────────
print("taste profile")
import numpy as np  # noqa: E402
import src.services.episodic_memory as epm  # noqa: E402
import src.services.llm_priority as llp  # noqa: E402
import src.services.size_norms as sn  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402

DIM = 8


def unit(i, dim=DIM):
    v = [0.0] * dim
    v[i] = 1.0
    return v


TASTE = unit(0)
_vectors: dict = {}


async def _run_chroma(fn, *a, **k):
    return fn(*a, **k)


async def _pitcher():
    return "test-model"


async def _no_cons(*a, **k):
    return []


cw.run_chroma = _run_chroma
cw.get_chroma_db = lambda: SimpleNamespace(
    get_by_ids=lambda ids, chunk_size=1000: {
        i: {"id": i, "embedding": _vectors[i]} for i in ids if i in _vectors})
eng._bulk_cached_ratings = lambda items, cat: {}
sn.load_tech_index = lambda: {}
llp.resolve_pitcher_model = _pitcher
eng._judge_lane = lambda: (False, "test stops before the judge")
epm.retrieve_considerations = _no_cons
settings.PILLARS_ENABLED = True


def seed(blob_raw=None, genres_raw=None):
    with conn_mod.get_db_session() as db:
        db.query(TasteVectorEntry).delete()
        db.query(EncryptedTasteVector).delete()
        db.add(TasteVectorEntry(user_id=1, summary_text="[MOVIE] taste",
                                genre_affinity=genres_raw))
        if blob_raw is not None:
            db.add(EncryptedTasteVector(user_id=1, media_category="movie",
                                        salt="unencrypted", encrypted_blob=blob_raw))
        db.commit()


def item(title, vec=None):
    key = f"radarr:{title}"
    _vectors.pop(key, None)
    if vec is not None:
        _vectors[key] = vec
    return {"title": title, "plex_rating_key": key, "size_mb": 40 * 1024,
            "genres": "Drama", "service": "radarr", "arr_id": 1}


def run(items):
    eng.DELETION_RUN_SUMMARY.clear()
    asyncio.run(eng.generate_deletion_proposals(1, items, category="movie"))
    return eng.DELETION_RUN_SUMMARY.get("movie") or {}


good_genres = json.dumps({"movie": {"genre_affinity": {"drama": 1.0}}})
log = capture(eng.logger)
seed("{not json", good_genres)
summary = run([item("A", unit(1))])
check("a corrupt taste blob is logged at WARNING", log.warnings("taste blob is corrupt"))
check(f"...and named on the run record ({summary.get('degraded')!r})",
      "taste vector" in (summary.get("degraded") or ""))

log = capture(eng.logger)
seed("{not json", "{also not json")
summary = run([item("A", unit(1))])
check(f"an unreadable profile is not reported as a cold start ({summary.get('stopped')!r})",
      "could not read the stored taste profile" in (summary.get("stopped") or "")
      and "no taste profile for this category yet" not in (summary.get("stopped") or ""))
check("...the unreadable genre affinity is logged at WARNING",
      log.warnings("genre affinity unreadable"))
seed(None, None)
summary = run([item("A", unit(1))])
check("a real cold start still says so",
      "no taste profile for this category yet" in (summary.get("stopped") or ""))

# cosine that raises: wrong-dimension item vectors against an 8-d taste
log = capture(eng.logger)
seed(json.dumps({"version": 0, "embedding": TASTE}), good_genres)
run([item("Short1", unit(1, 4)), item("Short2", unit(2, 4)), item("Fit", TASTE)])
cos_warn = log.warnings("taste cosine failed")
check(f"failed cosines: ONE warning per run with the count ({len(cos_warn)})",
      len(cos_warn) == 1 and "2 candidate(s)" in cos_warn[0].getMessage())

# ── 6. recommendations: one odd dislike no longer wipes the taste vector ────
print("recommendation ranking")
eng._chroma_get_by_ids = lambda ids: {
    i: {"id": i, "embedding": _vectors[i]} for i in ids if i in _vectors}
seed(json.dumps({"version": 0, "embedding": TASTE, "disliked_titles": [123, "Nope"]}),
     None)
far, fit = item("Far", unit(1)), item("Fit", TASTE)
ranked = asyncio.run(eng.score_arr_items(1, "movie", [far, fit], top_n=2))
check("a non-string disliked_titles entry keeps the taste ranking",
      [i["title"] for i in ranked] == ["Fit", "Far"])
log = capture(eng.logger)
seed("[1, 2]", None)
asyncio.run(eng.score_arr_items(1, "movie", [far, fit], top_n=2))
check("a corrupt recs taste blob is logged at WARNING",
      log.warnings("taste blob is corrupt"))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
