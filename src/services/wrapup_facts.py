"""
Curatarr — wrap-up facts: everything a wrap-up says, computed before any
model sees it.

The narrative is only as good as the numbers under it, and a number the
model computes is a number the model can get wrong. So every superlative,
contrast and outlier is decided HERE, in SQL and plain Python over the
user's own rows, and the curator only names and phrases them
(services/wrapup_story). Blocking — run it with ``run_read``.

Hours are PLAYED time, not item length: an abandoned ten-minute start of a
three-hour film is ten minutes. Spotify rows carry the played time in
view_offset_ms (duration_ms is NULL there), and the Plex history import
writes finished rows with no duration at all — those get a per-type
estimate, and the share of estimated time travels with the figure so the
tile can say "≈". (The old curation report summed duration_ms of every
play: partial plays counted in full, Spotify and imported history not at
all.)

A section with nothing real to say is LEFT OUT, never zeroed: the UI
renders only earned tiles and the prompt only sees real facts.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional

from src.database import orphaned
from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

SCOPES = ("all", "movie", "show", "anime", "music")
VIDEO = ("movie", "show", "anime")

# Assumed length of a finished play that carries no duration (history import)
_EST_MIN = {"movie": 105, "show": 42, "anime": 24, "music": 3.5}
_EST_DEFAULT_MIN = 45
_MIN_VOTES = 200            # a community score below this mass is noise
_ACCLAIMED = 7.8            # /10 — "acclaimed" for the highbrow and dismissal tiles
_ACCLAIMED_VOTES = 1000
_CONTRARIAN_COMMUNITY_MAX = 6.8
_CONTRARIAN_USER_MIN = 8.0
_TOP_ENRICH = 60            # titles whose verified data is read (cache only)
_COMEBACK_GAP_DAYS = 365
# Below this the wrap-up says "not enough yet" instead of dressing up three
# plays (Letterboxd asks for ten films, PlayStation for ten hours).
_ELIGIBLE_PLAYS = 5
_ELIGIBLE_HOURS = 3
# Bumped when the watch data underneath changes meaning: a closed year's
# facts are frozen, except when they were built on an older generation.
# 2: the owner's plays come from the Plex history (every play, real partials).
# 3: hours and days on the user's own clock, DST included (services/user_time).
# 4: the skip rate from Spotify's skips (music_skips), none without them.
# 5: court record, GB in vs out, time-to-watch, auteurs (services/wrapup_sections).
# 6: hours per day for the story mode's heatmap.
FACTS_VERSION = 10


@dataclass(frozen=True)
class Period:
    key: str
    start: datetime
    end: datetime
    label: str
    closed: bool            # a past calendar year: its facts never change

    @property
    def previous(self) -> Optional[tuple]:
        """The window just before this one, for "vs last time" — the same
        calendar span a year earlier for a year, the same length otherwise."""
        if self.key == "all":
            return None
        if self.key.startswith("year:"):
            y = self.start.year - 1
            return datetime(y, 1, 1), self.end.replace(year=y)
        return self.start - (self.end - self.start), self.start


def resolve_period(key: str, now: Optional[datetime] = None) -> Period:
    now = now or datetime.utcnow()
    if key == "30d":
        return Period(key, now - timedelta(days=30), now, "Last 30 days", False)
    if key == "6m":
        return Period(key, now - timedelta(days=182), now, "Last 6 months", False)
    if key == "all":
        return Period(key, datetime(1970, 1, 1), now, "All time", False)
    m = re.fullmatch(r"year:(\d{4})", key or "")
    if m and 2000 <= int(m.group(1)) <= now.year:
        y = int(m.group(1))
        end = datetime(y + 1, 1, 1)
        return Period(key, datetime(y, 1, 1), min(end, now), f"{y} in review", end <= now)
    raise ValueError(f"unknown period {key!r}")


def scope_types(scope: str) -> tuple:
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    return VIDEO + ("music",) if scope == "all" else (scope,)


# ── played time ─────────────────────────────────────────────────────────────

def played_ms(row: dict) -> tuple:
    """(played milliseconds, estimated?) for one play row — the Python twin
    of played_ms_sql, for rows already in memory."""
    if row.get("source") == "spotify":
        return int(row.get("view_offset_ms") or 0), False
    if row.get("completed"):
        if row.get("duration_ms"):
            return int(row["duration_ms"]), False
        return int(_EST_MIN.get(row.get("media_type"), _EST_DEFAULT_MIN) * 60_000), True
    return int(row.get("view_offset_ms") or 0), False


def a_play(W):
    """Not a row Plex wrote for "mark as played" (``source="marked"``, see
    plex_sync): it says the title was seen, not when or for how long, so it
    stays out of everything counted inside a period."""
    from sqlalchemy import func
    return func.coalesce(W.source, "") != "marked"


def played_ms_sql(W):
    from sqlalchemy import case, func
    est = case(*[(W.media_type == k, int(v * 60_000)) for k, v in _EST_MIN.items()],
               else_=_EST_DEFAULT_MIN * 60_000)
    return case(
        (W.source == "spotify", func.coalesce(W.view_offset_ms, 0)),
        (W.completed == True, func.coalesce(W.duration_ms, est)),   # noqa: E712
        else_=func.coalesce(W.view_offset_ms, 0))


def _estimated_sql(W):
    from sqlalchemy import and_, case, func
    return case((and_(W.completed == True, W.duration_ms.is_(None),       # noqa: E712
                      func.coalesce(W.source, "") != "spotify"), 1), else_=0)


def _h(ms) -> float:
    return round((ms or 0) / 3_600_000, 1)


# ── verified data (cache only) ──────────────────────────────────────────────

def _verified_lookup(items: list) -> dict:
    """{title: verified subset} via build_verified_data — the enrichment cache
    only, no LLM, no network. ``items``: dicts with title, media_type and
    whatever ids are known. A module function so tests can stand in for the
    cache."""
    from src.cache.metadata_cache import MetadataCache
    from src.services.media_enricher import build_verified_data
    from src.services.rating_value import count_number, rating_number

    out = {}
    if not items:
        return out
    with MetadataCache() as cache:
        for it in items:
            try:
                vd = build_verified_data(
                    it["title"], it["media_type"],
                    tmdb_id=it.get("tmdb_id"), tvdb_id=it.get("tvdb_id"),
                    anilist_id=it.get("anilist_id"), anidb_id=it.get("anidb_id"),
                    plex_rating_key=it.get("plex_item_id"), cache=cache) or {}
            except Exception as e:      # one broken cache row must not sink the wrap-up
                logger.warning("[wrapup] verified data for %r failed: %s", it["title"], e)
                vd = {}
            out[it["title"]] = {
                "year": vd.get("year") or it.get("year"),
                "rating": rating_number(vd.get("rating")),
                "votes": max(count_number(vd.get("vote_count")), count_number(vd.get("scored_by"))),
                "metacritic": rating_number(vd.get("metacritic")),
                "significant": bool(vd.get("significance")),
                "runtime_min": rating_number(vd.get("runtime_min")),
            }
    return out


def _identities(db, titles: list) -> dict:
    """{(lower title, media_type): MediaIdentity} — series ids live there,
    not on the episode rows."""
    from sqlalchemy import func
    from src.database.models import MediaIdentity as MI
    names = list({t["title"].lower() for t in titles if t.get("title")})
    out = {}
    for i in range(0, len(names), 500):
        for mi in db.query(MI).filter(func.lower(MI.title).in_(names[i:i + 500])):
            out.setdefault(((mi.title or "").lower(), mi.media_type), mi)
    return out


def _with_ids(db, items: list) -> list:
    idents = _identities(db, items)
    out = []
    for it in items:
        mi = idents.get((it["title"].lower(), it["media_type"]))
        out.append({**it,
                    "tmdb_id": it.get("tmdb_id") or (mi and mi.tmdb_id),
                    "tvdb_id": mi and mi.tvdb_id, "anilist_id": mi and mi.anilist_id,
                    "anidb_id": mi and mi.anidb_id, "year": mi and mi.year,
                    "plex_item_id": (mi and mi.plex_rating_key) or it.get("plex_item_id")})
    return out


# ── the collector ───────────────────────────────────────────────────────────

def build_wrapup_facts(user_id: int, scope: str, period_key: str,
                       now: Optional[datetime] = None, *,
                       is_admin: bool = False) -> dict:
    """The complete, deterministic fact set for one wrap-up."""
    from sqlalchemy import func
    from src.database.models import WatchHistoryEntry as W
    from src.services import wrapup_sections as _sections

    now = now or datetime.utcnow()
    period = resolve_period(period_key, now)
    types = scope_types(scope)
    ms = played_ms_sql(W)
    base = (W.user_id == user_id, W.viewed_at >= period.start,
            W.viewed_at < period.end, W.media_type.in_(types), a_play(W))

    out: dict = {"facts_version": FACTS_VERSION, "scope": scope, "period": {
        "key": period.key, "label": period.label, "closed": period.closed,
        "start": period.start.isoformat(), "end": period.end.isoformat()}}

    with get_db_session() as db:
        orphaned.include(db)        # plays of media gone from Plex count
        # The user's own clock, with the offset in force at each play
        # (services/user_time): night owl, peak hour, days and calendars.
        from src.services.user_time import local_sql, user_zone
        # Scanned from the first play, not from 1970 for "all time".
        first = db.query(func.min(W.viewed_at)).filter(*base).scalar()
        local = local_sql(W.viewed_at, user_zone(db, user_id),
                          max(period.start, first or period.start), period.end)
        plays, total_ms, est_ms = db.query(
            func.count(), func.sum(ms), func.sum(_estimated_sql(W) * ms)).filter(*base).one()
        if not plays:
            out["empty"] = True
            out["eligible"] = False
            return _finish(out)

        tempo = _tempo(db, W, ms, base, local, total_ms, est_ms, plays)
        tempo["by_type"] = {t: _h(v) for t, v in db.query(W.media_type, func.sum(ms))
                            .filter(*base).group_by(W.media_type).all() if v}
        if period.previous:
            ps, pe = period.previous
            prev = db.query(func.sum(ms)).filter(
                W.user_id == user_id, W.viewed_at >= ps, W.viewed_at < pe,
                W.media_type.in_(types), a_play(W)).scalar()
            if prev:
                tempo["prev_hours"] = _h(prev)
        out["tempo"] = tempo
        out["eligible"] = plays >= _ELIGIBLE_PLAYS and _h(total_ms) >= _ELIGIBLE_HOURS
        out["first_last"] = _first_last(db, W, base)

        titles = _title_rollup(db, W, ms, base)
        out["top"] = titles[:5]
        if titles:
            out["tempo"].update(_title_streak_and_calendar(db, W, base, local, titles[0]["title"]))
        out["discovery"] = _discovery(db, W, user_id, period, types, titles)
        out["comebacks"] = _comebacks(db, W, user_id, period, types)

        video_types = [t for t in types if t in VIDEO]
        if video_types:
            vrows = _video_rows(db, W, user_id, period, video_types)
            if vrows:
                prior = _prior_completions(db, W, user_id, period, vrows)
                out["completion"] = _completion(vrows, now)
                out["comfort"] = _comfort(vrows, prior)
                out["binge"] = _binge(vrows, period)
                vtitles = [t for t in titles if t["media_type"] in VIDEO][:_TOP_ENRICH]
                stars = _swipe_stars(db, user_id)
                extra = _dismissal_candidates(db, user_id, period, vrows, stars, now, types)
                want = _with_ids(db, vtitles + [c for c in extra if c["title"] not in
                                                {t["title"] for t in vtitles}])
                enriched = _verified_lookup(want)
                out["eras"] = _eras(vtitles, enriched, period)
                out["highbrow_comfort"] = _highbrow_split(vtitles, enriched, out["comfort"])
                out["contrarian"] = _contrarian(vtitles, enriched, out["comfort"], stars)
                out["dismissal"] = _dismissal(extra, enriched)
                vnames = {t["title"] for t in vtitles}
                out["auteurs"] = _sections.auteurs(db, [w for w in want if w["title"] in vnames])
        if "music" in types:
            out["music"] = _music(db, W, ms, user_id, base, period)
            moods = _moods(db, W, ms, base, local)
            if out["music"] and moods:
                out["music"]["moods"] = moods
        out["devices"] = _devices(db, W, ms, base, local)

        out["genres"] = _genres(db, W, ms, user_id, period, types)
        out["curatarr"] = _curatarr_angle(db, user_id, period, scope, types, is_admin)
        out["court"] = _sections.court_record(db, user_id, period, scope)
        out["time_to_watch"] = _sections.time_to_watch(db, W, user_id, period, types)
        if is_admin:
            # the server is the owner's to fill and to prune
            out["velocity"] = _sections.library_velocity(db, period, types)
        out["look_ahead"] = _look_ahead(db, user_id, types)

    out["traits"] = derive_traits(out, now)
    return _finish({k: v for k, v in out.items() if v not in (None, {}, [])})


def _finish(out: dict) -> dict:
    out["facts_hash"] = facts_hash(out)
    out["generated_at"] = datetime.utcnow().isoformat()
    return out


def facts_hash(facts: dict) -> str:
    """The identity of the numbers: what a story was written for. Ignores the
    time of computation and anything decorative attached afterwards."""
    core = {k: v for k, v in facts.items()
            if k not in ("generated_at", "facts_hash", "drift", "river", "posters")}
    return hashlib.sha1(json.dumps(core, sort_keys=True, default=str).encode(),
                        usedforsecurity=False).hexdigest()


# ── tempo & rhythm ──────────────────────────────────────────────────────────

_DAYPARTS = (("night", range(0, 5)), ("morning", range(5, 11)),
             ("afternoon", range(11, 17)), ("evening", range(17, 23)),
             ("night", range(23, 24)))


def _tempo(db, W, ms, base, local, total_ms, est_ms, plays) -> dict:
    from sqlalchemy import func
    hr = func.strftime("%H", local)
    by_hour = [0] * 24
    for h, v in db.query(hr, func.sum(ms)).filter(*base).group_by(hr).all():
        if h is not None:
            by_hour[int(h)] = int(v or 0)
    dw = func.strftime("%w", local)                     # 0 = Sunday
    by_dow = [0] * 7
    for d, v in db.query(dw, func.sum(ms)).filter(*base).group_by(dw).all():
        if d is not None:
            by_dow[int(d)] = int(v or 0)
    day = func.date(local)
    days = [r for r in db.query(day, func.sum(ms), func.count()).filter(*base)
            .group_by(day).all() if r[0]]
    weekend_day = db.query(func.sum(ms)).filter(
        *base, func.strftime("%w", local).in_(("0", "6")),
        func.strftime("%H", local).between("11", "17")).scalar() or 0

    parts = Counter()
    for name, hours in _DAYPARTS:
        parts[name] += sum(by_hour[h] for h in hours)
    total = max(1, sum(by_hour))
    big = max(days, key=lambda r: r[1] or 0)
    return {
        "hours": _h(total_ms), "plays": int(plays),
        "hours_estimated_share": round((est_ms or 0) / max(1, total_ms or 0), 2),
        "active_days": len(days),
        "longest_streak_days": _streak([d for d, _, _ in days]),
        "peak_hour": max(range(24), key=lambda h: by_hour[h]),
        "peak_weekday": max(range(7), key=lambda d: by_dow[d]),
        "by_hour_h": [_h(v) for v in by_hour],
        "dayparts": {k: round(v / total, 2) for k, v in parts.items()},
        "weekend_share": round((by_dow[0] + by_dow[6]) / max(1, sum(by_dow)), 2),
        "weekend_daytime_share": round(weekend_day / total, 2),
        "biggest_day": {"date": big[0], "hours": _h(big[1]), "plays": int(big[2])},
        # every active day's hours on the user's clock — the story's heatmap
        "by_day_h": {str(d): _h(v) for d, v, _ in days},
    }


def _streak(dates: list) -> int:
    ds = sorted({datetime.strptime(str(d)[:10], "%Y-%m-%d").date() for d in dates if d})
    best = cur = 1 if ds else 0
    for a, b in zip(ds, ds[1:]):
        cur = cur + 1 if (b - a).days == 1 else 1
        best = max(best, cur)
    return best


def _first_last(db, W, base) -> dict:
    def one(order):
        r = (db.query(W.title, W.series_title, W.media_type, W.viewed_at)
             .filter(*base).order_by(order).first())
        return r and {"title": r.series_title or r.title, "media_type": r.media_type,
                      "at": r.viewed_at.isoformat()}
    return {"first": one(W.viewed_at.asc()), "last": one(W.viewed_at.desc())}


def _title_streak_and_calendar(db, W, base, local, top_title) -> dict:
    """The longest run of consecutive days with one title (Apple's artist
    streak), and the days the #1 title was played (YouTube's calendar)."""
    from sqlalchemy import func
    key = func.coalesce(W.series_title, W.title)
    day = func.date(local)
    by_title = defaultdict(set)
    for k, d in db.query(key, day).filter(*base).distinct():
        if k and d:
            by_title[k].add(str(d))
    out = {"top_calendar": sorted(by_title.get(top_title, ()))}
    best = max(((k, _streak(list(ds))) for k, ds in by_title.items()),
               key=lambda x: x[1], default=None)
    if best and best[1] >= 3:
        out["title_streak"] = {"title": best[0], "days": best[1]}
    return out


