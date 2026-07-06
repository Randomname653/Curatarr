"""
Franchise/AniDB-gold prototype — TEST BEFORE BUILD (stdlib runner, no pytest).

AniDB is rate-limited into unusability (1 req/2-4s, ~150/day, bans), so the
plan is: (1) the manami-project anime-offline-database weekly dump as the
offline carrier of AniDB's gold (all provider ids, tags, related anime),
(2) AniList's TYPED relations (PREQUEL/SEQUEL/...) via the API we already
call. This proto proves both on real library titles before any wiring:

  A. DUMP — download the weekly release, verify structure (sources ids,
     tags, relatedAnime), and resolve OUR titles: via anidb id (from the
     existing tvdb mapping) and via title+year search.
  B. RELATIONS — AniList typed relations for Lostorage WIXOSS + Takamine.
  C. LIBRARY MATCH — do the relation titles exist in the user's own anime
     cache? (the "predecessor is IN your library" evidence line)

Run from repo root:  python tests/franchise_proto.py
The dump (~tens of MB) is cached in the scratchpad between runs.
"""
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import httpx

DUMP_URL = ("https://github.com/manami-project/anime-offline-database"
            "/releases/latest/download/anime-offline-database-minified.json")
SCRATCH = Path(os.environ.get("PROTO_SCRATCH", Path(__file__).parent)) / "anime_offline_proto.json"

CASES = [
    # (title, year, anidb_id from mapping, anilist_id) — anidb 13361 = Lostorage
    ("Lostorage incited WIXOSS", 2016, 13361, 21772),
    ("Please Put Them On, Takamine-san", 2025, None, 179965),
    ("Chihayafuru", 2011, None, 10800),
]


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


async def phase_a() -> list[dict]:
    print("=" * 78)
    print("PHASE A — anime-offline-database weekly dump")
    print("=" * 78)
    if SCRATCH.exists() and SCRATCH.stat().st_size > 1_000_000:
        print(f"  using cached dump: {SCRATCH} ({SCRATCH.stat().st_size/1e6:.1f} MB)")
    else:
        t0 = time.time()
        async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
            r = await client.get(DUMP_URL)
            r.raise_for_status()
            SCRATCH.write_bytes(r.content)
        print(f"  downloaded {len(r.content)/1e6:.1f} MB in {time.time()-t0:.0f}s")
    t0 = time.time()
    data = json.loads(SCRATCH.read_text(encoding="utf-8"))
    entries = data.get("data") or []
    print(f"  parsed {len(entries)} entries in {time.time()-t0:.1f}s "
          f"(schema keys: {sorted(entries[0].keys())})")

    # index: by provider id + by normalized title/synonym
    by_anidb, by_anilist, by_title = {}, {}, {}
    for e in entries:
        for src in e.get("sources") or []:
            m = re.search(r"anidb\.net/anime/(\d+)", src)
            if m:
                by_anidb[int(m.group(1))] = e
            m = re.search(r"anilist\.co/anime/(\d+)", src)
            if m:
                by_anilist[int(m.group(1))] = e
        for name in [e.get("title")] + (e.get("synonyms") or []):
            if name:
                by_title.setdefault(norm(name), []).append(e)
    print(f"  index: {len(by_anidb)} anidb ids, {len(by_anilist)} anilist ids, "
          f"{len(by_title)} title keys")

    found = []
    for title, year, anidb_id, anilist_id in CASES:
        e = by_anidb.get(anidb_id) or by_anilist.get(anilist_id)
        how = "provider id"
        if not e:
            cands = by_title.get(norm(title)) or []
            cands = [c for c in cands
                     if abs(((c.get("animeSeason") or {}).get("year") or 0) - year) <= 1]
            e, how = (cands[0], "title+year") if cands else (None, "MISS")
        print(f"\n### {title} -> {how}")
        if not e:
            continue
        ids = {p: re.search(r"/(\d+)$", s).group(1)
               for s in e.get("sources", [])
               for p in ("anidb", "anilist", "myanimelist", "kitsu")
               if p in s and re.search(r"/(\d+)$", s)}
        print(f"  ids: {ids}")
        print(f"  score: {e.get('score')}  episodes: {e.get('episodes')} "
              f" studios: {e.get('studios')}")
        print(f"  tags ({len(e.get('tags') or [])}): {', '.join((e.get('tags') or [])[:14])}…")
        rel = e.get("relatedAnime") or []
        print(f"  relatedAnime ({len(rel)}): {rel[:4]}")
        found.append(e)
    return found


ANILIST_URL = "https://graphql.anilist.co"
REL_QUERY = """
query($id:Int){ Media(id:$id, type:ANIME){
  id title { romaji english }
  relations { edges { relationType(version:2)
    node { id idMal seasonYear format title { romaji english } } } }
} }"""


async def phase_b() -> dict:
    print()
    print("=" * 78)
    print("PHASE B — AniList TYPED relations")
    print("=" * 78)
    rel_by_case = {}
    async with httpx.AsyncClient(timeout=30) as client:
        for title, year, _anidb, anilist_id in CASES:
            r = await client.post(ANILIST_URL, json={"query": REL_QUERY,
                                                     "variables": {"id": anilist_id}})
            media = ((r.json().get("data") or {}).get("Media") or {})
            edges = ((media.get("relations") or {}).get("edges")) or []
            rels = []
            for ed in edges:
                node = ed.get("node") or {}
                fmt = node.get("format") or ""
                if fmt in ("MANGA", "NOVEL", "ONE_SHOT"):
                    continue  # library curation cares about the ANIME graph
                t = (node.get("title") or {}).get("english") or \
                    (node.get("title") or {}).get("romaji")
                rels.append({"type": ed.get("relationType"), "title": t,
                             "year": node.get("seasonYear"), "format": fmt})
            rel_by_case[title] = rels
            print(f"\n### {title}")
            for rel in rels:
                print(f"  {rel['type']:<12} {rel['title']}  ({rel['year']}, {rel['format']})")
            await asyncio.sleep(0.7)
    return rel_by_case


def phase_c(rel_by_case: dict) -> None:
    print()
    print("=" * 78)
    print("PHASE C — library match (relations vs the user's own anime cache)")
    print("=" * 78)
    import sqlite3
    db = sqlite3.connect("data/cache/enrichment.db")
    rows = db.execute("SELECT response FROM api_cache WHERE cache_key LIKE 'v2:raw:anime:%'").fetchall()
    lib_titles = {}
    for (v,) in rows:
        try:
            d = json.loads(v)
        except Exception:
            continue
        if d.get("title"):
            lib_titles[norm(d["title"])] = d["title"]
    print(f"  library anime cache: {len(lib_titles)} titles")
    for case, rels in rel_by_case.items():
        hits = [(r, lib_titles.get(norm(r["title"] or ""))) for r in rels]
        print(f"\n### {case}")
        for r, hit in hits:
            mark = f"IN LIBRARY as {hit!r}" if hit else "not in library"
            print(f"  {r['type']:<12} {r['title']}  -> {mark}")


if __name__ == "__main__":
    asyncio.run(phase_a())
    rels = asyncio.run(phase_b())
    phase_c(rels)
