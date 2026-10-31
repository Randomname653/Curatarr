"""Recommendation cards get a "Why?" like deletion cards.

A rec card showed only the curator's "% match" and its pitch: nothing said
why a title was picked. Now each recommendation carries ``score_factors`` —
the deterministic signals it was picked on (its rank in the taste-ranked
library pool and what that rank was based on, the genres it shares with the
user's, a value the user taught Curatarr, TMDB trending, the listening graph,
an exploration pick, the lane, the curator's confidence) — stored with the
cached rec and rendered through the Deletions card's Why? button.

  * the factors are built from what the pipeline actually had for that title
  * nothing from the arr item itself (LAN address, path) reaches them
  * a cached rec keeps its breakdown; one cached before it has none (never
    a made-up one)
  * recs.js renders the button through deletions.js's _whyButton

    python tests/test_rec_why.py
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.connection import get_db_session  # noqa: E402
from src.database.models import (Base, CachedRecommendation,  # noqa: E402
                                 EncryptedTasteVector, TasteVectorEntry, User)
import src.services.episodic_memory as epm  # noqa: E402
import src.services.library_memory as lm  # noqa: E402
import src.services.media_enricher as me  # noqa: E402
import src.services.recommendations_engine as eng  # noqa: E402
import src.services.scheduler as sched  # noqa: E402
import src.routers.recommendations as recs_router  # noqa: E402

Base.metadata.create_all(conn_mod.engine)

from _checks import check  # noqa: E402


def _by_key(sf):
    return {f["key"]: f for f in (sf or {}).get("factors") or []}


# ── the builder: factors are the inputs, nothing more ───────────────────────
print("recommendation_score_factors")
sf = eng.recommendation_score_factors(
    lane="library", confidence=0.82,
    fit={"basis": "embedding", "rank": 3, "pool": 41, "cosine": 0.7123,
         "genres": ["Drama", "Thriller"], "disliked": False},
    values=["Partner loves Studio Ghibli films"])
f = _by_key(sf)
check("shape the Why? loader accepts: a dict with a factors list",
      recs_router._load_score_factors(json.dumps(sf)) == json.loads(json.dumps(sf)))
check("taste rank and pool are the fit's, on the embedding basis",
      f["taste"]["rank"] == 3 and f["taste"]["of"] == 41 and f["taste"]["cosine"] == 0.7123
      and "#3 of 41" in f["taste"]["label"] and "watch history" in f["taste"]["label"])
check("shared genres named", f["genres"]["label"].endswith("Drama, Thriller"))
check("the user's own value quoted", "Studio Ghibli" in f["values"]["label"])
check("curator confidence as a percentage", f["confidence"]["value"] == 82)
check("no invented points on any factor", all("points" not in x for x in sf["factors"]))
check("no trending / exploration / dislike when the inputs say none",
      not {"trending", "exploration", "disliked", "neighbour"} & set(f))
bare = _by_key(eng.recommendation_score_factors(lane="discovery", confidence="high"))
check("a discovery rec with nothing else: just its lane, no fabricated taste rank "
      "and no confidence from a non-number",
      set(bare) == {"lane"})

# ── end to end: generate_recommendations on a temp DB ───────────────────────
print("generate_recommendations")
with get_db_session() as db:
    db.add(User(id=1, plex_user_id="p1", plex_username="member"))
    db.flush()
    db.add(TasteVectorEntry(user_id=1, genre_affinity=json.dumps(
        {"movie": {"genre_affinity": {"Drama": 1.0, "Thriller": 0.5},
                   "top_titles": ["Seed Film"]}})))
    db.add(EncryptedTasteVector(user_id=1, media_category="movie", salt="0",
                                encrypted_blob=json.dumps({"embedding": [1.0, 0.0]})))
    db.commit()

LAN = "http://192.168.1.5:7878/movie/1"
POOL = [
    {"title": "Far Film", "genres": "Horror", "plex_rating_key": "k-far",
     "arr_url": LAN, "path": "/mnt/media/movies/Far Film"},
    {"title": "Near Film", "genres": "Drama, Comedy", "plex_rating_key": "k-near",
     "arr_url": LAN, "path": "/mnt/media/movies/Near Film"},
    {"title": "Genre Film", "genres": "Thriller", "arr_url": LAN,
     "path": "/mnt/media/movies/Genre Film"},
]
eng._chroma_get_by_ids = lambda ids: {"k-near": {"embedding": [0.9, 0.1]},
                                      "k-far": {"embedding": [0.0, 1.0]}}


async def _vd(*a, **kw):
    return None


async def _cons(user_id, profile, media_category=None, top_k=2):
    if profile.startswith("Genre Film"):
        return [{"content": "Keeps every Fincher thriller", "overlap": ["thriller"],
                 "media_category": "movie"}]
    return []


async def _trending(cat):
    return ["Hot New Film (2026)"]


PICKS = {"library": [{"title": "Genre Film", "reason": "r", "confidence": 0.6},
                     {"title": "Near Film", "reason": "r", "confidence": 0.9}],
         "discovery": [{"title": "Hot New Film", "reason": "r", "confidence": 0.7},
                       {"title": "Odd Pick", "reason": "Exploration pick: something new",
                        "confidence": 0.4}]}


async def _llm(prompt, max_tokens=800, **kw):
    return json.dumps(PICKS["library" if "LIBRARY (unwatched)" in prompt else "discovery"])


async def _owned(category=None):
    return {"titles": set(), "tmdb_ids": set(), "tvdb_ids": set()}


me.ensure_verified_data = _vd
epm.retrieve_considerations = _cons
eng._call_llm = _llm
eng._get_trending = _trending
lm.owned_index = _owned

scored = asyncio.run(eng.score_arr_items(1, "movie", POOL, top_n=50))
check("score_arr_items still ranks by embedding first, genre fallback after",
      [i["title"] for i in scored] == ["Near Film", "Far Film", "Genre Film"])
check("...and leaves the shared arr dicts untouched", all("_rec_fit" not in i for i in POOL))

lib = {r["title"]: r for r in asyncio.run(eng.generate_recommendations(
    1, category="movie", arr_library=scored))}
near, genre = _by_key(lib["Near Film"]["score_factors"]), _by_key(lib["Genre Film"]["score_factors"])
check("library pick: its rank in the pool it was ranked in, on the embedding basis",
      near["taste"]["rank"] == 1 and near["taste"]["of"] == 3
      and "watch history" in near["taste"]["label"] and near["taste"]["cosine"] > 0.9)
check("...a genre-only candidate says so, with the genres it matched",
      genre["taste"]["rank"] == 3 and "genres you watch" in genre["taste"]["label"]
      and "cosine" not in genre["taste"] and genre["genres"]["label"].endswith("Thriller"))
check("...Near Film shares Drama only, not Comedy (no affinity)",
      near["genres"]["label"].endswith(": Drama"))
check("...the value shown beside the candidate is the one quoted",
      "Fincher" in genre["values"]["label"] and "values" not in near)
check("...the curator's confidence is the model's own number",
      near["confidence"]["value"] == 90 and genre["confidence"]["value"] == 60)
blob = json.dumps([r["score_factors"] for r in lib.values()])
check("nothing from the arr item (LAN address, path) reaches the breakdown",
      "192.168" not in blob and "/mnt/" not in blob)

disc = {r["title"]: r for r in asyncio.run(eng.generate_recommendations(1, category="movie"))}
hot, odd = _by_key(disc["Hot New Film"]["score_factors"]), _by_key(disc["Odd Pick"]["score_factors"])
check("discovery: a pick on the TMDB trending list says so", "trending" in hot and "trending" not in odd)
check("...an exploration pick says so", "exploration" in odd and "exploration" not in hot)
check("...no library rank for a title that was never in the pool", "taste" not in hot)

# ── cached recs: the breakdown survives; legacy rows get none ───────────────
print("cache")
sched._write_rec_lane(1, "movie", "library", list(lib.values()))
with get_db_session() as db:
    db.add(CachedRecommendation(user_id=1, category="movie", lane="discovery",
                                title="Old Rec", reason="cached long ago", confidence=0.7))
    db.commit()
out = asyncio.run(recs_router._get_recommendations_impl(
    "movie", 8, False, "cache", None, SimpleNamespace(id=1, is_admin=False)))
cached = {r["title"]: r for r in out["recommendations"]}
check("a cached rec shows the same breakdown as the fresh one",
      cached["Near Film"]["score_factors"] == json.loads(json.dumps(lib["Near Film"]["score_factors"])))
check("a rec cached before the breakdown existed has none - not a made-up one",
      "score_factors" in cached["Old Rec"] and cached["Old Rec"]["score_factors"] is None)

# ── recs.js: the Deletions card's button, same disclosure ───────────────────
print("recs.js")
_JS = ROOT / "frontend" / "js"
recs_js = (_JS / "recs.js").read_text(encoding="utf-8")
del_js = (_JS / "deletions.js").read_text(encoding="utf-8")


def _fn(src, name):
    m = re.search(r"^(?:export )?(?:async )?function %s\(" % re.escape(name), src, re.M)
    assert m, f"{name}() not found"
    end = re.compile(r"^\}$", re.M).search(src, m.start())
    return src[m.start():end.end()].removeprefix("export ")


check("recs.js imports the shared Why? button from deletions.js",
      re.search(r"import \{[^}]*\b_whyButton\b[^}]*\} from './deletions\.js'", recs_js) is not None)
check("the rec card renders it via _recWhy -> _whyButton",
      "_recWhy(rec" in _fn(recs_js, "_recCard") and "_whyButton(" in _fn(recs_js, "_recWhy"))

nav_js = (_JS / "nav.js").read_text(encoding="utf-8")
check("the lane is the #recs in-view tab: registered with nav.js, written by setRecLane",
      re.search(r"registerTabRoute\('recs', \{open: openRecLane,", nav_js) is not None
      and "tabToHash('recs', lane)" in _fn(recs_js, "setRecLane"))
check("...a load started on another lane is abandoned, not rendered over the new one",
      "(state.currentRecsLane || 'all') !== laneFilter) return;" in _fn(recs_js, "loadRecs"))

node = shutil.which("node")
if node:
    js = r"""
