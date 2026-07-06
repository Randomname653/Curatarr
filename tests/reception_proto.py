"""
Reception-layer prototype — TEST BEFORE BUILD (stdlib runner, no pytest).

The judge goes nearly blind on obscure titles (5.8 rating, 12 votes, no
Wikipedia article). Before wiring a reception layer into the enricher, this
answers three questions on REAL titles from the library:

  A. ID RESOLUTION — do obscure anime resolve to AniList/MAL ids?
       path 1: anime_mapping (tvdb -> anidb -> anilist), then AniList idMal
       path 2: AniList GraphQL title search (fallback when mapping misses)
  B. COVERAGE — what do AniList / Jikan (MAL) / TMDB reviews actually return
       for an OBSCURE title vs a KNOWN control (counts, scores, text volume)?
  C. CONDENSATION (--condense) — can the summarizer compress the fetched
       reviews into a grounded RECEPTION block without inventing facts?

Run from repo root:
    python tests/reception_proto.py             # phases A + B (network only)
    python tests/reception_proto.py --condense  # + phase C (needs ollama)
"""
import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

import httpx

from src.config import settings

ANILIST_URL = "https://graphql.anilist.co"
JIKAN_URL = "https://api.jikan.moe/v4"

# (title, tvdb_id, year, note) — tvdb None forces the title-search fallback.
ANIME_CASES = [
    ("Please Put Them On, Takamine-san", 276880, 2025, "obscure, debated today"),
    ("Please Excuse My Younger Brothers", 307585, 2026, "very obscure, brand new"),
    ("Miss Kobayashi's Dragon Maid", None, 2017, "known control"),
]
# (title, tmdb_id, note)
MOVIE_CASES = [
    ("Dear Ms.: A Revolution in Print", 1465528, "obscure (votes=None in cache)"),
    ("Inception", 27205, "known control"),
]

ANILIST_MEDIA_FIELDS = """
  id idMal seasonYear averageScore meanScore popularity favourites
  title { romaji english }
  rankings { rank type allTime context }
  tags { name rank }
  stats { scoreDistribution { score amount } }
  reviews(sort: RATING_DESC, perPage: 4) {
    nodes { summary rating ratingAmount score body }
  }
"""


async def anilist_by_id(client: httpx.AsyncClient, anilist_id: int) -> dict:
    q = "query($id:Int){ Media(id:$id, type:ANIME){ %s } }" % ANILIST_MEDIA_FIELDS
    r = await client.post(ANILIST_URL, json={"query": q, "variables": {"id": anilist_id}})
    r.raise_for_status()
    return (r.json().get("data") or {}).get("Media") or {}


async def anilist_search(client: httpx.AsyncClient, title: str, year: int) -> dict:
    q = ("query($search:String){ Page(perPage:5){ media(search:$search, type:ANIME)"
         "{ %s } } }" % ANILIST_MEDIA_FIELDS)
    r = await client.post(ANILIST_URL, json={"query": q, "variables": {"search": title}})
    r.raise_for_status()
    media = ((r.json().get("data") or {}).get("Page") or {}).get("media") or []
    # best match: closest year (the entity-resolution lesson — never title-only)
    scored = [(abs((m.get("seasonYear") or 0) - year), m) for m in media]
    scored.sort(key=lambda x: x[0])
    return scored[0][1] if scored and scored[0][0] <= 1 else {}


async def jikan(client: httpx.AsyncClient, path: str) -> dict:
    r = await client.get(f"{JIKAN_URL}{path}")
    if r.status_code != 200:
        return {"_http": r.status_code}
    return r.json()


async def tmdb_reviews(client: httpx.AsyncClient, tmdb_id: int) -> list[dict]:
    if not settings.TMDB_API_KEY:
        return []
    r = await client.get(
        f"https://api.themoviedb.org/3/movie/{tmdb_id}/reviews",
        params={"api_key": settings.TMDB_API_KEY, "language": "en-US"},
    )
    if r.status_code != 200:
        return []
    return r.json().get("results") or []


