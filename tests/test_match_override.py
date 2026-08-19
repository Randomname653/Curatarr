"""Owner match overrides — durable entity pins (SoulSync port, MIT).

The Good-Boy-twins / Batman-Beyond class: automatic guards shrink
wrong-entity resolutions, only an owner pin CLOSES a case. Pin semantics
(mirroring SoulSync core/sync/match_overrides.py): one mapping per
(service, arr_id) via UNIQUE constraint; read at the very START of
fetch_and_prepare_raw, before arr ids, MediaIdentity and every title
search; applying purges the item's cached rows + flips enrichment
status so the pipeline rebuilds on the pin.

    python tests/test_match_override.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.database.models import MediaMatchOverride

cols = {c.name for c in MediaMatchOverride.__table__.columns}
check("model carries every pinnable external id",
      {"tmdb_id", "tvdb_id", "anilist_id", "mal_id", "imdb_id", "mbid"} <= cols)
uqs = [c for c in MediaMatchOverride.__table__.constraints
       if getattr(c, "name", "") == "uq_match_override_item"]
check("ONE pin per (service, arr_id) — unique constraint",
      uqs and {col.name for col in uqs[0].columns} == {"service", "arr_id"})

root = Path(__file__).resolve().parents[1]
me = (root / "src/services/media_enricher.py").read_text(encoding="utf-8")
check("fetch_and_prepare_raw reads the pin FIRST (highest authority)",
      "OWNER MATCH OVERRIDE" in me
      and me.index("OWNER MATCH OVERRIDE") < me.index("Resolve IDs from MediaIdentity"))
check("pinned ids override, unset ids leave resolution alone",
      "tmdb_id = ov.tmdb_id or tmdb_id" in me
      and "mbid = ov.mbid or mbid" in me)

en = (root / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("candidate search endpoint (TMDB, admin-gated, twins distinguishable)",
      '@router.get("/match-candidates")' in en and '"overview"' in en)
check("apply endpoint validates ids and requires at least one",
      "at least one external id required" in en)
check("apply/unpin purge BOTH cache-key epochs and flip BOTH status tables",
      "raw_prefetch:{prk}" in en
      and en.count("_purge_and_requeue_item(") >= 3   # def + apply + delete
      and "ArrEnrichmentStatus" in en.split("_purge_and_requeue_item")[1])

fe = (root / "frontend/index.html").read_text(encoding="utf-8")
check("Fix match button on deletion cards + pin/unpin handlers",
      "onFixMatch" in fe and "applyFixMatch" in fe and "removeFixMatch" in fe
      and "/api/enrichment/match-override" in fe)

ser = (root / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("proposal serializer carries media_id for the pin",
      '"media_id": p.media_id' in ser)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
