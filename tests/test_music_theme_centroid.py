"""Music taste ranks by a centre over the THEME phrases (taste engine stage 3).

Measured 2026-11-14 on the owner's history, three time cuts: which newly
heard artists does he come back to (5+ plays)? A centre over the theme
phrases of what he plays found them at AUC 0.57/0.57/0.58; the whole-profile
embedding sat at 0.50/0.48/0.52 and the genre overlap the music ranking fell
back to at 0.45/0.41/0.40. Video showed no such gain, so only music builds one.

The real Chroma store (temp) and facet map, a fake embedder (one axis per
phrase):
- an artist's theme vectors are found by name, also for a Plex artist that
  shares its profile with the same-named history entry;
- the centre leans to what the user plays, against the phrase mean;
- music candidates rank by it, theme-less ones keep the old order behind,
  the "Why?" says so; a film is never ranked by it.

    python tests/test_music_theme_centroid.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores, never the live data/

import asyncio
import json

from src.services import embed_service

AXES = ["night drive", "heartbreak", "club anthem", "protest", "lullaby", "road trip"]


async def _embed(texts, profile=None):
    return [[1.0 if a == t else 0.0 for a in AXES] for t in texts]

embed_service.embed_documents = _embed

import src.services.facet_index as fi  # noqa: E402
import src.vector_store.chromadb_wrapper as cw  # noqa: E402
import src.services.taste_engine as te  # noqa: E402

db = cw.get_chroma_db()


conn = app_db()
# Read once off the loop: write_facets asks for the embedding profile on the
# loop, and only a cached profile keeps that off the database there.
embed_service.get_profile()


def write(doc_id, title, themes, domain="music"):
    return asyncio.run(fi.write_facets(doc_id, title, domain, "", themes))


write("spotify:neon", "Neon Parish", ["night drive", "heartbreak"])
write("spotify:glass", "Glass Choir", ["night drive", "road trip"])
write("spotify:riot", "Riot Saints", ["protest", "club anthem"])
write("spotify:moss", "Moss & Ember", ["lullaby"])
write("radarr:9", "Night Drive Film", ["night drive"], domain="movie")   # never music

vecs = db.theme_vectors_by_title("music", ["Neon Parish", "neon parish!", "Riot Saints", "Unknown Act"])
check("an artist's theme vectors are found by name, any case and punctuation",
      set(vecs) == {"Neon Parish", "neon parish!", "Riot Saints"}, set(vecs))
check("...one unit vector per phrase", vecs["Neon Parish"].shape == (2, len(AXES)))
check("a movie's facets never answer for music",
      db.theme_vectors_by_title("music", ["Night Drive Film"]) == {})
mean = db.theme_mean("music")
check("the phrase mean is over the domain's distinct phrases only",
      mean is not None and abs(float(mean.sum()) - 1.0) < 1e-5 and mean[AXES.index("night drive")] > 0, mean)

reads = []
_real_pv = db._phrase_vectors
db._phrase_vectors = lambda pids, page=2000: (reads.append(len(pids)), _real_pv(pids, page))[1]
db._theme_mean_cache.clear()
m1 = db.theme_mean("music")
m2 = db.theme_mean("music")
check("the phrase mean is read once and kept: the second call reads no phrase vectors "
      "(14 s on the NAS's one Chroma thread, a chat search waited 52 s behind it)",
      len(reads) == 1 and (m1 == m2).all(), reads)
write("spotify:new", "New Act", ["lullaby", "road trip"])
db.theme_mean("music")
check("...and read again once the domain's facets change", len(reads) == 2, reads)
db._phrase_vectors = _real_pv

# ── the centre ───────────────────────────────────────────────────────────────
te.THEME_CENTROID_MIN = 2
theme = te._theme_centroid("music", [(3.0, "Neon Parish"), (1.0, "Glass Choir")])
check("a centre is built over the user's artists that carry themes", theme and theme["titles"] == 2, theme)
c = theme["centroid"]
check("...it leans to what they play: night drive above protest",
      c[AXES.index("night drive")] > c[AXES.index("protest")], c)
check("too few artists with themes: no centre",
      te._theme_centroid("music", [(1.0, "Neon Parish")]) is None)
te.THEME_CENTROID_MIN = 20
s_near = te.theme_score(db.theme_vectors_by_title("music", ["Glass Choir"])["Glass Choir"], theme)
s_far = te.theme_score(db.theme_vectors_by_title("music", ["Riot Saints"])["Riot Saints"], theme)
check("an artist of the same themes scores above one of other themes", s_near > s_far, (s_near, s_far))

# ── the ranking ──────────────────────────────────────────────────────────────
from src.database.models import EncryptedTasteVector, User  # noqa: E402

with conn.get_db_session() as s:
    s.add(User(id=1, plex_user_id="p1", plex_username="u1"))
    s.flush()
    for cat in ("music", "movie"):
        s.add(EncryptedTasteVector(user_id=1, media_category=cat, salt="unencrypted",
                                   encrypted_blob=json.dumps({"version": 0, "embedding": None,
                                                              "theme_centroid": theme})))
    s.commit()

import src.services.recommendations_engine as re_  # noqa: E402

items = [{"title": "Riot Saints", "genres": "punk"}, {"title": "No Themes Yet", "genres": "pop"},
         {"title": "Glass Choir", "genres": "synth"}, {"title": "Moss & Ember", "genres": "folk"}]
ranked = asyncio.run(re_.score_arr_items(1, "music", items, top_n=10))
order = [r["title"] for r in ranked]
check("music ranks by the theme centre: the closest themes first", order[:3] == ["Glass Choir", "Riot Saints", "Moss & Ember"]
      or order[0] == "Glass Choir", order)
check("...an artist without themes keeps the old order, behind the themed ones",
      order[-1] == "No Themes Yet", order)
fit = ranked[0]["_rec_fit"]
check("...the fit says it ranked by themes, with the score", fit["basis"] == "themes" and fit["cosine"] is not None, fit)
why = re_.recommendation_score_factors(lane="library", fit=fit)
check("...and the Why? names it",
      any("themes of what you listen to" in f.get("label", "") for f in why["factors"]), why)

films = [{"title": "Night Drive Film", "genres": "drama"}]
franked = asyncio.run(re_.score_arr_items(1, "movie", films, top_n=10))
check("a film is never ranked by a music theme centre",
      all(r.get("_rec_fit", {}).get("basis") != "themes" for r in franked), franked)

check.done()