def _clip(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s[:n] + ("…" if len(s) > n else "")


async def resolve_anime(client: httpx.AsyncClient, title: str,
                        tvdb_id: int | None, year: int) -> tuple[dict, str]:
    """Return (anilist media dict, how-resolved label)."""
    if tvdb_id:
        from src.services.anime_mapping import get_anime_mapping
        mapping = await get_anime_mapping()
        ids = mapping.lookup_tvdb(tvdb_id)
        if ids.get("anilist_id"):
            m = await anilist_by_id(client, ids["anilist_id"])
            if m:
                return m, f"mapping tvdb {tvdb_id} -> anilist {ids['anilist_id']}"
    m = await anilist_search(client, title, year)
    return m, ("title-search fallback" if m else "UNRESOLVED")


async def phase_ab() -> dict:
    """Resolution + coverage. Returns raw material keyed by title for phase C."""
    material: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=30) as client:
        print("=" * 78)
        print("PHASE A+B — ANIME (AniList + Jikan/MAL)")
        print("=" * 78)
        for title, tvdb_id, year, note in ANIME_CASES:
            print(f"\n### {title}  ({note})")
            m, how = await resolve_anime(client, title, tvdb_id, year)
            print(f"  resolution: {how}")
            if not m:
                continue
            mal_id = m.get("idMal")
            print(f"  anilist id={m.get('id')} idMal={mal_id} "
                  f"avgScore={m.get('averageScore')} mean={m.get('meanScore')} "
                  f"popularity={m.get('popularity')} favourites={m.get('favourites')}")
            top_tags = sorted(m.get("tags") or [], key=lambda t: -(t.get("rank") or 0))[:6]
            print(f"  top tags: {', '.join(f'{t['name']}({t['rank']}%)' for t in top_tags)}")
            al_reviews = (m.get("reviews") or {}).get("nodes") or []
            print(f"  anilist reviews: {len(al_reviews)}")
            for rv in al_reviews[:2]:
                print(f"    - [{rv.get('score')}/100, {rv.get('rating')}/{rv.get('ratingAmount')} helpful] "
                      f"{_clip(rv.get('summary'), 110)}")

            mat = {"title": title, "anilist": m, "jikan_reviews": [], "jikan_stats": {}}
            if mal_id:
                core = await jikan(client, f"/anime/{mal_id}")
                d = core.get("data") or {}
                print(f"  MAL: score={d.get('score')} scored_by={d.get('scored_by')} "
                      f"rank=#{d.get('rank')} members={d.get('members')}")
                await asyncio.sleep(1.2)  # jikan politeness (60/min)
                revs = await jikan(client, f"/anime/{mal_id}/reviews")
                items = revs.get("data") or []
                print(f"  MAL reviews: {len(items)}"
                      + (f"  (HTTP {revs['_http']})" if "_http" in revs else ""))
                for rv in items[:3]:
                    tags = ",".join(rv.get("tags") or [])
                    print(f"    - [{rv.get('score')}/10 {tags}] {_clip(rv.get('review'), 130)}")
                mat["jikan_reviews"] = items[:4]
                mat["jikan_stats"] = {k: d.get(k) for k in
                                      ("score", "scored_by", "rank", "members", "synopsis")}
                await asyncio.sleep(1.2)
            material[title] = mat

        print()
        print("=" * 78)
        print("PHASE B — MOVIES (TMDB reviews)")
        print("=" * 78)
        for title, tmdb_id, note in MOVIE_CASES:
            revs = await tmdb_reviews(client, tmdb_id)
            print(f"\n### {title}  ({note})")
            print(f"  tmdb reviews: {len(revs)}")
            for rv in revs[:2]:
                rating = (rv.get("author_details") or {}).get("rating")
                print(f"    - [{rating}/10 by {rv.get('author')}] {_clip(rv.get('content'), 130)}")
    return material


