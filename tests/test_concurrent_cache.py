"""Concurrent top-ups must not overwrite each other's work.

    python tests/test_concurrent_cache.py

Offline, on a throwaway database — the live cache is never touched.

Why this exists
---------------
Every archive walker has the same shape: read a whole raw cache entry, spend
seconds (HTTP) to minutes (the summariser) inside an ``await``, then write the
whole entry back. Run two of them at once over the same library and the later
write silently discards the earlier one's field — both callers return True, the
walker reports a title as done, and the value is gone.

It self-heals, because the ``*_checked`` marker is discarded along with the
value and the next pass redoes the title. What it costs is the work: API calls
against rate-limited services, and GPU minutes on a card that is also the
household's games machine. A backfill run with four walkers going at once can
spend a large share of both on results it throws away.

WAL and a 60s busy_timeout do not help here. They serialise writers and stop
"database is locked"; they do nothing about a writer whose in-memory copy is
older than the row it is about to replace.
"""
import asyncio
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


from src.cache.metadata_cache import MetadataCache, write_fields  # noqa: E402

TMPDIR = Path(tempfile.mkdtemp(prefix="curatarr-cache-test-"))
DB = TMPDIR / "cache.db"
KEY = "raw:anime:99999"


def _fresh():
    cache = MetadataCache(DB)
    cache.conn.execute("DELETE FROM api_cache")
    cache.conn.commit()
    cache.set_cache(KEY, {"title": "Test", "imdb_id": "tt1"}, days=30)
    return cache


# ── the failure, reproduced ─────────────────────────────────────────────────
# Two walkers, two connections, overlapping read-modify-write windows. The slow
# one reads first and writes last, so it wins and the fast one's field is gone.

async def _whole_row(cache, field, value, seconds):
    raw = cache.get_cache(KEY)["response"]
    await asyncio.sleep(seconds)
    raw[field] = value
    raw[field + "_checked"] = True
    cache.set_cache(KEY, raw, days=30)


async def _fields(cache, field, value, seconds):
    raw = cache.get_cache(KEY)["response"]
    await asyncio.sleep(seconds)
    write_fields(cache, KEY, raw,
                 {field: value, field + "_checked": True}, days=30)


async def _race(worker):
    a, b = _fresh(), MetadataCache(DB)
    await asyncio.gather(
        worker(a, "significance", "a documented classic", 0.30),
        worker(b, "wikidata", {"awards": ["Palme d'Or"]}, 0.05),
    )
    out = a.get_cache(KEY)["response"]
    a.close()
    b.close()
    return out


lost = asyncio.run(_race(_whole_row))
check("a whole-row write discards the other walker's field",
      "significance" in lost and "wikidata" not in lost)
check("...and its 'checked' marker goes with it, so the work is repeated "
      "rather than lost for good",
      "wikidata_checked" not in lost)

kept = asyncio.run(_race(_fields))
check("writing only the owned fields keeps both",
      kept.get("significance") == "a documented classic"
      and kept.get("wikidata") == {"awards": ["Palme d'Or"]})
check("...and both markers survive",
      kept.get("significance_checked") and kept.get("wikidata_checked"))
check("untouched fields are left alone",
      kept.get("title") == "Test" and kept.get("imdb_id") == "tt1")

# ── replace, not merge ──────────────────────────────────────────────────────
# json_patch merges nested objects by default. A re-check that now returns
# fewer facts must not leave the previous answer's sub-keys standing.

cache = _fresh()
write_fields(cache, KEY, {}, {"wikidata": {"awards": ["old"], "source_work": "X"}}, days=30)
write_fields(cache, KEY, {}, {"wikidata": {"awards": ["new"]}}, days=30)
check("a nested value is replaced, not merged with its predecessor",
      cache.get_cache(KEY)["response"]["wikidata"] == {"awards": ["new"]})

write_fields(cache, KEY, {}, {"significance": "text"}, days=30)
write_fields(cache, KEY, {}, {"significance_checked": True},
             drop=("significance",), days=30)
final = cache.get_cache(KEY)["response"]
check("a dropped field is removed while the stamp beside it is written",
      "significance" not in final and final.get("significance_checked") is True)

check("patching a row that does not exist reports so, rather than inventing one",
      cache.patch_cache("raw:movie:nope", {"x": 1}) is False)

raw = {"title": "First", "y": 2}
write_fields(cache, "raw:movie:first", raw, {"x": 1}, days=30)
check("...and the caller's fallback writes the whole row instead",
      (cache.get_cache("raw:movie:first") or {}).get("response", {}).get("x") == 1)
cache.close()

# ── every walker actually uses it ───────────────────────────────────────────
# The guard that matters: a new top-up written in the old shape reintroduces
# the bug silently, because nothing about it looks wrong.

WALKERS = {
    "src/services/media_enricher.py": ("topup_significance", "topup_omdb"),
    "src/services/reception.py": ("topup_reception",),
    "src/services/wikidata.py": ("topup_wikidata",),
    "src/services/external_ids.py": ("harvest",),
}
for path, fns in WALKERS.items():
    src = (ROOT / path).read_text(encoding="utf-8")
    check(f"{Path(path).name} writes fields, not whole rows",
          "write_fields(cache" in src
          and "cache.set_cache(key, raw" not in src)

shutil.rmtree(TMPDIR, ignore_errors=True)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
