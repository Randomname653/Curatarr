"""Harvest the external ids the *arr services already hold.

Sonarr and Radarr know each title's IMDb, TVDB and TMDB id. Curatarr asks
them for the library on every sync and keeps none of it: the enrichment path
resolves ids itself, which works for films and Western series and largely
fails for anime, where the profile comes from AniList and never touches TMDB.

The cost of that was invisible until two sources that key on the IMDb id —
OMDb and Wikidata — reported a quarter of the library as unreachable.
Measured on the live install: 2,319 video titles had no IMDb id, 1,833 of
them were sitting in Sonarr WITH one.

What the *arr tells us about its own entry is ground truth. The risk is not
the id, it is deciding WHICH entry a cached title refers to — and doing that
by name is the one soft step in an otherwise hard chain. So, in order:

  1. an id join, when the entry already carries a TVDB or TMDB id. No title
     is read at all; this is exact.
  2. the name, with the year holding a veto. Franchises reuse names freely
     and the works can be decades apart.

Audited against an independent anidb->tvdb mapping, the name step agreed on
1,788 of 1,796 checkable anime. Seven of the eight disagreements were the
mapping pointing at a parent series, not a bad match. The one real error was
"Space Pirate Captain Harlock": a cached entry dated 2013 (the CGI film)
wearing the id of the 1978 television series of the same name. The year was
sitting in both records the whole time, unused.

A wrong id is not a missing fact, it is a confident lie about a different
work — the same failure the corrupt-id detector and the Fix-match pin exist
to undo. Anything still ambiguous after both steps is left alone.
"""
import json
import logging

logger = logging.getLogger(__name__)

TV = ("show", "anime")

# Which *arr libraries a cached media_type may legitimately live in. Anime
# films sit in Radarr while anime series sit in Sonarr, and a raw entry says
# only "anime" either way — searching Sonarr alone is how a film ends up
# wearing a same-named series' id.
FAMILIES = {"movie": ("movie",), "show": ("tv",), "anime": ("tv", "movie")}

# A season that starts in December and finishes in January is dated either
# side depending on who is asked. Anything wider is a different work.
YEAR_SLACK = 1

# Stamped on every claim. Loosening or tightening the rules above means the
# claims already on disk were decided under rules that no longer apply, and
# bumping this offers every one of them for re-judgement — the same trick the
# distilled sources use to retire answers written under an older prompt.
# "1" is implicit: the first run recorded no version at all.
MATCH_VERSION = "2"


def _unprefixed(cache_key: str) -> str:
    """Strip the cache-version prefix a stored key carries."""
    from src.cache.metadata_cache import _CACHE_VERSION
    prefix = f"{_CACHE_VERSION}:"
    return cache_key[len(prefix):] if cache_key.startswith(prefix) else cache_key


def _norm(title: str) -> str:
    """Alphanumerics only — the difference between 'Re:ZERO' and 'Re Zero'
    is punctuation nobody agrees on."""
    return "".join(c for c in (title or "").lower() if c.isalnum())


def _ids_of(item: dict) -> dict:
    return {
        "imdb_id": item.get("imdbId") or None,
        "tvdb_id": item.get("tvdbId") or None,
        "tmdb_id": item.get("tmdbId") or None,
    }


def _index(entries: list) -> dict:
    """{normalised title: ids + year} for unambiguous names only.

    Every alternate title an *arr knows is indexed too — anime is routinely
    filed under a romanisation the enrichment side never saw. A name that two
    DIFFERENT works share is dropped rather than guessed; the same work listed
    twice is not a collision. The year rides along so a caller can veto a
    match the name alone would have allowed.
    """
    seen: dict = {}
    for item in entries:
        names = [item.get("title"), item.get("sortTitle")]
        names += [a.get("title") for a in (item.get("alternateTitles") or [])]
        ids = _ids_of(item)
        if not any(ids.values()):
            continue
        rec = dict(ids, year=item.get("year") or None)
        for name in filter(None, names):
            key = _norm(name)
            if not key:
                continue
            prev = seen.get(key)
            if prev is None and key not in seen:
                seen[key] = rec
            elif prev and _ids_of_rec(prev) != ids:
                seen[key] = None          # two works, one name — never claim it
    return {k: v for k, v in seen.items() if v}


