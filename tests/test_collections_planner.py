"""Tests for the collection designer mapping core (Block 5).

    python tests/test_collections_planner.py
"""
from _harness import check  # noqa: I001 - first: temp stores

from pathlib import Path


from src.services.collection_designer import (MAX_ITEMS, MIN_ITEMS,
                                              PLEX_TYPE, map_designs)
from src.services.library_memory import normalize_title
from src.services.plex_collections import COLLECTION_PREFIX


POOL = [{"title": f"Movie {i}", "plex_rating_key": str(1000 + i)} for i in range(20)]
POOL.append({"title": "The 'Burbs", "plex_rating_key": "9999"})
BY_TITLE = {c["title"]: c for c in POOL}
BY_NORM = {normalize_title(c["title"]): c for c in POOL}

# happy path + hallucination drop + normalize fallback
themes = [{
    "title": "Suburban Dread",
    "description": "Something is wrong behind the hedges.",
    "items": ["Movie 1", "Movie 2", "Movie 3", "the burbs",     # normalized match
              "Totally Invented Film", "Movie 4"],
}]
d = map_designs(themes, BY_TITLE, BY_NORM, "10", PLEX_TYPE["movie"])
check("theme mapped", len(d) == 1)
check("prefix applied", d[0]["title"] == f"{COLLECTION_PREFIX}Suburban Dread")
check("hallucinated title dropped, normalized match kept",
      d[0]["keys"] == ["1001", "1002", "1003", "9999", "1004"])
check("section + plex_type carried",
      d[0]["section_key"] == "10" and d[0]["plex_type"] == 1)

# min-size discard
small = [{"title": "Tiny", "items": ["Movie 1", "Movie 2", "Nope", "Nada"]}]
check(f"theme with <{MIN_ITEMS} resolved keys discarded",
      map_designs(small, BY_TITLE, BY_NORM, "10", 1) == [])

# cap + dedup
big = [{"title": "Everything", "items": [f"Movie {i}" for i in range(20)] * 2}]
d = map_designs(big, BY_TITLE, BY_NORM, "11", 2)
check(f"keys deduped and capped at {MAX_ITEMS}",
      len(d[0]["keys"]) == MAX_ITEMS == len(set(d[0]["keys"])))

# malformed input tolerated
check("garbage themes tolerated",
      map_designs([{"items": "notalist"}, {"title": ""}, None and {} or {}],
                  BY_TITLE, BY_NORM, "10", 1) == [])
check("empty themes -> empty", map_designs([], BY_TITLE, BY_NORM, "10", 1) == [])

# ── wiring asserts ───────────────────────────────────────────────────────────

root = Path(__file__).resolve().parents[1]
# The prefix guard (Kometa coexistence) and the writes themselves are
# behaviour tests now: tests/test_plex_collection_slots.py.

dc = (root / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("custodian task registered with needs_llm",
      '"plex_collections", "Curatarr collections", 168.0' in dc
      and dc.split('"plex_collections"')[1][:120].count("needs_llm=True") == 1)

# All Plex collection writes live behind the media-server seam
offenders = []
for p in (root / "src").rglob("*.py"):
    if p.name in ("plex_adapter.py",):
        continue
    if "/library/collections" in p.read_text(encoding="utf-8"):
        offenders.append(p.name)
check("collection endpoint touched ONLY by the Plex adapter",
      offenders == [])

cd = (root / "src/services/collection_designer.py").read_text(encoding="utf-8")
check("designer prompt carries seasonal context",
      "seasonal_context()" in cd)

check.done()
