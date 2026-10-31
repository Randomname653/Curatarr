"""A rebind matches on the strongest key both servers share (2026-10-31).

Seen live against the owner's server, Plex next to a test Jellyfin on the
same anime library (/medianew, TheTVDB plugin, scan still fetching episode
metadata):
- Plex keyed 39,430 episodes ``tvdb-ep``; Jellyfin's real episodes had no
  ids yet, only their numbers below a series that had its TVDB id from the
  folder name. One key per item: no common key, nothing matched.
- Jellyfin listed 58,135 "missing" (virtual) episodes TheTVDB knows of but no
  file holds - and those DID carry tvdb-ep ids, so the matches went onto
  ghosts.
- A Jellyfin without TheTVDB keys its series ``tmdb-tv``, Plex ``tvdb``.

    python tests/test_media_server_keys.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from collections import Counter  # noqa: E402

from src.services.media_server.base import ExternalIds as X, FileRef, MediaItem, canonical_keys  # noqa: E402
from src.services.media_server.id_map import item_rows, key_rank  # noqa: E402

from _checks import check  # noqa: E402


# ── the keys of one item ────────────────────────────────────────────────────
ep = MediaItem(key="p-e1", kind="episode", title="Infiltrators", season_number=1, episode_number=1,
               external_ids=X(tvdb_episode_id=355101),
               files=(FileRef("/medianew/AnimeShows/009-1 (2006)/Season 01/e01.mkv", 356519749),))
ks = canonical_keys(ep, parent_keys=["tvdb:80814", "tmdb-tv:12345"], roots=["/medianew/AnimeShows"])
check("an episode: its TVDB id first, then the chain of every series key, then its file",
      ks == ["tvdb-ep:355101", "tvdb:80814/s01e01", "tmdb-tv:12345/s01e01",
             "file:009-1 (2006)/season 01/e01.mkv|356519749#s01e01",
             "file:009-1 (2006)/season 01/e01.mkv|356519749"])
series = MediaItem(key="p-s", kind="series", title="009-1", external_ids=X(tvdb_id=80814, tmdb_id=12345, imdb_id="tt1"))
check("a series: TVDB, then TMDb and IMDb", canonical_keys(series) == ["tvdb:80814", "tmdb-tv:12345", "imdb:tt1"])
film = MediaItem(key="p-m", kind="movie", title="Akira", external_ids=X(tmdb_id=149),
                 files=(FileRef("/medianew/movies/Akira (1988)/akira.mkv", 99),))
check("a film: TMDb, then its file", canonical_keys(film, roots=["/medianew/movies"]) ==
      ["tmdb:149", "file:akira (1988)/akira.mkv|99"])
check("an id before a chain before a file or a name",
      [key_rank(k) for k in ("tvdb-ep:1", "tvdb:1/s01e01", "file:x|1", "name:x")] == [0, 1, 2, 2])


# ── the owner's case: Plex by ids, Jellyfin's real episodes by number only ──
def walk(series_ids, ep_ids, keys):
    s = MediaItem(key=keys[0], kind="series", title="009-1", library_key="L", external_ids=series_ids)
    se = MediaItem(key=keys[1], kind="season", title="Season 1", library_key="L", parent_key=keys[0], season_number=1)
    eps = [MediaItem(key=f"{keys[2]}{n}", kind="episode", title=f"E{n}", library_key="L", parent_key=keys[1],
                     grandparent_key=keys[0], season_number=1, episode_number=n, external_ids=ep_ids(n),
                     files=(FileRef(f"/medianew/AnimeShows/009-1/Season 01/e{n:02d}.mkv", 1000 + n),))
           for n in (1, 2, 3)]
    return [s, se] + eps


roots = {"L": ("/medianew/AnimeShows",)}
plex = walk(X(tvdb_id=80814), lambda n: X(tvdb_episode_id=355100 + n), ("ps", "pse", "pe"))
jelly = walk(X(tvdb_id=80814), lambda n: X(), ("js", "jse", "je"))
p_rows, j_rows = item_rows(plex, roots), item_rows(jelly, roots)
j_by = {}
for e, k, sid in j_rows:
    j_by.setdefault((e, k), sid)
p_keys = {}
for e, k, sid in p_rows:
    p_keys.setdefault(sid, []).append((e, k))
matched = {}
for sid, ks in p_keys.items():
    hits = sorted(((k, j_by[(e, k)]) for e, k in ks if (e, k) in j_by), key=lambda h: (key_rank(h[0]), h[0]))
    if hits:
        matched[sid] = hits[0]
check("Plex's episodes keyed by TVDB find Jellyfin's id-less ones through the series chain",
      {sid: m[1] for sid, m in matched.items() if sid.startswith("pe")} == {"pe1": "je1", "pe2": "je2", "pe3": "je3"}
      and all(m[0] == f"tvdb:80814/s01e0{sid[-1]}" for sid, m in matched.items() if sid.startswith("pe")))
check("...the series and the season too", matched.get("ps", (None, None))[1] == "js"
      and matched.get("pse", (None, None))[1] == "jse")
check("with one key per item (the old rule) they would not have",
      not ({k for _e, k, sid in item_rows(plex, roots, aliases=False) if sid.startswith("pe")}
           & {k for _e, k, sid in item_rows(jelly, roots, aliases=False) if sid.startswith("je")}))

# a Jellyfin without TheTVDB: series by TMDb, Plex knows the TMDb id as well
plex2 = walk(X(tvdb_id=80814, tmdb_id=12345), lambda n: X(tvdb_episode_id=355100 + n), ("ps", "pse", "pe"))
jelly2 = walk(X(tmdb_id=12345), lambda n: X(), ("js", "jse", "je"))
shared = ({k for _e, k, sid in item_rows(plex2, roots) if sid == "pe2"}
          & {k for _e, k, sid in item_rows(jelly2, roots) if sid == "je2"})
check("a TMDb-only Jellyfin still meets Plex on the TMDb series chain (and the file)",
      "tmdb-tv:12345/s01e02" in shared)

# Jellyfin mid-scan: the file found, not yet placed (no series, no numbers)
unplaced = MediaItem(key="jx", kind="episode", title="Back Arrow", library_key="L",
                     files=(FileRef("/medianew/AnimeShows/009-1/Season 01/e02.mkv", 1002),))
check("an episode the other server has not placed yet still meets it by its file",
      {k for _e, k, sid in item_rows(plex, roots) if sid == "pe2"}
      & {k for _e, k, sid in item_rows([unplaced], roots) if sid == "jx"} == {"file:009-1/season 01/e02.mkv|1002"})

# ── aliases never widen a conflict ──────────────────────────────────────────
ident = Counter(sid for _e, _k, sid in item_rows(jelly, roots, aliases=False))
check("each item has one identity however many keys it files (a double episode keeps two)",
      ident["je1"] == 1 and ident["js"] == 1)
double = MediaItem(key="jd", kind="episode", title="E2-3", library_key="L", parent_key="jse", grandparent_key="js",
                   season_number=1, episode_number=2, episode_number_end=3)
ident2 = Counter(sid for _e, _k, sid in item_rows(jelly[:2] + [double], roots, aliases=False))
check("...a double episode two", ident2["jd"] == 2)

src = (ROOT / "src/services/media_server/rebind.py").read_text(encoding="utf-8")
check("the rebind counts conflicts against identities, and takes the strongest shared key",
      "item_rows(t_items, t_roots, aliases=False)" in src and "key=lambda h: (key_rank(h[0]), h[0])" in src)

# ── Jellyfin's "missing" items are not the library ─────────────────────────
ad = (ROOT / "src/services/media_server/jellyfin_adapter.py").read_text(encoding="utf-8")
check("the library walk, the children of a series and the title search leave missing items out",
      '_NOT_MISSING = {"IsMissing": "false"}' in ad and ad.count("**_NOT_MISSING") == 3)
check("a page may take two minutes while the server scans", "_TIMEOUT = 120.0" in ad)

check.done()