# ── titles ──────────────────────────────────────────────────────────────────

def _title_rollup(db, W, ms, base) -> list:
    """Hour-ranked titles (a series, a film, an artist), top 80."""
    from sqlalchemy import func
    key = func.coalesce(W.series_title, W.title)
    total = func.sum(ms)
    rows = (db.query(key, W.media_type, total, func.count(), func.max(W.tmdb_id),
                     func.max(W.plex_item_id))
            .filter(*base).group_by(key, W.media_type)
            .order_by(total.desc()).limit(80).all())
    return [{"title": k, "media_type": mt, "hours": _h(v), "plays": int(n),
             "tmdb_id": tmdb, "plex_item_id": pk}
            for k, mt, v, n, tmdb, pk in rows if k]


def _discovery(db, W, user_id, period, types, titles) -> dict:
    """Scout vs Loyalist: the share of this period's time on titles (artists,
    for music) never played before the period began."""
    from sqlalchemy import func
    if period.key == "all" or not titles:
        return {}
    keys = [t["title"] for t in titles]
    key = func.coalesce(W.series_title, W.title)
    known = {k for (k,) in db.query(key).filter(
        W.user_id == user_id, W.viewed_at < period.start,
        W.media_type.in_(types), key.in_(keys)).distinct()}
    total = sum(t["hours"] for t in titles) or 1
    new = [t for t in titles if t["title"] not in known]
    return {"new_share": round(sum(t["hours"] for t in new) / total, 2),
            "new_titles": len(new), "returning_titles": len(titles) - len(new),
            "best_new": new[0]["title"] if new else None}


