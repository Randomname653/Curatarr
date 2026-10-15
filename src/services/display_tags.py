"""The tags a card shows: what tells a title apart, not what its category says.

The deletion cards used to print the first four genres the arr sent. Those
lists are short and generic, and in the anime tab every card began with
"Animation" and "Anime" - the tab already says that. The enriched profile
carries better ones (AniList's genres for anime: Ecchi, Mecha, Slice of Life;
TMDB's for films) plus short themes ("Social anxiety", "Drug heist").

``tags_for`` picks, per title:

* up to three genres from the profile (the arr's list only when there is no
  profile), minus the ones the category implies, the rarest in the owner's
  library first: "Mecha" says more in the anime tab than "Action", which
  42 % of it carries;
* up to two themes, rarest first, skipping long sentences and themes that a
  sizeable share of the category carries.

Rarity is measured on the enriched profiles of the same category and kept
for a few hours; it moves with the library, not by the minute.
"""
import json
import logging
import threading
import time
from collections import Counter
from typing import Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

MAX_GENRES = 3
MAX_THEMES = 2
THEME_MAX_CHARS = 32
THEME_MAX_WORDS = 4
# A theme that this share of the category carries ("female protagonist" on
# 353 of 3,309 anime) describes the library, not the title.
THEME_COMMON_SHARE = 0.05
_STATS_TTL_S = 6 * 3600

# What the category already says. "Animation" stays on a film or a show,
# where it does tell something apart.
IMPLIED: Dict[str, set] = {"anime": {"anime", "animation"}}
# One genre under two spellings across TMDB / AniList / the arrs.
_ALIASES = {"science fiction": "sci-fi", "sport": "sports", "mini-series": "miniseries"}

_stats: Dict[str, Tuple[float, int, Counter, Counter]] = {}
_stats_lock = threading.Lock()


def _norm(label: str) -> str:
    k = " ".join(str(label).lower().split())
    return _ALIASES.get(k, k)


def _as_list(raw) -> List[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [p for p in raw.split(",")]
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if isinstance(x, str) and str(x).strip()]


def _display(label: str) -> str:
    """Sentence case for an all-lowercase theme; anything else as written."""
    return label[:1].upper() + label[1:] if label == label.lower() else label


def profile_key(category: str, service: str, media_id, title: str = "") -> Optional[str]:
    """The enriched-profile cache key (unversioned; the cache adds the
    version). Music profiles are keyed by artist name."""
    if category == "music":
        return f"enriched:music:{title[:40]}" if title else None
    if not (category and service and media_id):
        return None
    return f"enriched:{category}:{service}:{media_id}"


def _load_stats(category: str, cache) -> Tuple[int, Counter, Counter]:
    """(profiles, genre counts, theme counts) over the category's enriched
    profiles. Each profile counts a label once."""
    from src.cache.metadata_cache import _CACHE_VERSION
    lo = f"{_CACHE_VERSION}:enriched:{category}:"
    hi = lo[:-1] + chr(ord(":") + 1)
    genres, themes, n = Counter(), Counter(), 0
    cur = cache.conn.cursor()
    try:
        cur.execute(
            "SELECT json_extract(response, '$.genres'), json_extract(response, '$.themes') "
            "FROM api_cache WHERE cache_key >= ? AND cache_key < ?", (lo, hi))
        for g, t in cur:
            n += 1
            genres.update({_norm(x) for x in _as_list(g)})
            themes.update({_norm(x) for x in _as_list(t)})
    finally:
        cur.close()
    return n, genres, themes


def library_stats(category: str, cache, *, now: Optional[float] = None):
    now = time.time() if now is None else now
    with _stats_lock:
        hit = _stats.get(category)
        if hit and now - hit[0] < _STATS_TTL_S:
            return hit[1:]
    try:
        n, g, t = _load_stats(category, cache)
    except Exception as e:   # a locked or missing cache: no rarity, plain order
        logger.warning("[tags] library stats for %s unavailable: %s", category, e)
        return 0, Counter(), Counter()
    with _stats_lock:
        _stats[category] = (now, n, g, t)
    return n, g, t


def pick_tags(category: str, genres: Iterable[str], themes: Iterable[str],
              n: int, genre_counts: Counter, theme_counts: Counter) -> List[dict]:
    """The card's tags from one title's genres and themes and the library's
    counts. Ties keep the source's order (sorted() is stable)."""
    implied = IMPLIED.get(category, set())
    seen, gs = set(), []
    for g in genres:
        k = _norm(g)
        if k in implied or k in seen:
            continue
        seen.add(k)
        gs.append(g)
    gs = sorted(gs, key=lambda g: genre_counts.get(_norm(g), 0))[:MAX_GENRES]

    cap = max(2, THEME_COMMON_SHARE * n) if n else None
    ts = []
    for t in themes:
        k = _norm(t)
        if (k in seen or len(t) > THEME_MAX_CHARS or len(t.split()) > THEME_MAX_WORDS
                or (cap is not None and theme_counts.get(k, 0) > cap)):
            continue
        seen.add(k)
        ts.append(t)
    ts = sorted(ts, key=lambda t: theme_counts.get(_norm(t), 0))[:MAX_THEMES]
    return ([{"label": g, "kind": "genre"} for g in gs]
            + [{"label": _display(t), "kind": "theme"} for t in ts])


def tags_for(items: List[dict], cache=None) -> Dict[int, List[dict]]:
    """{index: tags} for proposal-like dicts (category, service, media_id or
    arr_id, title, genres). Blocking: call it in a thread from a coroutine."""
    from src.cache.metadata_cache import MetadataCache
    own = cache is None
    cache = cache or MetadataCache()
    try:
        keys = {i: profile_key(it.get("category") or "", it.get("service") or "",
                               it.get("media_id") or it.get("arr_id"), it.get("title") or "")
                for i, it in enumerate(items)}
        profiles = cache.get_cache_many([k for k in keys.values() if k])
        out = {}
        for i, it in enumerate(items):
            cat = it.get("category") or ""
            prof = (profiles.get(keys[i]) or {}).get("response") or {} if keys[i] else {}
            genres = _as_list(prof.get("genres")) or _as_list(it.get("genres") or "")
            themes = _as_list(prof.get("themes"))
            n, gc, tc = library_stats(cat, cache) if cat else (0, Counter(), Counter())
            out[i] = pick_tags(cat, genres, themes, n, gc, tc)
        return out
    finally:
        if own:
            cache.close()


def attach_tags(proposals: List[dict]) -> List[dict]:
    """Adds ``tags`` to each proposal dict in place; a failure leaves the
    cards on their plain genres rather than failing the list."""
    try:
        for i, tags in tags_for(proposals).items():
            proposals[i]["tags"] = tags
    except Exception as e:
        logger.warning("[tags] card tags unavailable: %s", e)
    return proposals
