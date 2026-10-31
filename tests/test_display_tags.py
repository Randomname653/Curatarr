"""The tags on a deletion card say what tells the title apart.

2026-10-15: the anime tab showed "Action, Animation, Anime, Drama" on nearly
every card - the arr's first four genres, two of them what the tab already
says. src/services/display_tags.py picks from the enriched profile instead:
genres minus the category's own, rarest in the library first, then up to two
short, uncommon themes.

Runs against a throwaway metadata cache in a temp dir.

    python tests/test_display_tags.py
"""
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import _harness  # noqa: E402,F401  (first: temp stores and .env, never the live data/)
from src.cache.metadata_cache import MetadataCache  # noqa: E402
from src.services import display_tags as dt  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


def labels(tags, kind):
    return [t["label"] for t in tags if t["kind"] == kind]


# ── pick_tags: the rules on their own ────────────────────────────────────────
gc = Counter({"action": 40, "comedy": 45, "mecha": 2, "sci-fi": 20, "drama": 35})
tc = Counter({"female protagonist": 30, "friendship": 4, "sentient arm": 1})
tags = dt.pick_tags("anime", ["Action", "Animation", "Anime", "Mecha", "Science Fiction"],
                    ["Female protagonist", "Sentient arm", "Friendship"], 100, gc, tc)
check("the anime tab drops Anime and Animation", not {"Anime", "Animation"} & set(labels(tags, "genre")))
check("rarest genre first, at most three", labels(tags, "genre") == ["Mecha", "Science Fiction", "Action"])
check("a theme a large share of the category carries is skipped",
      "Female protagonist" not in labels(tags, "theme"))
check("rarest theme first", labels(tags, "theme") == ["Sentient arm", "Friendship"])

film = dt.pick_tags("movie", ["Animation", "Comedy"], [], 100, Counter(), Counter())
check("Animation stays on a film, where it tells something apart", labels(film, "genre") == ["Animation", "Comedy"])

dup = dt.pick_tags("movie", ["Sci-Fi", "Science Fiction", "Sport", "Sports"], [], 0, Counter(), Counter())
check("one genre under two spellings shows once", labels(dup, "genre") == ["Sci-Fi", "Sport"])

long_t = dt.pick_tags("movie", [], ["female protagonist's journey toward self-acceptance",
                                    "a theme of five whole words", "drug heist"], 0, Counter(), Counter())
check("sentences are not chips (length / word cap)", labels(long_t, "theme") == ["Drug heist"])
check("an all-lowercase theme is sentence-cased, mixed case left alone",
      dt._display("drug heist") == "Drug heist" and dt._display("mid-2000s UK scene") == "mid-2000s UK scene")
check("no library stats: source order is kept",
      labels(dt.pick_tags("show", ["Drama", "Mystery"], [], 0, Counter(), Counter()), "genre") == ["Drama", "Mystery"])

# ── profile keys ─────────────────────────────────────────────────────────────
check("film / show / anime profiles are keyed by service and arr id",
      dt.profile_key("anime", "sonarr", "2755") == "enriched:anime:sonarr:2755")
check("music profiles are keyed by the artist's name (first 40 chars)",
      dt.profile_key("music", "plex", "1030193", "Audio Bullys") == "enriched:music:Audio Bullys")
check("no id, no key", dt.profile_key("movie", "radarr", None) is None)

# ── tags_for through a real cache ────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
cache = MetadataCache(cache_path=tmp / "enrichment.db")
lib = {
    "1": (["Action", "Comedy"], ["school setting"]),
    "2": (["Action", "Drama"], ["school setting"]),
    "3": (["Action", "Romance"], ["school setting"]),
    "4": (["Comedy", "Mecha", "Action"], ["sentient arm", "school setting"]),
}
for mid, (g, t) in lib.items():
    cache.set_cache(f"enriched:anime:sonarr:{mid}", {"genres": g, "themes": t}, days=30)
cache.set_cache("enriched:music:Some Band", {"genres": ["Trip Hop"], "themes": ["Late-night grooves"]}, days=30)
dt._stats.clear()
items = [
    {"category": "anime", "service": "sonarr", "media_id": "4", "title": "Mecha Show",
     "genres": "Action, Animation, Anime, Science Fiction"},
    {"category": "anime", "service": "sonarr", "arr_id": 4, "title": "Fresh run"},
    {"category": "anime", "service": "sonarr", "media_id": "99", "title": "Unenriched",
     "genres": "Action, Animation, Anime, Drama"},
    {"category": "music", "service": "plex", "media_id": "5", "title": "Some Band", "genres": ""},
]
out = dt.tags_for(items, cache=cache)
check("the profile's genres win over the arr's list, rarest first",
      labels(out[0], "genre") == ["Mecha", "Comedy", "Action"])
check("a theme every title in the category carries is not shown",
      labels(out[0], "theme") == ["Sentient arm"])
check("a fresh run's dicts (arr_id instead of media_id) find the same profile",
      out[1] == out[0])
check("no profile: the arr's genres, minus Anime / Animation",
      labels(out[2], "genre") == ["Drama", "Action"])
check("music cards get tags from the artist profile",
      labels(out[3], "genre") == ["Trip Hop"] and labels(out[3], "theme") == ["Late-night grooves"])

props = [dict(items[0])]
dt.attach_tags(props)
check("attach_tags adds tags in place (own cache handle)", "tags" in props[0])
broken = [{"category": "anime", "service": "sonarr", "media_id": object()}]
_real = dt.tags_for
dt.tags_for = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("cache gone"))
try:
    dt.attach_tags(broken)
finally:
    dt.tags_for = _real
check("a failure leaves the list without tags instead of failing it", "tags" not in broken[0])
cache.close()

# ── wiring ───────────────────────────────────────────────────────────────────
router = (ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8")
check("both deletion-list paths attach tags off the event loop",
      router.count("await asyncio.to_thread(attach_tags,") == 2)
js = (ROOT / "frontend/js/deletions.js").read_text(encoding="utf-8")
check("the card renders the server's tags, themes in their own style",
      "Array.isArray(p.tags)" in js and "badge theme badge-sm" in js)
css = (ROOT / "frontend/css/app.css").read_text(encoding="utf-8")
check("the theme chip style uses tokens only", ".badge.theme {" in css
      and "var(--text3)" in css.split(".badge.theme {")[1].split("}")[0]
      and "#" not in css.split(".badge.theme {")[1].split("}")[0])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