def _comebacks(db, W, user_id, period, types) -> list:
    """Titles (artists, for music) back this period after at least a year of
    silence — Apple Replay's Comebacks. The gap runs from the last play before
    the period to the first play inside it."""
    from sqlalchemy import func
    if period.key == "all":
        return []
    key = func.coalesce(W.series_title, W.title)
    inside = {k: v for k, v in db.query(key, func.min(W.viewed_at)).filter(
        W.user_id == user_id, W.media_type.in_(types), a_play(W),
        W.viewed_at >= period.start, W.viewed_at < period.end).group_by(key).all() if k}
    if not inside:
        return []
    names = list(inside)
    out = []
    for i in range(0, len(names), 500):
        for k, last in db.query(key, func.max(W.viewed_at)).filter(
                W.user_id == user_id, W.media_type.in_(types), W.viewed_at < period.start,
                key.in_(names[i:i + 500])).group_by(key):
            gap = (inside[k] - last).days
            if gap >= _COMEBACK_GAP_DAYS:
                out.append({"title": k, "gap_days": gap})
    return sorted(out, key=lambda c: -c["gap_days"])[:3]


def _video_rows(db, W, user_id, period, video_types) -> list:
    cols = (W.plex_item_id, W.title, W.series_title, W.media_type, W.season,
            W.episode, W.viewed_at, W.duration_ms, W.view_offset_ms, W.completed,
            W.tmdb_id, W.source)
    names = [c.key for c in cols]
    q = (db.query(*cols).filter(W.user_id == user_id, W.viewed_at >= period.start,
                                W.viewed_at < period.end, W.media_type.in_(video_types), a_play(W))
         .order_by(W.viewed_at.asc()))
    return [dict(zip(names, r)) for r in q]


