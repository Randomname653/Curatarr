"""show/anime taste vectors aggregate per SERIES, and drops are decided per
series — never per dozed-off episode.

Found in the 2026-10 vector/memory audit: the series profile is aliased
onto every played episode key, so each episode row entered the centroid as
another copy of the same series vector (×2.0 replay bonus from the 11th
episode). A 300-episode sitcom outweighed a miniseries ~66× and a film
~600×; a half-watched episode of that sitcom also pushed the sitcom's own
vector into the DROP centroid.

    python tests/test_series_taste_aggregation.py
"""
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services.taste_engine import (  # noqa: E402
    DROP_GRACE_DAYS, _is_drop, _naive_utc, _series_entries, recency_weight)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


NOW = datetime(2026, 10, 15, 12, 0, 0)
HOUR = 3_600_000


def ep(series, n, days_ago, pct=1.0, done=None):
    """One episode play row, shaped like _load_watch_entries' dicts."""
    return {"plex_item_id": f"{series}-{n}", "title": f"Episode {n}",
            "series_title": series, "media_type": "show", "genres": "Comedy",
            "duration_ms": HOUR, "view_offset_ms": int(HOUR * pct),
            "completed": (pct >= 0.9) if done is None else done,
            "viewed_at": NOW - timedelta(days=days_ago)}


def series_weights(entries, canonical=None, has_profile=lambda e: True):
    rows = sorted(entries, key=lambda e: e["viewed_at"], reverse=True)
    return {rep.get("series_title") or rep["title"]: w
            for w, rep in _series_entries(rows, NOW, canonical or {}, has_profile)}


# ── volume vs intent: one vote per series ────────────────────────────────────

sitcom = [ep("Sitcom", i, 0) for i in range(300)]
mini = [ep("Miniseries", i, 0) for i in range(6)]
w = series_weights(sitcom + mini)
new_ratio = w["Sitcom"] / w["Miniseries"]
# the old row-level weight: Σ recency × completion × min(2, 1 + 0.1·(n−1))
old_ratio = (300 * min(2.0, 1 + 0.1 * 299)) / (6 * min(2.0, 1 + 0.1 * 5))
check(f"old row weighting gave the sitcom {old_ratio:.0f}x the miniseries", old_ratio > 60)
check(f"series weighting: {new_ratio:.2f}x (log-scaled depth, not volume)",
      1.5 < new_ratio < 2.5)
check("weight = recency × (1 + log10(finished episodes))",
      math.isclose(w["Sitcom"], recency_weight(NOW, NOW) * (1 + math.log10(300))))
check("one entry per series, not per episode", len(w) == 2)

# ── in-progress and dozed-off episodes ───────────────────────────────────────

w = series_weights([ep("Comfort", i, 2) for i in range(40)]
                   + [ep("Comfort", 99, 60, pct=0.2)])
check("a dozed-off episode of a show with finished episodes is not a drop",
      w.get("Comfort", 0) > 0)

w = series_weights([ep("Comfort", i, 2) for i in range(40)]
                   + [ep("Comfort", 41, 0, pct=0.6, done=False)])
check("a 40-90% episode is not counted as a finished view",
      math.isclose(w["Comfort"], recency_weight(NOW - timedelta(days=0), NOW)
                   * (1 + math.log10(40))))

w = series_weights([ep("NewShow", 1, 0, pct=0.6, done=False)])
check("new series, first episode in progress (60%) -> undecided, no entry",
      "NewShow" not in w)

w = series_weights([ep("NewShow", 1, 3, pct=0.2)])
check("new series at 20%, 3 days ago -> still undecided (grace)", "NewShow" not in w)

w = series_weights([ep("Abandoned", 1, 45, pct=0.2)])
check("first episode at 20%, untouched for 45 days -> series drop",
      w.get("Abandoned", 0) < 0)

w = series_weights([ep("Paused", 1, 45, pct=0.7, done=False)])
check("first episode at 70%, untouched -> ambiguous, not a drop", "Paused" not in w)

w = series_weights([ep("Sampled", i, 45, pct=0.2) for i in range(3)])
check("three episodes started (> SERIES_DROP_MAX_EPISODES) -> not a drop",
      "Sampled" not in w)

ordered = _series_entries(
    sorted(sitcom + [ep("Abandoned", 1, 45, pct=0.2)],
           key=lambda e: e["viewed_at"], reverse=True), NOW, {}, lambda e: True)
check("entries ordered by |weight|",
      [abs(x) for x, _ in ordered] == sorted((abs(x) for x, _ in ordered), reverse=True))

# ── representative entry (what gets embedded) ────────────────────────────────

rows = [ep("Sitcom", i, i) for i in range(5)]
canon = {"Sitcom": "sonarr:42"}
reps = _series_entries(rows, NOW, canon, lambda e: True)
check("canonical enrichment key is the representative when it has a profile",
      reps[0][1]["plex_item_id"] == "sonarr:42" and reps[0][1]["title"] == "Sitcom")
reps = _series_entries(rows, NOW, canon,
                       lambda e: e["plex_item_id"] == "Sitcom-1")
check("falls back to the newest row that has a profile",
      reps[0][1]["plex_item_id"] == "Sitcom-1")
check("a series with no profile anywhere is left to the genre counters",
      _series_entries(rows, NOW, canon, lambda e: False) == [])

# ── _is_drop grace window (movies use the row-level path) ────────────────────

movie = {"completed": False, "duration_ms": HOUR, "view_offset_ms": int(HOUR * 0.2)}
check("20% watched yesterday -> still in progress, not a drop",
      not _is_drop(dict(movie, viewed_at=NOW - timedelta(days=1)), NOW))
