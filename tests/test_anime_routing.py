"""Movies are not anime because their title says "to"; a dead fallback
source does not end a lane.

2026-10-06: every enrichment run since at least 10-02 aborted the movie
lane within a minute. The title guess filed 195 movies as anime on English
words ("Back to the Future"), sent them to AniList + Jikan instead of TMDB,
and with Jikan unreachable each one failed as transient; five in a row
ended the lane while 2,000 movies waited behind them.

    python tests/test_anime_routing.py
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from src.routers.enrichment import _counts_toward_abort  # noqa: E402
from src.services import media_enricher as me  # noqa: E402


def test_the_library_decides_not_the_title():
    for title in ("Back to the Future", "How to Train Your Dragon", "No Hard Feelings",
                  "Road to Perdition", "Talk to Me"):
        assert me._looks_like_anime(title), title   # the guess still misfires ...
        assert not me._is_anime_item("movie", None), title   # ... and no longer decides
    assert not me._is_anime_item("show", None)
    assert not me._is_anime_item("show", "standard")
    assert me._is_anime_item("anime", None)
    assert me._is_anime_item("show", "anime")


def test_a_movie_asks_tmdb():
    is_anime = me._is_anime_item("movie", None)
    assert me._expected_sources_for("movie", is_anime, {"imdb_id": "tt0319061"}) == ["tmdb", "omdb"]
    assert me._expected_sources_for("anime", me._is_anime_item("anime", None), {}) == ["anilist", "jikan"]


def test_both_fetch_paths_use_the_filing():
    src = pathlib.Path(me.__file__).read_text(encoding="utf-8")
    assert src.count("is_anime = _is_anime_item(media_type, sonarr_series_type)") == 2
    assert "sonarr_series_type is None and _looks_like_anime(title)" not in src


def test_only_an_outage_counts_toward_the_abort():
    def tfe(**statuses):
        return me.TransientFetchError({k: {"status": v} for k, v in statuses.items()})
    # Jikan down, AniList answered with a miss: the item stays due, the lane goes on.
    assert not _counts_toward_abort(tfe(anilist="miss", jikan="transient"))
    assert not _counts_toward_abort(tfe(tmdb="not_found", omdb="transient"))
    # Nothing answered at all: an outage, it counts.
    assert _counts_toward_abort(tfe(anilist="transient", jikan="transient"))
    assert _counts_toward_abort(tfe(jikan="transient", anilist="skipped"))
    # TMDB itself answering 429/5xx or not at all: counts, as before.
    assert _counts_toward_abort(me.TMDBTransientError(503, path="/search/movie"))
    assert _counts_toward_abort(me.TMDBTransientError(0, path="/movie/1"))


def test_the_producer_skips_without_counting():
    src = (_ROOT / "src" / "routers" / "enrichment.py").read_text(encoding="utf-8")
    block = src[src.index("except TMDBTransientError as te:"):]
    head = block[:block.index("_transient_streak[pcat] += 1")]
    assert "if not _counts_toward_abort(te):" in head and "return" in head


if __name__ == "__main__":
    fails = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"  PASS  {name}")
            except Exception as e:  # noqa: BLE001
                fails += 1
                print(f"  FAIL  {name}: {type(e).__name__}: {e}")
    sys.exit(1 if fails else 0)