def _prior_completions(db, W, user_id, period, vrows) -> set:
    """(title key, season, episode) completed BEFORE the period — the comfort
    rewatches. A film keys on (title, None, None)."""
    if period.key == "all":
        return set()
    names = list({r["series_title"] or r["title"] for r in vrows})
    out = set()
    for i in range(0, len(names), 400):
        chunk = names[i:i + 400]
        q = db.query(W.series_title, W.title, W.season, W.episode).filter(
            W.user_id == user_id, W.viewed_at < period.start, W.completed == True,   # noqa: E712
            (W.series_title.in_(chunk)) | (W.title.in_(chunk)))
        out |= {(s or t, se, ep) for s, t, se, ep in q}
    return out


# ── completion, comfort, binge ──────────────────────────────────────────────

def _completion(vrows, now) -> dict:
    from src.services.taste_engine import _is_drop
    from src.services.viewing_sessions import _watched_enough
    done = sum(1 for r in vrows if _watched_enough(r))
    drops = [r for r in vrows if _is_drop(r, now)]
    dropped = Counter(r["series_title"] or r["title"] for r in drops)
    return {"completion_rate": round(done / len(vrows), 2),
            "drop_rate": round(len(drops) / len(vrows), 2),
            "dropped_titles": [t for t, _ in dropped.most_common(5)]}


def _comfort(vrows, prior) -> dict:
    """The comfort title: most re-completions inside the period plus
    completions of things already finished before it."""
    reps = Counter()
    seen = Counter()
    kinds = {}
    for r in vrows:
        if not r["completed"]:
            continue
        k = (r["series_title"] or r["title"], r["season"], r["episode"])
        kinds[k[0]] = r["media_type"]
        seen[k] += 1
        if seen[k] > 1 or k in prior:
            reps[k[0]] += 1
    if not reps:
        return {}
    title, n = reps.most_common(1)[0]
    return {"title": title, "media_type": kinds.get(title), "replays": n,
            "comfort_share": round(sum(reps.values()) / (sum(seen.values()) or 1), 2),
            "comfort_titles": [t for t, _ in reps.most_common(8)]}


def _binge(vrows, period) -> dict:
    """Deepest sitting (viewing_sessions.sittings — phantom bulk-marks are not
    episodes) and the most episodes of one series inside any 48 hours."""
    from src.services.viewing_sessions import PHANTOM_GAP_MIN, _watched_enough, sittings
    by_series = defaultdict(list)
    for r in vrows:
        if r["series_title"]:
            by_series[r["series_title"]].append(r)
    best_sit = best_48 = None
    span = max(1, (period.end - period.start).days + 1)
    for s, rows in by_series.items():
        for sit in sittings(rows, now=period.end, window_days=span):
            if sit["episodes"] and (not best_sit or sit["episodes"] > best_sit["episodes"]):
                best_sit = {"series": s, "media_type": rows[0]["media_type"],
                            "episodes": sit["episodes"],
                            "span_min": sit["span_min"], "date": sit["start"].date().isoformat()}
        eps, prev = [], None
        for r in rows:
            real = prev is None or (r["viewed_at"] - prev).total_seconds() >= PHANTOM_GAP_MIN * 60
            if real and _watched_enough(r):
                eps.append(r["viewed_at"])
            prev = r["viewed_at"]
        lo = 0
        for hi, t in enumerate(eps):
            while t - eps[lo] > timedelta(hours=48):
                lo += 1
            n = hi - lo + 1
            if not best_48 or n > best_48["episodes"]:
                best_48 = {"series": s, "media_type": rows[0]["media_type"],
                           "episodes": n, "ended": t.date().isoformat()}
    out = {}
    if best_sit and best_sit["episodes"] >= 3:
        out["deepest_sitting"] = best_sit
    if best_48 and best_48["episodes"] >= 4:
        out["in_48h"] = best_48
    return out


# ── eras, contrasts, outliers ───────────────────────────────────────────────

def _eras(titles, enriched, period) -> dict:
    """Viewing Age (hour-weighted median release year), oldest vs newest,
    decade shares, Steam's fresh / recent / catalogue split, and Goodreads'
    shortest vs longest (films)."""
    pts = sorted((int(enriched[t["title"]]["year"]), t["hours"], t["title"])
                 for t in titles if (enriched.get(t["title"]) or {}).get("year"))
    if len(pts) < 3:
        return {}
    half, acc, median = sum(h for _, h, _ in pts) / 2, 0.0, pts[0][0]
    for y, h, _ in pts:
        acc += h
        if acc >= half:
            median = y
            break
    total = sum(h for _, h, _ in pts) or 1
    dec, split = Counter(), Counter()
    for y, h, _ in pts:
        dec[f"{y // 10 * 10}s"] += h
        age = period.end.year - y
        split["fresh" if age <= 1 else "recent" if age <= 7 else "catalogue"] += h
    out = {"viewing_age_year": median,
           "oldest": {"title": pts[0][2], "year": pts[0][0]},
           "newest": {"title": pts[-1][2], "year": pts[-1][0]},
           "decades": {k: round(v / total, 2) for k, v in sorted(dec.items())},
           "split": {k: round(split[k] / total, 2) for k in ("fresh", "recent", "catalogue")}}
    films = sorted((enriched[t["title"]]["runtime_min"], t["title"]) for t in titles
                   if t["media_type"] == "movie" and (enriched.get(t["title"]) or {}).get("runtime_min"))
    if len(films) >= 2 and films[-1][0] - films[0][0] >= 30:
        out["shortest"] = {"title": films[0][1], "minutes": int(films[0][0])}
        out["longest"] = {"title": films[-1][1], "minutes": int(films[-1][0])}
    return out


def _acclaimed(e: Optional[dict]) -> bool:
    if not e:
        return False
    return bool(((e.get("rating") or 0) >= _ACCLAIMED and (e.get("votes") or 0) >= _ACCLAIMED_VOTES)
                or (e.get("metacritic") or 0) >= 75 or e.get("significant"))


def _highbrow_split(titles, enriched, comfort) -> dict:
    comfort_set = set((comfort or {}).get("comfort_titles") or [])
    hb = cf = other = 0.0
    top_hb = None
    for t in titles:
        if t["title"] in comfort_set:
            cf += t["hours"]
        elif _acclaimed(enriched.get(t["title"])):
            hb += t["hours"]
            top_hb = top_hb or t["title"]
        else:
            other += t["hours"]
    total = hb + cf + other
    if total < 2 or not (hb or cf):
        return {}
    top_cf = next((t["title"] for t in titles if t["title"] in comfort_set), None)
    return {"highbrow": round(hb / total, 2), "comfort": round(cf / total, 2),
            "other": round(other / total, 2), "top_highbrow": top_hb, "top_comfort": top_cf}


def _swipe_stars(db, user_id) -> dict:
    from src.database.models import SwipeRating
    return {(r.title or "").lower(): {"stars": r.stars, "category": r.category, "title": r.title}
            for r in db.query(SwipeRating).filter(
                SwipeRating.user_id == user_id, SwipeRating.seen == True,      # noqa: E712
                SwipeRating.stars.isnot(None), SwipeRating.title.isnot(None))}


