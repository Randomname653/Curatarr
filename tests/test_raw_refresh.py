"""LLM-free raw-cache refresher + cache-inventory endpoint wiring.

Owner ask 2026-08-18: the read-through cache let 103k rows silently age
out (nothing re-pulls what nobody queries). run_raw_refresh keeps the
RAW layer warm in the background — discovery cards and expired prefetch
rows (stale ids re-fetch even gone-from-arr media) — pure API work, no
LLM; the enrichment cycle runs on top via its own custodian task.

    python tests/test_raw_refresh.py
"""
import asyncio
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


import src.services.raw_refresh as rr
import src.services.media_enricher as me
import src.cache.metadata_cache as mcache


class FakeMC:
    writes = []

    def set_cache(self, key, value, days=None):
        FakeMC.writes.append((key, days))

    def get_cache(self, key):
        return None

    def close(self):
        pass


mcache.MetadataCache = FakeMC

calls = []


async def _fake_fetch(title, media_type, **kw):
    calls.append((title, media_type, kw.get("tmdb_id")))
    if title == "Enriched Fresh":
        return {"_already_enriched": True}
    if title == "No Data":
        return None
    return {"title": title, "media_type": media_type,
            "_cache_key": f"raw:{media_type}:{kw.get('tmdb_id')}",
            "_plex_rating_key": kw.get("plex_rating_key")}

me.fetch_and_prepare_raw = _fake_fetch

ok = asyncio.run(rr._refresh_one({
    "title": "Gone Movie", "media_type": "tv", "tmdb_id": 42,
    "_plex_rating_key": "sonarr:629"}))
check("refresh re-fetches by stored ids and writes raw + prefetch back",
      ok and ("raw:show:42", 30) in FakeMC.writes
      and ("raw_prefetch:sonarr:629", 30) in FakeMC.writes)
check("tv normalized to show before the fetch",
      calls[-1][1] == "show")

FakeMC.writes.clear()
check("fresh enriched profile -> skip (no writes)",
      asyncio.run(rr._refresh_one({"title": "Enriched Fresh",
                                   "media_type": "movie"})) is False
      and FakeMC.writes == [])
check("no API data -> False", asyncio.run(rr._refresh_one(
    {"title": "No Data", "media_type": "movie"})) is False)
check("no title -> refuse", asyncio.run(rr._refresh_one({})) is False)

# ── wiring ───────────────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
dc = (root / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("custodian runs the refresher LLM-free with card + deep budget",
      '"raw_refresh"' in dc and "_run_raw_refresh, takes_deep=True, takes_task=True" in dc
      and "needs_llm" not in dc.split('"raw_refresh"')[1][:200])

en = (root / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("cache-inventory endpoint exists with per-class + coverage stats",
      '@router.get("/cache-inventory")' in en
      and '"significance_text"' in en and '"omdb_writer"' in en)

fe = (root / "frontend/index.html").read_text(encoding="utf-8")
check("KB view renders the inventory on demand",
      "loadCacheInventory" in fe and "/api/enrichment/cache-inventory" in fe)

rrs = (root / "src/services/raw_refresh.py").read_text(encoding="utf-8")
check("discovery cards get a bounded share of the budget",
      "budget // 3" in rrs)
check("expired prefetch rows are read STALE by design (cursor-resumable)",
      "expires_at <= datetime('now')" in rrs and "_CURSOR_KEY" in rrs)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
