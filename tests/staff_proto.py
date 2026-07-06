"""
Staff prototype — TEST BEFORE BUILD (stdlib runner, no pytest).

Anime staff (Original Creator / Series Composition / Music / Character
Design) is missing from the evidence because the OMDb path needs an imdb_id
anime rarely have. AniList carries staff with roles on the SAME call the
reception/franchise layer already makes. Before wiring: fetch staff for 20+
REAL anime from the user's library cache and measure coverage + role quality.

Run from repo root:  python tests/staff_proto.py
"""
import asyncio
import json
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import httpx

ANILIST_URL = "https://graphql.anilist.co"
QUERY = """
query($id:Int){ Media(id:$id, type:ANIME){
  id title { romaji english }
  staff(perPage: 12, sort: RELEVANCE) {
    edges { role node { name { full } } } }
} }"""

# the curation-relevant creative core; directors already have their own line
KEEP = re.compile(
    r"original creator|original story|series composition|script|screenplay"
    r"|music|character design|chief director", re.I)


def pick_titles(n: int = 24) -> list[tuple[str, int, int]]:
    """(title, year, anilist_id) from the user's own anime cache — newest and
    oldest mixed so the sample isn't one era."""
    db = sqlite3.connect("data/cache/enrichment.db")
    rows = db.execute(
        "SELECT response FROM api_cache WHERE cache_key LIKE 'v2:raw:anime:%'"
    ).fetchall()
    items = []
    for (v,) in rows:
        try:
            d = json.loads(v)
        except Exception:
            continue
        if d.get("anilist_id") and d.get("title"):
            items.append((d["title"], d.get("year") or 0, int(d["anilist_id"])))
    # dedupe by anilist id, sort by year, take an even spread
    seen, uniq = set(), []
    for it in sorted(items, key=lambda x: x[1]):
        if it[2] in seen:
            continue
        seen.add(it[2])
        uniq.append(it)
    if len(uniq) <= n:
        return uniq
    step = len(uniq) / n
    return [uniq[int(i * step)] for i in range(n)]


def extract_staff(media: dict) -> list[str]:
    out, seen = [], set()
    for ed in ((media.get("staff") or {}).get("edges")) or []:
        role = (ed.get("role") or "").strip()
        name = ((ed.get("node") or {}).get("name") or {}).get("full")
        if not name or not KEEP.search(role):
            continue
        # strip episode-scoped suffixes: "Script (eps 3, 7)" -> "Script"
        role = re.sub(r"\s*\(.*?\)\s*$", "", role)
        key = (role.lower(), name)
        if key in seen:
            continue
        seen.add(key)
        out.append(f"{role}: {name}")
    return out[:6]


async def main():
    cases = pick_titles()
    print(f"Testing staff extraction on {len(cases)} real library anime\n")
    hits = full = 0
    async with httpx.AsyncClient(timeout=30) as client:
        for title, year, al_id in cases:
            r = await client.post(ANILIST_URL, json={"query": QUERY,
                                                     "variables": {"id": al_id}})
            media = ((r.json().get("data") or {}).get("Media")) or {}
            staff = extract_staff(media)
            if staff:
                hits += 1
                if len(staff) >= 3:
                    full += 1
            print(f"### {title} ({year})")
            print(f"    {'; '.join(staff) if staff else '-- no creative-core staff --'}")
            await asyncio.sleep(0.75)
    print(f"\ncoverage: {hits}/{len(cases)} with staff, {full}/{len(cases)} with 3+ roles")


if __name__ == "__main__":
    asyncio.run(main())