def _contrarian(titles, enriched, comfort, stars) -> dict:
    """Biggest gap between the user's own verdict and the community's: swipe
    stars where the user gave them; a comfort rewatch counts as an 8.5."""
    comfort_set = set((comfort or {}).get("comfort_titles") or [])
    best = None
    for t in titles:
        e = enriched.get(t["title"]) or {}
        com, votes = e.get("rating"), e.get("votes") or 0
        if com is None or votes < _MIN_VOTES or com > _CONTRARIAN_COMMUNITY_MAX:
            continue
        s = stars.get(t["title"].lower())
        mine, basis = (float(s["stars"]), "rated") if s else (None, "")
        if mine is None and t["title"] in comfort_set:
            mine, basis = 8.5, "rewatched"
        if mine is None or mine < _CONTRARIAN_USER_MIN:
            continue
        gap = round(mine - com, 1)
        if not best or gap > best["gap"]:
            best = {"title": t["title"], "media_type": t["media_type"], "you": mine,
                    "community": round(com, 1), "votes": votes, "gap": gap, "basis": basis}
    return best or {}


def _dismissal_candidates(db, user_id, period, vrows, stars, now, types) -> list:
    """Titles the user walked away from this period: dropped, deleted, or
    swiped 4 stars or less."""
    from src.database.models import DeletionProposal as DP
    from src.services.taste_engine import _is_drop
    seen, out = set(), []

    def add(title, media_type, how, tmdb_id=None):
        if title and title.lower() not in seen and media_type in VIDEO:
            seen.add(title.lower())
            out.append({"title": title, "media_type": media_type, "how": how, "tmdb_id": tmdb_id})

    for r in vrows:
        if _is_drop(r, now):
            add(r["series_title"] or r["title"], r["media_type"], "dropped", r.get("tmdb_id"))
    for dp in db.query(DP.title, DP.category, DP.tmdb_id).filter(
            DP.user_id == user_id, DP.status == "deleted", DP.category.in_(types),
            DP.resolved_at >= period.start, DP.resolved_at < period.end).limit(40):
        add(dp.title, dp.category, "deleted", dp.tmdb_id)
    for s in stars.values():
        if s["stars"] <= 4:
            add(s["title"], s["category"], "rated low")
    return out[:40]


def _dismissal(cands, enriched) -> dict:
    """The acclaimed title the user walked away from — the highest community
    score among the candidates wins."""
    best = None
    for c in cands:
        e = enriched.get(c["title"]) or {}
        if (e.get("rating") or 0) < _ACCLAIMED or (e.get("votes") or 0) < _ACCLAIMED_VOTES:
            continue
        if not best or e["rating"] > best["community"]:
            best = {"title": c["title"], "media_type": c["media_type"], "how": c["how"],
                    "community": round(e["rating"], 1), "votes": e["votes"], "year": e.get("year")}
    return best or {}


# ── music ───────────────────────────────────────────────────────────────────

def _music(db, W, ms, user_id, base, period) -> dict:
    """Top artists and tracks, the track on repeat (most plays in one day),
    the skip rate, and the user's best-rated artist (PlexAmp stars)."""
    from sqlalchemy import case, func
    from src.database.models import MusicSkip, PlexRating
    mb = (*base[:3], W.media_type == "music")
    n = db.query(func.count()).filter(*mb).scalar()
    if not n:
        return {}
    total = func.sum(ms)
    artists = (db.query(W.series_title, total, func.count())
               .filter(*mb, W.series_title.isnot(None))
               .group_by(W.series_title).order_by(total.desc()).limit(5).all())
    tracks = (db.query(W.title, W.series_title, func.count())
              .filter(*mb).group_by(W.title, W.series_title)
              .order_by(func.count().desc()).limit(5).all())
    day = func.date(W.viewed_at)
    rep = (db.query(W.title, W.series_title, day, func.count())
           .filter(*mb).group_by(W.title, W.series_title, day)
           .order_by(func.count().desc()).first())
    fav = (db.query(PlexRating.artist_name, func.max(PlexRating.rating))
           .filter(PlexRating.user_id == user_id, PlexRating.artist_name.isnot(None))
           .group_by(PlexRating.artist_name).order_by(func.max(PlexRating.rating).desc())
           .first())
    out = {
        "top_artists": [{"artist": a, "hours": _h(v), "plays": int(c)} for a, v, c in artists],
        "top_tracks": [{"track": t, "artist": a, "plays": int(c)} for t, a, c in tracks],
    }
    # Skips are the Spotify plays under 30 s (music_skips) plus its plays
    # that did not finish. A Plex track is always written finished, so only
    # Spotify listening has a skip rate — computed over all plays it read
    # "skipped 0%" for every Plexamp listener.
    # sum() of a Boolean column comes back as a Boolean (True, i.e. 1) —
    # count the finished plays as integers.
    sp_n, sp_done = db.query(func.count(), func.sum(case((W.completed == True, 1), else_=0))).filter(  # noqa: E712
        *mb, W.source == "spotify").one()
    sk = (MusicSkip.user_id == user_id, MusicSkip.at >= period.start, MusicSkip.at < period.end)
    n_skips = db.query(func.count(MusicSkip.id)).filter(*sk).scalar() or 0
    if sp_n or n_skips:
        out["skip_rate"] = round((n_skips + (sp_n or 0) - (sp_done or 0)) / ((sp_n or 0) + n_skips), 2)
        worst = (db.query(MusicSkip.title, MusicSkip.artist, func.count())
                 .filter(*sk).group_by(MusicSkip.title, MusicSkip.artist)
                 .order_by(func.count().desc()).first())
        if worst and worst[2] >= 3:
            out["most_skipped"] = {"track": worst[0], "artist": worst[1], "skips": int(worst[2])}
    if rep and rep[3] >= 5:
        out["on_repeat"] = {"track": rep[0], "artist": rep[1], "date": str(rep[2]),
                            "plays": int(rep[3])}
    if fav and fav[0]:
        out["best_rated_artist"] = fav[0]
    return out


def _daypart(hour: int) -> str:
    return next(name for name, hours in _DAYPARTS if hour in hours)


_MOOD_COVERAGE = 0.3        # of the music time, else the moods say too little
_MOOD_MIN_H = 2.0


def _moods(db, W, ms, base, local) -> dict:
    """What the music sounded like: Plex's moods and styles over the played
    time (services/music_tags), and the leading mood of each part of the
    day on the user's clock. {} under a third of the time tagged."""
    from sqlalchemy import func
    from src.services.music_tags import tags_for_tracks
    hr = func.strftime("%H", local)
    rows, names = [], {}
    for k, artist, album, h, v in (db.query(W.plex_item_id, W.series_title, W.album, hr, func.sum(ms))
                                   .filter(*base[:3], W.media_type == "music")
                                   .group_by(W.plex_item_id, W.series_title, W.album, hr)):
        if k and h is not None and v:
            rows.append((k, int(h), int(v)))
            if artist:
                names[str(k)] = (artist, album)
    total = sum(v for _, _, v in rows)
    tags = tags_for_tracks(db, list({k for k, _, _ in rows}), names)
    moods, styles, tagged = Counter(), Counter(), 0
    by_part: dict = defaultdict(Counter)
    part_ms = Counter()
    for k, h, v in rows:
        t = tags.get(str(k))
        if not t:
            continue
        if t["mood"]:
            tagged += v
            part = _daypart(h)
            part_ms[part] += v
            for m in t["mood"]:
                moods[m] += v
                by_part[part][m] += v
        for st in t["style"]:
            styles[st] += v
    if not total or tagged / total < _MOOD_COVERAGE or _h(tagged) < _MOOD_MIN_H:
        return {}
    out = {
        "coverage": round(tagged / total, 2),
        "top_moods": [{"mood": m, "share": round(v / tagged, 2)} for m, v in moods.most_common(5)],
        "top_styles": [{"style": st, "share": round(v / total, 2)}
                       for st, v in styles.most_common(5)],
    }
    # a part of the day needs an hour of tagged music to have a mood
    out["by_daypart"] = {p: {"mood": c.most_common(1)[0][0],
                             "share": round(c.most_common(1)[0][1] / part_ms[p], 2),
                             "hours": _h(part_ms[p])}
                         for p, c in by_part.items() if _h(part_ms[p]) >= 1}
    return out