check(f"20% watched {DROP_GRACE_DAYS + 1} days ago -> drop",
      _is_drop(dict(movie, viewed_at=NOW - timedelta(days=DROP_GRACE_DAYS + 1)), NOW))
check("without `now` the old rule holds (<40% and not completed)",
      _is_drop(dict(movie, viewed_at=NOW)))
check("60% is never a drop", not _is_drop(dict(movie, view_offset_ms=int(HOUR * 0.6),
                                               viewed_at=NOW - timedelta(days=90)), NOW))

# ── viewed_at normalisation ──────────────────────────────────────────────────

naive = datetime(2026, 10, 1, 8, 0)
check("naive datetime passes through", _naive_utc(naive) == naive)
check("aware datetime -> naive UTC",
      _naive_utc(datetime(2026, 10, 1, 10, 0, tzinfo=timezone(timedelta(hours=2)))) == naive)
check("ISO string with Z -> naive UTC", _naive_utc("2026-10-01T08:00:00Z") == naive)
check("unparseable -> None", _naive_utc("yesterday") is None)
check("recency_weight accepts an ISO string instead of crashing",
      math.isclose(recency_weight("2026-10-15T12:00:00", NOW), 1.0))
check("recency_weight on an aware datetime matches the naive one",
      recency_weight(datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc), NOW)
      == recency_weight(datetime(2026, 8, 16, 12, 0), NOW))

# ── end to end: compute_taste_vector_for_user on a real (in-memory) DB ───────

import asyncio  # noqa: E402
from contextlib import contextmanager  # noqa: E402

import numpy as np  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.cache.metadata_cache as mc_mod  # noqa: E402
import src.services.embed_service as es  # noqa: E402
import src.services.taste_engine as te  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
from src.database.models import Base, EnrichmentStatus, WatchHistoryEntry  # noqa: E402

engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def _session():
    db = Session()
    try:
        yield db
        db.commit()
    finally:
        db.close()


te.get_db_session = _session
real_now = datetime.utcnow()
with _session() as db:
    for i in range(300):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="p1", plex_item_id=f"sit-{i}", title=f"Ep {i}",
                                 series_title="Sitcom", media_type="show", genres="Comedy",
                                 duration_ms=HOUR, view_offset_ms=HOUR, completed=True,
                                 viewed_at=real_now - timedelta(hours=i)))
    for i in range(6):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="p1", plex_item_id=f"mini-{i}", title=f"Part {i}",
                                 series_title="Miniseries", media_type="show", genres="Drama",
                                 duration_ms=HOUR, view_offset_ms=HOUR, completed=True,
                                 viewed_at=real_now - timedelta(hours=i)))
    for i in range(12):   # dozed off in a dozen sitcom episodes, weeks ago
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="p1", plex_item_id=f"sit-p{i}", title=f"Ep p{i}",
                                 series_title="Sitcom", media_type="show", genres="Comedy",
                                 duration_ms=HOUR, view_offset_ms=int(HOUR * 0.2),
                                 completed=False, viewed_at=real_now - timedelta(days=40 + i)))
    db.add(EnrichmentStatus(plex_rating_key="sonarr:1", title="Sitcom",
                            media_category="show", enriched=True, enriched_at=real_now))
    db.add(EnrichmentStatus(plex_rating_key="sonarr:2", title="Miniseries",
                            media_category="show", enriched=True, enriched_at=real_now))

# the series profile is aliased onto every episode key (enrichment.py)
SIT, MINI = [1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0]


def _profile_for(key):
    if "sit" in key or key.endswith("sonarr:1"):
        return {"title": "Sitcom", "genres": ["Comedy"], "embedding_text": "SITCOM"}
    if "mini" in key or key.endswith("sonarr:2"):
        return {"title": "Miniseries", "genres": ["Drama"], "embedding_text": "MINI"}
    return None


class _FakeCache:
    def __init__(self, *a, **k): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def close(self): pass
    def set_cache(self, *a, **k): pass
    def get_cache(self, key):
        if key.startswith("enriched:"):
            p = _profile_for(key)
            return {"response": p} if p else None
        return None
    def get_cache_many(self, keys):
        return {k: self.get_cache(k) for k in keys if self.get_cache(k)}


async def _fake_embed(texts, profile=None):
    return [list(SIT) if "SITCOM" in t else list(MINI) for t in texts]

te.MetadataCache = _FakeCache
mc_mod.MetadataCache = _FakeCache
es.embed_documents = _fake_embed
cw.get_chroma_db = lambda: type("C", (), {"get_by_id": lambda s, i: None,
                                         "embeddings_for_domain": lambda s, d, limit=30000: []})()

res = asyncio.run(te.compute_taste_vector_for_user(1, category="show"))
emb = np.asarray(res["embedding"])
cos_mini = float(emb @ np.asarray(MINI))
check(f"end to end: the miniseries keeps a real share of the show vector (cos {cos_mini:.2f})",
      cos_mini > 0.3)
check("end to end: the sitcom still leads (deeper engagement)",
      float(emb @ np.asarray(SIT)) > cos_mini)
check("end to end: dozed-off sitcom episodes leave the drop centroid empty",
      res["drop_embedding"] is None)
check("end to end: one embedding per series", res["embedding_items"] == 2)

# ── wiring ───────────────────────────────────────────────────────────────────

src = (ROOT / "src/services/taste_engine.py").read_text(encoding="utf-8")
check("show/anime rebuild goes through _series_entries",
      'if category in ("show", "anime"):' in src
      and "_series_entries(entries, now, series_key_by_title, _has_profile)" in src)
check("row-level drop checks pass `now` (grace window)",
      "_is_drop(e)" not in src and "if _is_drop(e, now):" in src
      and src.count("if _is_drop(e, now):") == 2)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
