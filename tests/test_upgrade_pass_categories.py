"""The hourly source-upgrade pass enriches its items and leaves the taste
profiles alone when nothing changed.

NAS log 2026-11-14, every hour at :15: "Enrichment start: 4 items across []",
"Producer lanes started (0 categories, 0 items total)", "0 items processed",
then "Computing taste vectors for user 1 categories=[]" - a full rebuild of
all four profiles (380k music plays, ~5,000 embeddings, the curator's
summaries) that also pushed the container from ~0.9 to 1.5 GB. Two causes:
the pass hands an empty category list (the lanes are built per category),
and an empty list reached compute_all_taste_vectors, which reads [] as
"every category".

    python tests/test_upgrade_pass_categories.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import asyncio

conn = app_db()
from src.database.models import EnrichmentStatus, User  # noqa: E402
from src.routers import enrichment as en  # noqa: E402
from src.services import app_state, taste_engine  # noqa: E402

# ── 1. [] is "no category", None is "all" ──────────────────────────────────
calls: list = []


async def _fake_compute(uid, categories=None, **kw):
    calls.append((uid, categories))

real_compute = taste_engine.compute_all_taste_vectors
taste_engine.compute_all_taste_vectors = _fake_compute
try:
    asyncio.run(en._run_taste_computation(1, []))
    check("an empty category list rebuilds nothing", calls == [], calls)
    asyncio.run(en._run_taste_computation(1, None))
    asyncio.run(en._run_taste_computation(1, ["music"]))
    check("None still means every category, a list only its own",
          calls == [(1, None), (1, ["music"])], calls)
finally:
    taste_engine.compute_all_taste_vectors = real_compute

# ── 2. the upgrade pass gets lanes for its own items ───────────────────────
with conn.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True, is_active=True))
    db.add(EnrichmentStatus(plex_rating_key="m1", title="Glass Meridian", media_category="movie",
                            enriched=True, fetch_tier="fast", provisional=True))
    db.add(EnrichmentStatus(plex_rating_key="a1", title="Lantern Row", media_category="anime",
                            enriched=True, fetch_tier="fast", provisional=True))


class _Seen(Exception):
    """Stops the run where the lanes are laid out: no API, no LLM."""


seen: dict = {}


def _capture(by_cat):
    seen.update({c: [i["plex_rating_key"] for i in v] for c, v in by_cat.items()})
    raise _Seen()

real_interleave = en._interleave_by_cat
en._interleave_by_cat = _capture
app_state.set_state("enrichment_running", "1")   # the job hands over its lock
try:
    try:
        asyncio.run(en._run_enrichment(user_id=1, categories=[], source="upgrade", limit=None,
                                       force=True, fast_only=False,
                                       specific_plex_rating_keys=["m1", "a1"]))
    except _Seen:
        pass
finally:
    en._interleave_by_cat = real_interleave
check("each targeted item lands in its own category's lane",
      seen == {"anime": ["a1"], "movie": ["m1"]}, seen)
check("the run still releases the enrichment lock",
      app_state.get_state("enrichment_running") == "0")

check.done()