_PLATFORMS = (("iphone", "iPhone"), ("ipad", "iPad"), ("tvos", "Apple TV"), ("apple tv", "Apple TV"),
              ("ios", "iPhone"), ("android tv", "Android TV"), ("android", "Android"),
              ("windows", "Windows"), ("os x", "Mac"), ("macos", "Mac"), ("osx", "Mac"),
              ("chromecast", "Chromecast"), ("cast", "Chromecast"), ("roku", "Roku"),
              ("tizen", "Samsung TV"), ("webos", "LG TV"), ("xbox", "Xbox"),
              ("playstation", "PlayStation"), ("sonos", "Sonos"), ("linux", "Linux"),
              ("chrome", "Web"), ("firefox", "Web"), ("safari", "Web"), ("web", "Web"))


def platform_family(platform: Optional[str]) -> Optional[str]:
    """Spotify writes "iOS 17.1 (iPhone14,2)", Plex "iOS": one name each."""
    low = (platform or "").lower()
    if not low:
        return None
    return next((name for key, name in _PLATFORMS if key in low), (platform or "")[:32])


_DEVICE_KNOWN = 0.5         # of the time with a known player, else no device story


def _devices(db, W, ms, base, local) -> dict:
    """Where the time was spent: hours by player (the Plex device name, else
    the platform), and the player of the night. {} when under half the time
    has a known player or only one is known."""
    from sqlalchemy import func
    hr = func.strftime("%H", local)
    acc, night, total = Counter(), Counter(), 0
    for player, platform, h, v in (db.query(W.player, W.platform, hr, func.sum(ms))
                                   .filter(*base).group_by(W.player, W.platform, hr)):
        v = int(v or 0)
        total += v
        label = (player or "").strip() or platform_family(platform)
        if not label or h is None:
            continue
        acc[label] += v
        if _daypart(int(h)) == "night":
            night[label] += v
    known = sum(acc.values())
    if not total or known / total < _DEVICE_KNOWN or len(acc) < 2:
        return {}
    out = {"known_share": round(known / total, 2),
           "top": [{"player": p, "hours": _h(v), "share": round(v / known, 2)}
                   for p, v in acc.most_common(3)]}
    n_total = sum(night.values())
    if night and _h(n_total) >= 2:
        p, v = night.most_common(1)[0]
        out["night"] = {"player": p, "share": round(v / n_total, 2), "hours": _h(n_total)}
    return out


# ── genres vs baseline ──────────────────────────────────────────────────────

def _genre_shares(db, W, ms, filters) -> dict:
    from sqlalchemy import func
    acc = Counter()
    for g, v in (db.query(W.genres, func.sum(ms))
                 .filter(*filters, W.genres.isnot(None), W.genres != "")
                 .group_by(W.genres).all()):
        parts = [p.strip() for p in (g or "").split(",") if p.strip()]
        for p in parts:
            acc[p] += (v or 0) / len(parts)
    total = sum(acc.values()) or 1
    return {k: v / total for k, v in acc.items()}


def _genres(db, W, ms, user_id, period, types) -> dict:
    """Top genres by played time; against the user's own past: risers and
    fallers (percentage points) and genres played for the first time ever
    (SoundCloud's Genre Discovery Count)."""
    now_s = _genre_shares(db, W, ms, (W.user_id == user_id, W.viewed_at >= period.start,
                                      W.viewed_at < period.end, W.media_type.in_(types), a_play(W)))
    if not now_s:
        return {}
    top = sorted(now_s.items(), key=lambda kv: -kv[1])[:5]
    out = {"top": [{"genre": g, "share": round(s, 2)} for g, s in top]}
    if period.key != "all":
        was = _genre_shares(db, W, ms, (W.user_id == user_id, W.viewed_at < period.start,
                                        W.media_type.in_(types)))
        if was:
            delta = {g: now_s.get(g, 0) - was.get(g, 0) for g in set(now_s) | set(was)}
            ranked = sorted(delta.items(), key=lambda kv: kv[1])
            rising = [{"genre": g, "pp": round(d * 100)} for g, d in reversed(ranked[-2:]) if d >= 0.05]
            fading = [{"genre": g, "pp": round(d * 100)} for g, d in ranked[:2] if d <= -0.05]
            first = sorted((g for g in now_s if g not in was), key=lambda g: -now_s[g])
            if rising:
                out["rising"] = rising
            if fading:
                out["fading"] = fading
            if first:
                out["new_count"] = len(first)
                out["first_time"] = first[:5]
    return out


# ── the Curatarr angle ──────────────────────────────────────────────────────