const esc = s => String(s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const act = (name, ...args) => `data-action="${name}"` + (args.length ? ` data-args='${JSON.stringify(args)}'` : '');
const EL = '$el';
""" + _fn(del_js, "_whyButton") + "\n" + _fn(recs_js, "_recWhy") + """
const rec = %s;
const out = {fresh: _recWhy(rec, 'library-0'), legacy: _recWhy({title: 'Old', score_factors: null}, 'discovery-0')};
console.log(JSON.stringify(out));
""" % json.dumps(lib["Near Film"])
    r = subprocess.run([node, "-e", js], capture_output=True, text=True, encoding="utf-8")
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    if r.returncode:
        print(r.stderr[:800])
    fresh = res.get("fresh") or {}
    btn, panel = fresh.get("btn", ""), fresh.get("panel", "")
    check("a fresh rec gets a real <button> with aria-expanded=false and toggleWhy",
          btn.startswith('<button type="button" class="btn-why" aria-expanded="false"')
          and 'data-action="toggleWhy"' in btn)
    m = re.search(r'aria-controls="([^"]+)"', btn)
    check("...controlling a hidden why-panel (the Escape listener's target)",
          m is not None and f'id="{m.group(1)}"' in panel and "why-panel" in panel and " hidden>" in panel)
    check("...listing every factor label", all(esc_l in panel for esc_l in (
        "#1 of 3 unwatched titles", "The curator rated the fit 90%")))
    check("a legacy rec gets no button and no panel",
          res.get("legacy") == {"btn": "", "panel": ""})
else:
    print("  (node not installed - render checked statically)")
    check("legacy rec: no button without factors", "if (!f.length) return {btn: '', panel: ''};"
          in _fn(recs_js, "_recWhy"))

shutil.rmtree(_TMP, ignore_errors=True)
check.done()
