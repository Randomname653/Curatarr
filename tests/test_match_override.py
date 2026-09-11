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
      # the pin is now read by one helper (_match_override_ids) which keeps
      # only the ids the owner actually set, and BOTH resolution paths apply
      # them with `or` so an unset id leaves normal resolution untouched
      me.count('_pin = _match_override_ids(plex_rating_key)') == 2
      and me.count('tmdb_id = _pin.get("tmdb_id") or tmdb_id') == 2
      and me.count('mbid = _pin.get("mbid") or mbid') == 2)
check("the on-demand path honours the pin too, not just the nightly walk",
      # it did not: a pinned title reverted whenever the judge / chat / re-eval
      # enriched it live, because the pin lived only in fetch_and_prepare_raw
      me.index("_pin = _match_override_ids(plex_rating_key)")
      < me.index("async def enrich_media_item(")
      < me.rindex("_pin = _match_override_ids(plex_rating_key)"))
check("the tvdb id authority never overrules a pin",
      me.count('not _pin.get("tmdb_id")') == 2)

en = (root / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("candidate search endpoint (TMDB, admin-gated, twins distinguishable)",
      '@router.get("/match-candidates")' in en and '"overview"' in en)
check("apply endpoint validates ids and requires at least one",
      "at least one external id required" in en)
check("apply/unpin purge BOTH cache-key epochs and flip BOTH status tables",
      "raw_prefetch:{prk}" in en
      and en.count("_purge_and_requeue_item(") >= 3   # def + apply + delete
      and "ArrEnrichmentStatus" in en.split("_purge_and_requeue_item")[1])

from tests.frontend_files import everything
fe = everything()
check("Fix match button on deletion cards + pin/unpin handlers",
      "onFixMatch" in fe and "openMatchPicker" in fe and "removeFixMatch" in fe
      and "/api/enrichment/match-override" in fe)

ser = (root / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("proposal serializer carries media_id for the pin",
      '"media_id": p.media_id' in ser)

# ── healing 2026-09: negative pin + generalized picker ───────────────────────
check("negative pin column on the override (\"Not this one\")",
      "rejected_ids" in cols)
check("rejected ids ride into ALL four resolvers; a rejected arr id is treated as absent",
      'ctx["rejected"]' in me
      and 'rejected=rejected.get("tmdb_id")' in me and 'rejected=rejected.get("anilist_id")' in me
      and 'rejected=rejected.get("mal_id")' in me and 'rejected=rejected.get("mbid")' in me
      and '_ok("tmdb_id", ctx.get("tmdb_id"))' in me)
mm = (root / "src/services/music_metadata.py").read_text(encoding="utf-8")
check("MusicBrainz name search widened to 5 and skips excluded mbids",
      '"limit": 5, "fmt": "json"' in mm and 'a.get("id") not in rejected' in mm)
_apply = en.split("async def apply_match_override")[1].split("\n@router")[0]
check("apply endpoint accepts the rejected list, derives the category, drops the KB cache",
      '"rejected"' in _apply and "_derive_category(" in _apply and "_kb.invalidate()" in _apply
      and 'or "movie"' not in _apply)
check("candidates come from the arr's own lookup + TMDB + AniList, keyed by item",
      "lookup_series(title)" in en and "anilist_candidates" in en
      and "service: Optional[str] = None" in en)
check("the picker is shared: deletion cards open the same renderMatchPicker",
      "renderMatchPicker(box" in fe and fe.count("renderMatchPicker(") >= 2
      and "p.category||'movie'" not in fe)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