def _curatarr_angle(db, user_id, period, scope, types, is_admin) -> dict:
    from sqlalchemy import func
    from src.database.models import (ConversationMessage as CM, CuratorPrinciple as CP,
                                     CuratorResolutionLog as CRL, DeletionProposal as DP,
                                     ProtectedMedia as PM)

    def inp(col):
        return (col >= period.start, col < period.end)

    def cat(col):
        return () if scope == "all" else (col == scope,)

    out: dict = {}
    dels = (db.query(DP).filter(DP.user_id == user_id, DP.status == "deleted",
                                *cat(DP.category), *inp(DP.resolved_at)).all())
    if dels:
        biggest = max(dels, key=lambda d: d.storage_mb or 0)
        out["graveyard"] = {
            "deleted": len(dels),
            "gb_freed": round(sum(d.storage_mb or 0 for d in dels) / 1024, 1),
            "biggest": {"title": biggest.title, "gb": round((biggest.storage_mb or 0) / 1024, 1),
                        "media_type": biggest.category, "poster": biggest.poster_url},
        }
    came_back = (db.query(DP).filter(DP.user_id == user_id, DP.returned_at.isnot(None),
                                     *cat(DP.category), *inp(DP.returned_at))
                 .order_by(DP.returned_at.desc()).first())
    if came_back:
        out["came_back"] = {"title": came_back.title, "media_type": came_back.category,
                            "poster": came_back.poster_url}

    ov = (db.query(CRL).filter(CRL.user_id == user_id, CRL.outcome == "kept",
                               CRL.resolution_type == "override", *cat(CRL.category),
                               *inp(CRL.created_at))
          .order_by(CRL.created_at.desc()).all())
    manual = (db.query(func.count(PM.id)).filter(PM.user_id == user_id, PM.source == "manual",
                                                 *cat(PM.category), *inp(PM.created_at))
              .scalar() or 0)
    if ov or manual:
        reasons = Counter(r.override_reason for r in ov if r.override_reason)
        shield = {"overrides": len(ov), "manual_protections": int(manual)}
        if reasons:
            shield["top_reason"] = reasons.most_common(1)[0][0]
        if ov:
            shield["fiercest"] = {"title": ov[0].title,
                                  "curator_stance": (ov[0].curator_stance or "")[:140]}
        out["shield"] = shield

    deb = (db.query(CM.thread_id, func.count()).filter(
        CM.user_id == user_id, CM.role == "user",
        CM.thread_id.like("deletion_proposal:%"), *inp(CM.created_at))
        .group_by(CM.thread_id).order_by(func.count().desc()).first())
    if deb and deb[1] >= 3:
        try:
            dp = db.get(DP, int(deb[0].split(":", 1)[1]))
        except (ValueError, IndexError):
            dp = None
        if dp and (scope == "all" or dp.category == scope):
            out["most_debated"] = {"title": dp.title, "messages": int(deb[1]),
                                   "outcome": dp.status, "media_type": dp.category,
                                   "poster": dp.poster_url}

    principles = (db.query(CP.text).filter(CP.user_id == user_id, CP.status == "active",
                                           *cat(CP.category), *inp(CP.activated_at))
                  .order_by(CP.activated_at.desc()).limit(3).all())
    if principles:
        out["principles"] = [p for (p,) in principles]

    dust = _dust_collector(db, user_id, types)
    if dust:
        out["dust_collector"] = dust
    if is_admin:
        out["ledger"] = True        # the UI loads the curation ledger into its own section
    return out


def _dust_collector(db, user_id, types) -> Optional[dict]:
    """The largest owned title this user has never played."""
    from sqlalchemy import func
    from src.database.models import MediaTechProfile as MTP, WatchHistoryEntry as W
    big = (db.query(MTP.title, MTP.media_type, MTP.size_mb, MTP.tmdb_id)
           .filter(MTP.media_type.in_(types), MTP.title.isnot(None), MTP.size_mb > 0)
           .order_by(MTP.size_mb.desc()).limit(60).all())
    if not big:
        return None
    titles = [b.title for b in big]
    played = {(t or "").lower() for (t,) in db.query(func.coalesce(W.series_title, W.title))
              .filter(W.user_id == user_id,
                      (W.series_title.in_(titles)) | (W.title.in_(titles))).distinct()}
    for b in big:
        if b.title.lower() not in played:
            return {"title": b.title, "media_type": b.media_type,
                    "gb": round((b.size_mb or 0) / 1024, 1), "tmdb_id": b.tmdb_id}
    return None


def _look_ahead(db, user_id, types) -> list:
    """Owned-but-unwatched picks from the recommendation cache's library lane
    — the only items a look-ahead may recommend."""
    from src.database.models import CachedRecommendation as CR
    rows = (db.query(CR).filter(CR.user_id == user_id, CR.lane == "library",
                                CR.category.in_(types))
            .order_by(CR.confidence.desc()).limit(5).all())
    return [{"title": r.title, "media_type": r.category, "year": r.year,
             "reason": (r.reason or "")[:160], "poster": r.poster_url} for r in rows]


# ── traits: the archetype is decided here, only named by the model ─────────

TRAIT_KEYS = ("night_owl", "matinee", "completionist", "sampler", "scout", "loyalist",
              "archivist", "day_one", "binger", "highbrow", "comfort_seeker",
              "contrarian", "purger", "defender")


def derive_traits(f: dict, now: datetime) -> list:
    """[{key, strength 0..1, evidence}] past their threshold, strongest first.
    ``strength`` = how far past the threshold, so the first trait is the one
    the user most clearly earned."""
    t = f.get("tempo") or {}
    out = []

    def add(key, value, threshold, ceiling, evidence):
        if value is None or value < threshold:
            return
        span = max(1e-6, ceiling - threshold)
        out.append({"key": key, "evidence": evidence,
                    "strength": round(min(1.0, 0.1 + 0.9 * (value - threshold) / span), 2)})

    dp = t.get("dayparts") or {}
    add("night_owl", dp.get("night"), 0.30, 0.70, f"{round(100 * dp.get('night', 0))}% after 23:00")
    add("matinee", t.get("weekend_daytime_share"), 0.35, 0.70,
        f"{round(100 * t.get('weekend_daytime_share', 0))}% on weekend afternoons")
    c = f.get("completion") or {}
    if (t.get("plays") or 0) >= 20 and c:
        add("completionist", c.get("completion_rate"), 0.90, 1.0,
            f"finished {round(100 * c['completion_rate'])}% of plays")
        add("sampler", c.get("drop_rate"), 0.25, 0.60, f"dropped {round(100 * c['drop_rate'])}% early")
    d = f.get("discovery") or {}
    if "new_share" in d:
        add("scout", d["new_share"], 0.60, 0.95, f"{round(100 * d['new_share'])}% on new titles")
        add("loyalist", 1 - d["new_share"], 0.75, 1.0,
            f"{round(100 * (1 - d['new_share']))}% on titles you already knew")
    e = f.get("eras") or {}
    if e:
        add("archivist", now.year - e["viewing_age_year"], 25, 50, f"viewing age {e['viewing_age_year']}")
        add("day_one", e["split"].get("fresh"), 0.50, 0.90,
            f"{round(100 * e['split']['fresh'])}% on this year's releases")
    b = (f.get("binge") or {}).get("in_48h") or {}
    if b:
        add("binger", b.get("episodes"), 10, 30, f"{b['episodes']} episodes of {b['series']} in 48 hours")
    hc = f.get("highbrow_comfort") or {}
    add("highbrow", hc.get("highbrow"), 0.50, 0.85, f"{round(100 * hc.get('highbrow', 0))}% acclaimed")
    add("comfort_seeker", hc.get("comfort"), 0.35, 0.70, f"{round(100 * hc.get('comfort', 0))}% rewatches")
    ct = f.get("contrarian") or {}
    add("contrarian", ct.get("gap"), 2.5, 5.0, ct and f"{ct['you']} vs {ct['community']} on {ct['title']}")
    cu = f.get("curatarr") or {}
    g = cu.get("graveyard") or {}
    add("purger", g.get("gb_freed"), 200, 2000, g and f"{g['gb_freed']} GB freed")
    s = cu.get("shield") or {}
    add("defender", s.get("overrides"), 3, 15, s and f"{s['overrides']} titles kept over the curator")
    out.sort(key=lambda x: -x["strength"])
    return out[:5]


# ── what the selector can offer ─────────────────────────────────────────────