def _ids_of_rec(rec: dict) -> dict:
    return {k: rec.get(k) for k in ("imdb_id", "tvdb_id", "tmdb_id")}


def _by_id(entries: list) -> dict:
    """{("tvdb"|"tmdb", id): ids + year} — the exact join.

    An id that two entries claim is dropped for the same reason a shared name
    is: it identifies nothing on its own.
    """
    out: dict = {}
    for item in entries:
        ids = _ids_of(item)
        if not any(ids.values()):
            continue
        rec = dict(ids, year=item.get("year") or None)
        for kind, field in (("tvdb", "tvdbId"), ("tmdb", "tmdbId")):
            v = item.get(field)
            if not v:
                continue
            key = (kind, str(v))
            prev = out.get(key)
            if prev is None and key not in out:
                out[key] = rec
            elif prev and _ids_of_rec(prev) != ids:
                out[key] = None
    return {k: v for k, v in out.items() if v}


def _year_ok(raw_year, arr_year) -> bool:
    """A year can only ever veto. Neither side is obliged to have one."""
    if not raw_year or not arr_year:
        return True
    try:
        return abs(int(raw_year) - int(arr_year)) <= YEAR_SLACK
    except (TypeError, ValueError):
        return True


def _match(raw: dict, index: dict, families: tuple):
    """Resolve one cached entry against the *arr libraries.

    Returns ``(record, how)`` — ``how`` being "tvdb", "tmdb" or "title" — or
    ``(None, None)`` when nothing can be claimed without guessing.
    """
    # 1. Exact: the entry already carries an id the *arr also files under.
    for field, kind in (("tvdb_id", "tvdb"), ("tmdb_id", "tmdb")):
        value = raw.get(field)
        if not value:
            continue
        for fam in families:
            rec = index[fam]["by_id"].get((kind, str(value)))
            if rec:
                return rec, kind

    # 2. Soft: the name, across every library the type may live in, with the
    #    year removing the candidates that are a different work.
    key = _norm(raw.get("title"))
    if not key:
        return None, None
    cands = [index[fam]["by_name"].get(key) for fam in families]
    cands = [c for c in cands if c]
    survivors = [c for c in cands if _year_ok(raw.get("year"), c.get("year"))]
    # A series and the film it spawned a year later share a name and both
    # survive the slack. An exact year is the stronger claim, so it wins
    # outright rather than being dragged into a tie — but two candidates
    # dated the same year really are indistinguishable, and stay refused.
    year = raw.get("year")
    if year and len(survivors) > 1:
        exact = [c for c in survivors if c.get("year") == year]
        if len(exact) == 1:
            return exact[0], "title"
    if len(survivors) == 1:
        return survivors[0], "title"
    return None, None


async def build_arr_id_index() -> dict:
    """{"tv": {"by_name":…, "by_id":…}, "movie": {…}} from whatever *arr is
    configured."""
    from src.config import settings
    from src.services.arr_client import RadarrClient, SonarrClient

    index = {"tv": {"by_name": {}, "by_id": {}},
             "movie": {"by_name": {}, "by_id": {}}}
    # The clients open their HTTP session in __aenter__; constructing one
    # without the context manager leaves it with no session at all.
    sources = (
        ("tv", SonarrClient, settings.effective_sonarr_url,
         settings.SONARR_API_KEY, "get_series"),
        ("movie", RadarrClient, settings.effective_radarr_url,
         settings.RADARR_API_KEY, "get_movies"),
    )
    for fam, cls, url, key, method in sources:
        if not (url and key):
            continue
        try:
            client = cls(url, key)
            async with client:
                entries = await getattr(client, method)() or []
            index[fam] = {"by_name": _index(entries), "by_id": _by_id(entries)}
        except Exception as e:
            logger.debug("[external-ids] %s unavailable: %s", fam, e)
    return index


def _families_for(raw: dict):
    mtype = {"tv": "show"}.get(raw.get("media_type"), raw.get("media_type"))
    return FAMILIES.get(mtype)


def _without_loose_claim(raw: dict, old: dict) -> dict:
    """A copy of ``raw`` with the earlier run's own answer taken back out.

    Re-judging an entry while it still wears the ids that run wrote is not a
    check, it is a mirror: the id join finds the very id under test and
    confirms it. Only values that provably came from that match are removed —
    an id the enrichment path resolved itself is never touched.
    """
    probe = dict(raw)
    for field in ("imdb_id", "tvdb_id", "tmdb_id"):
        if old and probe.get(field) and probe[field] == old.get(field):
            probe.pop(field, None)
    return probe