CONDENSE_SYS = (
    "You write a RECEPTION summary for a media curator's evidence file. "
    "Use ONLY the material given — never your own knowledge of the title. "
    "If the material is thin, say so plainly instead of padding."
)


def build_condense_prompt(mat: dict) -> str:
    m = mat["anilist"]
    lines = [f"TITLE: {mat['title']}", ""]
    lines.append(f"AniList: average score {m.get('averageScore')}/100, "
                 f"popularity {m.get('popularity')}, favourites {m.get('favourites')}")
    tags = sorted(m.get("tags") or [], key=lambda t: -(t.get("rank") or 0))[:8]
    lines.append("Community tags: " + ", ".join(f"{t['name']} ({t['rank']}%)" for t in tags))
    js = mat.get("jikan_stats") or {}
    if js.get("score"):
        lines.append(f"MyAnimeList: {js['score']}/10 from {js.get('scored_by')} users, "
                     f"rank #{js.get('rank')}, {js.get('members')} members")
    for rv in (m.get("reviews") or {}).get("nodes") or []:
        lines.append(f"\nAniList review ({rv.get('score')}/100): "
                     f"{_clip(rv.get('summary'), 200)} {_clip(rv.get('body'), 700)}")
    for rv in mat.get("jikan_reviews") or []:
        tags_s = ",".join(rv.get("tags") or [])
        lines.append(f"\nMAL review ({rv.get('score')}/10, {tags_s}): "
                     f"{_clip(rv.get('review'), 900)}")
    lines.append(
        "\nTASK: Write RECEPTION — 3 to 5 sentences: the community verdict, what "
        "viewers praise, what they slam, and whether opinion is split. Plain "
        "prose, no bullet points, no scores invented beyond those above.")
    return "\n".join(lines)


def hallucination_sniff(output: str, source: str) -> list[str]:
    """Capitalized tokens in the output that never occur in the source —
    a crude tripwire for invented names/claims, not a verdict."""
    words = set(re.findall(r"\b[A-Z][a-zA-Z'\-]{3,}\b", output))
    src_lower = source.lower()
    return sorted(w for w in words if w.lower() not in src_lower)


async def phase_c(material: dict) -> None:
    print()
    print("=" * 78)
    print("PHASE C — CONDENSATION (summarizer -> RECEPTION block)")
    print("=" * 78)
    async with httpx.AsyncClient(timeout=180) as client:
        for title, mat in material.items():
            if not (mat.get("jikan_reviews") or (mat["anilist"].get("reviews") or {}).get("nodes")):
                print(f"\n### {title}: no review material fetched — skipping")
                continue
            prompt = build_condense_prompt(mat)
            print(f"\n### {title}  (input material: {len(prompt)} chars)")
            for model in (settings.SUMMARIZER_MODEL, settings.BASE_SUMMARIZER_MODEL):
                try:
                    r = await client.post(
                        f"{settings.effective_ollama}/api/chat",
                        json={"model": model,
                              "messages": [{"role": "system", "content": CONDENSE_SYS},
                                           {"role": "user", "content": prompt}],
                              "stream": False,
                              "options": {"temperature": 0.2, "num_predict": 400}},
                    )
                    if r.status_code != 200:
                        continue
                    out = (r.json().get("message") or {}).get("content", "").strip()
                    if not out:
                        continue
                    print(f"  [{model}]")
                    print("  " + out.replace("\n", "\n  "))
                    sus = hallucination_sniff(out, prompt)
                    print(f"  grounding sniff (capitalized tokens not in source): "
                          f"{sus if sus else 'clean'}")
                    break
                except Exception as e:
                    print(f"  [{model}] failed: {e}")


if __name__ == "__main__":
    mat = asyncio.run(phase_ab())
    if "--condense" in sys.argv:
        asyncio.run(phase_c(mat))
    else:
        print("\n(phase C skipped — rerun with --condense to test the summarizer step)")