def available(user_id: int) -> dict:
    """Scopes this user has plays in, and the calendar years with plays."""
    from sqlalchemy import func
    from src.database.models import WatchHistoryEntry as W
    with get_db_session() as db:
        orphaned.include(db)        # plays of media gone from Plex count
        types = [t for (t,) in db.query(W.media_type).filter(W.user_id == user_id)
                 .distinct() if t in SCOPES]
        years = sorted({int(y) for (y,) in db.query(func.strftime("%Y", W.viewed_at))
                        .filter(W.user_id == user_id).distinct() if y}, reverse=True)
    scopes = (["all"] if len(types) > 1 else []) + [s for s in SCOPES[1:] if s in types]
    return {"scopes": scopes or ["all"], "years": years}


# ── taste drift (vectors) ───────────────────────────────────────────────────

_DRIFT_MIN_NOW = 12
_DRIFT_MIN_PAST = 24
# A first guess; the log line below records every measured value so the
# words can be calibrated on the household's real month-over-month drift.
_DRIFT_STEADY, _DRIFT_SHIFT = 0.15, 0.35


async def taste_drift(user_id: int, scope: str, period_key: str) -> dict:
    """How this period's watching moved against the user's own past, in the
    embedding space the taste engine already uses: the centroid of the
    period's item vectors vs the centroid of everything before it, both
    centred on the domain corpus mean (raw text embeddings share one cone —
    uncentred, two different tastes still read ~0.94). Plus up to three
    clusters of the period with their share now vs before. Seconds of Chroma
    reads and NumPy: on the Chroma thread, never on the loop. Per medium only
    (the corpus mean is per domain); {} when there is too little to say."""
    if scope == "all" or period_key == "all":
        return {}
    from src.database.runner import run_read
    from src.vector_store.chromadb_wrapper import run_chroma

    period = resolve_period(period_key)
    now_rows, past_rows = await run_read(_drift_rows, user_id, scope, period)
    if len(now_rows) < _DRIFT_MIN_NOW or len(past_rows) < _DRIFT_MIN_PAST:
        return {}
    try:
        out = await run_chroma(_drift_sync, scope, now_rows, past_rows)
    except Exception as e:
        logger.warning("[wrapup] taste drift for %s/%s failed: %s", user_id, scope, e)
        return {}
    if out:
        logger.info("[wrapup] drift %s/%s/%s: %.3f (%d clusters)", user_id, scope, period_key,
                    out["drift"], len(out.get("clusters") or []))
    return out


_RIVER_MIN_MONTHS = 3


async def taste_river(user_id: int, scope: str, period_key: str) -> list:
    """The period month by month from the taste snapshots
    (services/taste_snapshots): each month's similarity to the month before,
    centred on the corpus mean, with what it was mostly made of. Per medium
    only, like the drift; [] under three months with a mean."""
    if scope == "all":
        return []
    from src.database.runner import run_read
    from src.services.taste_snapshots import series
    from src.vector_store.chromadb_wrapper import run_chroma
    period = resolve_period(period_key)
    lo, hi = period.start.strftime("%Y-%m"), period.end.strftime("%Y-%m")
    months = [m for m in await run_read(series, user_id, scope) if lo <= m["month"] <= hi]
    if sum(1 for m in months if m["mean"]) < _RIVER_MIN_MONTHS:
        return []
    try:
        return await run_chroma(_river_sync, scope, months)
    except Exception as e:
        logger.warning("[wrapup] taste river for %s/%s failed: %s", user_id, scope, e)
        return []


def _river_sync(category, months) -> list:
    from src.services.taste_engine import _domain_corpus_mean
    from src.services.taste_snapshots import month_over_month
    centre = _domain_corpus_mean(category)
    sims = dict(month_over_month(months, centre))
    return [{"month": m["month"], "hours": m["hours"], "similarity": sims.get(m["month"]),
             "top_titles": m["top_titles"][:3]} for m in months]


def _drift_rows(user_id, scope, period) -> tuple:
    from sqlalchemy import func
    from src.database.models import WatchHistoryEntry as W
    ms = played_ms_sql(W)
    key = func.coalesce(W.series_title, W.title)

    def top(db, filters, n):
        total = func.sum(ms)
        q = (db.query(key, func.max(W.series_title), func.max(W.plex_item_id), total)
             .filter(W.user_id == user_id, W.media_type == scope, a_play(W), *filters)
             .group_by(key).order_by(total.desc()).limit(n))
        return [{"title": k, "series_title": s, "plex_item_id": pk,
                 "media_type": scope, "w": float(v or 0)} for k, s, pk, v in q if k and v]

    with get_db_session() as db:
        orphaned.include(db)        # plays of media gone from Plex count
        return (top(db, (W.viewed_at >= period.start, W.viewed_at < period.end), 200),
                top(db, (W.viewed_at < period.start,), 400))


def _drift_sync(category, now_rows, past_rows) -> dict:
    import numpy as np
    from src.services.taste_engine import _chroma_item_vector, _cluster_centroids, _domain_corpus_mean

    def vecs(rows):
        out = []
        for r in rows:
            v = _chroma_item_vector(r, None)
            if v:
                out.append((np.asarray(v, dtype=float), r["w"], r["title"]))
        return out

    now_v, past_v = vecs(now_rows), vecs(past_rows)
    if len(now_v) < _DRIFT_MIN_NOW or len(past_v) < _DRIFT_MIN_PAST:
        return {}
    dim = len(now_v[0][0])
    now_v = [x for x in now_v if len(x[0]) == dim]
    past_v = [x for x in past_v if len(x[0]) == dim]
    mean = _domain_corpus_mean(category)
    centre = np.asarray(mean, dtype=float) if mean is not None and len(mean) == dim else np.zeros(dim)

    def centroid(vs):
        m = np.stack([v for v, _, _ in vs]) - centre
        w = np.asarray([w for _, w, _ in vs])
        c = (m * w[:, None]).sum(axis=0)
        n = np.linalg.norm(c)
        return c / n if n > 0 else c

    drift = round(float(1 - centroid(now_v) @ centroid(past_v)), 3)
    clusters = _cluster_centroids([(list(v), w, t) for v, w, t in now_v], max_k=3,
                                  corpus_mean=mean if mean is not None and len(mean) == dim else None) or []
    out_clusters = []
    if clusters:
        cents = np.stack([np.asarray(c["embedding"], dtype=float) for c in clusters])
        past_m = np.stack([v for v, _, _ in past_v])
        past_w = np.asarray([w for _, w, _ in past_v])
        assign = np.argmax(past_m @ cents.T, axis=1)
        for i, c in enumerate(clusters):
            before = float(past_w[assign == i].sum() / (past_w.sum() or 1))
            share = float(c["share"])
            out_clusters.append({
                "id": f"C{i + 1}", "share": round(share, 2), "share_before": round(before, 2),
                "trend": ("rising" if share - before >= 0.10 else
                          "fading" if before - share >= 0.10 else "steady"),
                "top_titles": c["top_titles"]})
    return {"drift": drift,
            "label": ("steady" if drift < _DRIFT_STEADY else
                      "shifting" if drift < _DRIFT_SHIFT else "new direction"),
            "clusters": out_clusters}