def _recheck(cache, index, rows) -> int:
    """Undo claims an earlier, looser run made that today's rules refuse.

    The first version of this harvest matched on the name alone, in one
    library, with no year check. Everything it wrote is marked, so it can be
    re-judged against the evidence it had rather than the answer it gave.
    """
    from src.services.media_enricher import _RAW_CACHE_DAYS
    reverted = 0
    for key, resp in rows:
        try:
            raw = json.loads(resp)
        except Exception:
            continue
        if not raw.get("ids_from_arr"):
            continue
        if raw.get("ids_from_arr_v") == MATCH_VERSION:
            continue                              # already judged under these rules
        families = _families_for(raw)
        if not families:
            continue
        # What that run would have claimed: one library, name only, no year.
        old = index[families[0]]["by_name"].get(_norm(raw.get("title")))
        probe = _without_loose_claim(raw, old)
        rec, how = _match(probe, index, families)
        if how:
            for field in ("imdb_id", "tvdb_id", "tmdb_id"):
                if rec.get(field):
                    probe[field] = rec[field]
            probe["ids_from_arr"] = how
            probe["ids_from_arr_v"] = MATCH_VERSION
            raw = probe
        else:
            probe.pop("ids_from_arr", None)
            probe.pop("ids_from_arr_v", None)
            raw = probe
            reverted += 1
        cache.set_cache(_unprefixed(key), raw, days=_RAW_CACHE_DAYS)
    return reverted


async def harvest(cache, *, limit: int = 0, task=None, should_stop=None) -> dict:
    """Fill missing ids on raw cache entries from the *arr libraries."""
    from src.services.media_enricher import _RAW_CACHE_DAYS

    index = await build_arr_id_index()
    if not any(index[f]["by_name"] or index[f]["by_id"] for f in index):
        return {"source": "external_ids", "visited": 0, "added": 0,
                "reverted": 0}

    def _rows():
        return cache.conn.execute(
            """
            SELECT cache_key, response FROM api_cache
            WHERE (cache_key LIKE 'v2:raw:%' OR cache_key LIKE 'raw:%')
              AND expires_at > datetime('now')
            """
        ).fetchall()

    rows = _rows()
    reverted = _recheck(cache, index, rows)
    if reverted:
        # A reverted entry is a gap again, and the fill pass below reads the
        # rows it was handed — re-read so this run can offer it a better
        # match instead of leaving it empty until the next one.
        rows = _rows()

    visited = added = 0
    by_how: dict = {}
    for key, resp in rows:
        if should_stop and should_stop():
            break
        try:
            raw = json.loads(resp)
        except Exception:
            continue
        if raw.get("imdb_id") or not raw.get("title"):
            continue
        families = _families_for(raw)
        if not families:
            continue
        visited += 1
        if limit and visited > limit:
            break
        rec, how = _match(raw, index, families)
        if not rec:
            continue
        changed = False
        for field in ("imdb_id", "tvdb_id", "tmdb_id"):
            if rec.get(field) and not raw.get(field):
                raw[field] = rec[field]
                changed = True
        if changed:
            # How the entry was identified, not merely that it was: an exact
            # id join and a name-with-year join are not equally trustworthy,
            # and a later audit should be able to tell them apart.
            raw["ids_from_arr"] = how
            raw["ids_from_arr_v"] = MATCH_VERSION
            by_how[how] = by_how.get(how, 0) + 1
            # set_cache prefixes the cache version itself; the key read back
            # out of the table already carries it, and passing it through
            # unchanged writes a second copy under "v2:v2:raw:…" that nothing
            # ever reads.
            cache.set_cache(_unprefixed(key), raw, days=_RAW_CACHE_DAYS)
            added += 1
        if task is not None and visited % 50 == 0:
            from src.services.task_monitor import task_monitor
            task_monitor.update(task, processed=visited, total=len(rows),
                                message=f"{added} matched so far")
    return {"source": "external_ids", "visited": visited, "added": added,
            "reverted": reverted, "by": by_how}
