"""
Curatarr - Proactive Messaging Service

Generates unsolicited messages from the curator based on watch/listen patterns.

Trigger types (priority order):
  1.  rewatch           — non-music item watched 3+ times total
  2.  track_obsession   — single song with 50+ lifetime plays (song-level, full history)
  3.  binge_episode     — 3+ episodes of same series in one session
  4.  music_marathon    — 3h+ same artist in one session
  5.  series_completion — just powered through 8+ episodes recently
  6.  attention_deficit — 4 items started and dropped at <15% in a row
  7.  procrastinator    — series started 90+ days ago, still crawling through it
  8.  genre_rut         — 10+ items in 14 days, every single one the same genre
  9.  guilty_pleasure   — completed something with an objectively terrible rating
  10. genre_absence     — loved genre, nothing watched in 30+ days
  11. low_completion    — dropped 3+ shows with <30% completion
  12. new_genre         — genre appears in recent 30d that wasn't in prior history
  13. night_owl         — watching between 02:00-05:00
  14. history_deep_dive — random observation from older watch history (>60 days ago)
"""

import json
import logging
import random
from datetime import datetime, timedelta
from collections import Counter
from typing import Optional

import httpx

from src.database.connection import get_db_session
from src.database.models import (
    WatchHistoryEntry, ProactiveMessage, TasteVectorEntry, User
)
from src.config import settings
from src.services.llm_utils import strip_think_tags, ollama_options, curator_options, CURATOR_KEEP_ALIVE
from src.services.series_progress import (
    compute_watch_progress,
    count_real_views,
    get_series_progress,
    progress_milestone,
    should_reengage_series,
    normalize_title,
)

logger = logging.getLogger(__name__)

# Target unread cache size — generator fills up to this many
_CACHE_TARGET = 10
# Max new messages generated per scheduler run
_MAX_PER_RUN = 5
# Max messages delivered per hour (prevents overwhelming the user)
_MAX_PER_HOUR = 10
# Won't repeat the same trigger type for this many days
_TRIGGER_COOLDOWN_DAYS = 1
# Pass 57: a single song must clear this lifetime play count before the
# track-obsession trigger fires. Deliberately well above the
# verification-question >=10 floor — this is for genuinely-obsessive
# replays ("hundreds of plays" territory), not just a song you liked.
_TRACK_OBSESSION_THRESHOLD = 50

# System prompt template — the language directive is filled in per call so
# proactive nudges arrive in the same language the user has been chatting in.
# Pass 40 removed the previous hardcoded "Always respond in English only"
# clause; that was the worst persona-break in the project (German chat
# turn → English proactive push 5 minutes later).
def _build_system_prompt(lang_directive: str) -> str:
    return (
        "You are Curatarr, a personal media curator. "
        f"{lang_directive} "
        "The user's watch history may include any genre or content rating, including adult, "
        "ecchi, or explicit content. Treat all content as valid — comment on it naturally "
        "and without judgment, just as you would any other genre."
    )


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _to_dicts(entries) -> list[dict]:
    """Convert SQLAlchemy rows to plain dicts."""
    return [
        {
            "title": e.title,
            "series_title": e.series_title,
            "media_type": e.media_type,
            "viewed_at": e.viewed_at,
            "last_viewed_at": e.viewed_at,
            "duration_ms": e.duration_ms,
            "view_offset_ms": e.view_offset_ms,
            "completed": e.completed,
            "season": getattr(e, "season", None),
            "episode": getattr(e, "episode", None),
            "genres": e.genres,
            # rating: not yet in WatchHistoryEntry schema — will be non-None once
            # TMDB ratings are synced; detect_guilty_pleasure stays dormant until then
            "rating": getattr(e, "rating", None),
        }
        for e in entries
    ]


def _completion_rate(e: dict) -> float:
    if e.get("duration_ms") and e["duration_ms"] > 0 and e.get("view_offset_ms"):
        return min(1.0, e["view_offset_ms"] / e["duration_ms"])
    return 1.0 if e.get("completed") else 0.3


# ── TRIGGER DETECTION ─────────────────────────────────────────────────────────

def _series_recently_covered(
    series_key: str, entries: list[dict], asked_subjects: dict | None,
) -> bool:
    """True when we've already sent a proactive message about this series AND
    the user hasn't progressed enough since to warrant asking again.

    This is the "don't ask me the same thing twice" guard the user asked for:
    once the curator has asked how the season-1 ending landed, it stays quiet on
    that series until they reach a new season / finish it (see
    ``series_progress.should_reengage_series``). Legacy messages stored before
    progress milestones existed carry ``None`` — we allow ONE more ask there so a
    real milestone gets captured, then future runs gate properly.
    """
    if not asked_subjects:
        return False
    asked = asked_subjects.get("series") or {}
    key = normalize_title(series_key)
    if key not in asked:
        return False
    last = asked[key]
    if last is None:
        return False
    cur = compute_watch_progress(entries, series_key)
    if not cur:
        return True
    return not should_reengage_series(cur, last)


def _titled_recently_covered(title: str, asked_subjects: dict | None) -> bool:
    """True when we've already sent a proactive message about this titled item
    (a movie, or a deep-history reflection). Unlike a series there's no progress
    to advance — once the curator has asked about it, a growing play count is not
    a new question, so it's one-and-done."""
    if not asked_subjects:
        return False
    return normalize_title(title) in (asked_subjects.get("titles") or set())


def detect_binge(entries: list[dict], now: datetime,
                 asked_subjects: dict | None = None) -> Optional[dict]:
    cutoff = now - timedelta(hours=settings.BINGE_SESSION_HOURS)
    recent = [e for e in entries
              if e["media_type"] in ("show", "anime")
              and e["viewed_at"] and e["viewed_at"] >= cutoff]
    by_series: dict = {}
    for e in recent:
        key = e["series_title"] or e["title"]
        by_series.setdefault(key, []).append(e)
    for series, eps in by_series.items():
        if len(eps) >= settings.BINGE_EPISODE_THRESHOLD:
            if _series_recently_covered(series, entries, asked_subjects):
                continue
            return {
                "type": "binge_episode",
                "series": series,
                "count": len(eps),
                "hours": settings.BINGE_SESSION_HOURS,
                "media_type": eps[0]["media_type"],
            }
    return None


def detect_music_marathon(entries: list[dict], now: datetime) -> Optional[dict]:
    cutoff = now - timedelta(hours=4)
    recent = [e for e in entries
              if e["media_type"] == "music" and e["viewed_at"] and e["viewed_at"] >= cutoff]
    by_artist: dict = {}
    for e in recent:
        artist = e["series_title"] or "Unknown"
        by_artist[artist] = by_artist.get(artist, 0) + (e["duration_ms"] or 200_000)
    for artist, ms in by_artist.items():
        if ms / 3_600_000 >= 3.0:
            return {"type": "music_marathon", "artist": artist, "hours": round(ms / 3_600_000, 1)}
    return None


def detect_series_completion(entries: list[dict], now: datetime,
                             asked_subjects: dict | None = None) -> Optional[dict]:
    cutoff = now - timedelta(hours=48)
    recent = [e for e in entries
              if e["media_type"] in ("show", "anime") and e["viewed_at"] and e["viewed_at"] >= cutoff]
    by_series: dict = {}
    for e in recent:
        key = e["series_title"] or e["title"]
        by_series.setdefault(key, set()).add(e.get("episode"))
    for series, eps in by_series.items():
        if len(eps) >= 8:
            if _series_recently_covered(series, entries, asked_subjects):
                continue
            return {"type": "series_completion", "series": series, "episodes_watched": len(eps)}
    return None


def detect_rewatch(entries: list[dict],
                   asked_subjects: dict | None = None) -> Optional[dict]:
    """A title the user genuinely RE-watched — the basis for a "why do you keep
    coming back?" nudge.

    Pass 57: music is excluded here (song-level replays are
    ``detect_track_obsession``'s job).

    Series-awareness fix: the old code counted every episode of a show as a
    "rewatch" — 12 distinct episodes of Frieren collapsed to "watched 12 times",
    which made the curator ask about "the ending" of a series the user was still
    working through. Now:
      * MOVIES keep the simple rule — the same film played 3+ times is a rewatch;
      * a SERIES only counts when episodes were actually RE-viewed, i.e. plays
        beyond the first pass (``total_plays - distinct_episodes >= 3``) — not
        merely watching many distinct episodes once.
    Subjects already asked about are skipped so the question doesn't repeat.
    """
    # Movies (and any non-series, non-music titled item). Counting rows here
    # would count abandoned starts and resume-duplicates as rewatches, so go
    # through count_real_views (see series_progress for what it filters).
    movie_plays: dict = {}
    for e in entries:
        if e["media_type"] in ("music", "show", "anime"):
            continue
        movie_plays.setdefault(e["series_title"] or e["title"], []).append(e)
    movie_counter: Counter = Counter(
        {title: count_real_views(plays) for title, plays in movie_plays.items()})

    # Distinct series present, newest-first order preserved.
    series_keys: list[str] = []
    seen: set = set()
    for e in entries:
        if e["media_type"] not in ("show", "anime"):
            continue
        key = e["series_title"] or e["title"]
        if key not in seen:
            seen.add(key)
            series_keys.append(key)

    # Collect eligible candidates from both pools; pick the strongest one. The
    # score is the rewatch intensity (play count for movies, episode-replays for
    # series) so the most-rewatched thing wins regardless of media type.
    candidates: list[tuple[int, dict]] = []

    for title, count in movie_counter.most_common(10):
        if count < 3 or _titled_recently_covered(title, asked_subjects):
            continue
        sample = next(
            e for e in entries
            if e["media_type"] not in ("music", "show", "anime")
            and (e["series_title"] or e["title"]) == title
        )
        candidates.append((count, {
            "type": "rewatch",
            "title": title,
            "count": count,
            "media_type": sample["media_type"],
            "genres": sample.get("genres", ""),
            "is_series": False,
        }))

    for key in series_keys:
        prog = compute_watch_progress(entries, key)
        if not prog:
            continue
        # NOT total_plays - distinct_episodes: that counted an episode logged
        # once partially and once finished as a replay, and counted four
        # abandoned starts of episode 1 as three replays.
        replays = prog["replays"]
        if replays < 3 or _series_recently_covered(key, entries, asked_subjects):
            continue
        sample = next(
            e for e in entries
            if e["media_type"] in ("show", "anime")
            and (e["series_title"] or e["title"]) == key
        )
        candidates.append((replays, {
            "type": "rewatch",
            "title": key,
            "count": prog["total_plays"],   # raw rows, for context only
            "replays": replays,
            "media_type": sample["media_type"],
            "genres": sample.get("genres", ""),
            "is_series": True,
        }))

    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def detect_track_obsession(user_id: int,
                           asked_tracks: set | None = None) -> Optional[dict]:
    """A single SONG played far more than the rest — surfaced at song
    granularity, not collapsed to the artist.

    Pass 57: unlike the other detectors this runs a SQL aggregate over
    the user's FULL music history, not the shared 5000-row in-memory
    window. Spotify libraries blow past 5000 plays fast, so a long-tail
    favourite played hundreds of times over years would be undercounted
    (or invisible) in the windowed view.

    Rotation: once the curator has asked "why this song on repeat?", the answer
    doesn't change because the play count climbed higher — so we never re-ask the
    same track. ``asked_tracks`` holds the songs already surfaced; we pull the
    top obsessions and return the first one NOT yet asked about, rotating to the
    next-most-played fresh track each time the trigger fires.

    Returns a hybrid payload: the obsession track + its play count, AND
    the artist's total across all their songs — so the message can pivot
    between "this song specifically" and "this artist generally".
    """
    asked = asked_tracks or set()
    from sqlalchemy import func as _func
    with get_db_session() as db:
        rows = (
            db.query(
                WatchHistoryEntry.title,
                WatchHistoryEntry.series_title,
                _func.count(WatchHistoryEntry.id).label("plays"),
            )
            .filter(
                WatchHistoryEntry.user_id == user_id,
                WatchHistoryEntry.media_type == "music",
                WatchHistoryEntry.title.isnot(None),
            )
            .group_by(WatchHistoryEntry.title, WatchHistoryEntry.series_title)
            .having(_func.count(WatchHistoryEntry.id) >= _TRACK_OBSESSION_THRESHOLD)
            .order_by(_func.count(WatchHistoryEntry.id).desc())
            .limit(25)
            .all()
        )
        if not rows:
            return None
        # First track we haven't already asked about — rotate past the ones we
        # have. If every top obsession has been covered, fire nothing (another
        # trigger gets its turn).
        row = next((r for r in rows if normalize_title(r.title) not in asked), None)
        if row is None:
            return None
        # Artist total across ALL their tracks — the hybrid context that
        # lets the message say "you play this artist a lot, but THIS song
        # is N of those plays".
        artist_total = row.plays
        if row.series_title:
            artist_total = (
                db.query(_func.count(WatchHistoryEntry.id))
                .filter(
                    WatchHistoryEntry.user_id == user_id,
                    WatchHistoryEntry.media_type == "music",
                    WatchHistoryEntry.series_title == row.series_title,
                )
                .scalar()
            ) or row.plays
    return {
        "type": "track_obsession",
        "track": row.title,
        "artist": row.series_title or "an unknown artist",
        "track_plays": row.plays,
        "artist_total": artist_total,
    }


def detect_genre_absence(entries: list[dict], now: datetime) -> Optional[dict]:
    """Loved genre (top-3 historically) not seen in 30+ days."""
    if not entries:
        return None

    # One pass counts overall frequency AND collects the recent set
    # (Jules PR #19) — the split-and-strip work per row happens once
    # instead of twice over a multi-year history.
    genre_counter: Counter = Counter()
    recent_genres: set = set()
    cutoff_30d = now - timedelta(days=30)
    for e in entries:
        genres = e.get("genres")
        if not genres:
            continue
        viewed_at = e.get("viewed_at")
        is_recent = viewed_at and viewed_at >= cutoff_30d
        for g in genres.split(","):
            g = g.strip()
            if g:
                genre_counter[g] += 1
                if is_recent:
                    recent_genres.add(g)

    if not genre_counter:
        return None

    for genre, total in genre_counter.most_common(5):
        if genre not in recent_genres:
            return {
                "type": "genre_absence",
                "genre": genre,
                "total_watched": total,
                "days_absent": 30,
            }
    return None


def detect_low_completion(entries: list[dict], now: datetime) -> Optional[dict]:
    """3+ shows dropped with <30% completion in the last 60 days."""
    cutoff = now - timedelta(days=60)
    dropped = []
    seen_series: set = set()
    for e in entries:
        if e["media_type"] not in ("show", "anime", "movie"):
            continue
        if not (e["viewed_at"] and e["viewed_at"] >= cutoff):
            continue
        rate = _completion_rate(e)
        if rate < 0.3:
            key = e["series_title"] or e["title"]
            if key not in seen_series:
                seen_series.add(key)
                dropped.append({"title": key, "rate": round(rate, 2), "media_type": e["media_type"]})

    if len(dropped) >= 3:
        return {
            "type": "low_completion",
            "dropped": dropped[:5],
            "count": len(dropped),
        }
    return None


def detect_history_deep_dive(entries: list[dict], now: datetime,
                             asked_subjects: dict | None = None) -> Optional[dict]:
    """Pick a random memorable item from history older than 60 days.

    Skips subjects already reflected on (series gated by progress, movies/music
    one-and-done) so the curator rotates to fresh memories instead of dredging
    up the same title. NOTE: ``completed`` here is per-row (a single finished
    episode), so for a series this means "an episode they finished long ago", NOT
    "they finished the series" — the progress phrase attached at send time keeps
    the LLM from assuming an ending.
    """
    old = [e for e in entries
           if e["viewed_at"] and e["viewed_at"] < now - timedelta(days=60)]
    if not old:
        return None

    # Prefer highly rewatched or completed items
    completed = [e for e in old if e.get("completed") or _completion_rate(e) >= 0.9]
    pool = completed if completed else old

    def _covered(e: dict) -> bool:
        key = e["series_title"] or e["title"]
        if e["media_type"] in ("show", "anime"):
            return _series_recently_covered(key, entries, asked_subjects)
        return _titled_recently_covered(key, asked_subjects)

    pool = [e for e in pool if not _covered(e)]
    if not pool:
        return None

    pick = random.choice(pool[:100])
    return {
        "type": "history_deep_dive",
        "title": pick["series_title"] or pick["title"],
        "media_type": pick["media_type"],
        "viewed_at": pick["viewed_at"].isoformat() if pick["viewed_at"] else None,
        "genres": pick.get("genres", ""),
    }


def detect_new_genre(entries: list[dict], now: datetime) -> Optional[dict]:
    """A genre appears in last 30d that wasn't in prior 90d history."""
    cutoff_recent = now - timedelta(days=30)
    cutoff_prior_start = now - timedelta(days=120)
    cutoff_prior_end = now - timedelta(days=30)

    recent_genres: Counter = Counter()
    prior_genres: set = set()

    for e in entries:
        t = e["viewed_at"]
        if not t:
            continue
        for g in (e.get("genres") or "").split(","):
            g = g.strip().lower()
            if not g:
                continue
            if t >= cutoff_recent:
                recent_genres[g] += 1
            elif cutoff_prior_start <= t < cutoff_prior_end:
                prior_genres.add(g)

    for genre, count in recent_genres.most_common():
        if count >= 3 and genre not in prior_genres:
            return {"type": "new_genre", "genre": genre, "count": count}
    return None


def detect_night_owl(entries: list[dict], now: datetime) -> Optional[dict]:
    # Watched something between 2 AM and 5 AM
    cutoff = now - timedelta(days=7)
    recent = [e for e in entries if (e.get("last_viewed_at") or datetime.min) >= cutoff]

    for e in recent:
        view_time = e.get("last_viewed_at")
        if view_time and 2 <= view_time.hour <= 5:
            # Carry artist + media type: a bare music-track title ("Celebrity
            # Skin") was meaningless in the message AND unresolvable in the
            # follow-up chat — the curator had to admit it didn't know what
            # it had flagged.
            title = e.get("title") or "something"
            mt = e.get("media_type")
            artist = (e.get("series_title") or "").strip()
            display = f"{artist} – {title}" if (mt == "music" and artist) else title
            verb = "listening to" if mt == "music" else "watching"
            return {
                "type": "night_owl",
                "trigger_type": "night_owl",
                "media_title": display,
                "title": title,
                "artist": artist or None,
                "media_type": mt,
                "context": f"Up late {verb} {display}",
            }
    return None


def detect_genre_rut(entries: list[dict], now: datetime) -> Optional[dict]:
    """10+ items in the last 14 days, every single one from the exact same primary genre."""
    cutoff = now - timedelta(days=14)
    recent = [
        e for e in entries
        if e["viewed_at"] and e["viewed_at"] >= cutoff
        and e["media_type"] in ("show", "anime", "movie")
    ]
    if len(recent) < 10:
        return None

    genres_seen: set = set()
    for e in recent:
        primary = (e.get("genres") or "").split(",")[0].strip().lower()
        if primary:
            genres_seen.add(primary)

    if len(genres_seen) == 1:
        return {
            "type": "genre_rut",
            "genre": next(iter(genres_seen)),
            "count": len(recent),
        }
    return None


def detect_attention_deficit(entries: list[dict], now: datetime) -> Optional[dict]:
    """The last 4 distinct titles watched were all dropped at <15% completion."""
    # Only consider unique titles in recency order (entries are newest-first)
    seen: set = set()
    recent_4: list[dict] = []
    for e in entries:
        key = e["series_title"] or e["title"]
        if key not in seen and e["media_type"] in ("show", "anime", "movie"):
            seen.add(key)
            recent_4.append(e)
        if len(recent_4) == 4:
            break

    if len(recent_4) < 4:
        return None

    dropped = [e for e in recent_4 if _completion_rate(e) < 0.15]
    if len(dropped) == 4:
        titles = [e["series_title"] or e["title"] for e in dropped[:2]]
        return {"type": "attention_deficit", "titles": titles}
    return None


def detect_procrastinator(entries: list[dict], now: datetime,
                          asked_subjects: dict | None = None) -> Optional[dict]:
    """Series actively watched within the last 7 days but started 90+ days ago, <20 episodes total."""
    by_series: dict = {}
    for e in entries:
        if e["media_type"] not in ("show", "anime"):
            continue
        key = e["series_title"] or e["title"]
        by_series.setdefault(key, []).append(e)

    for series, eps in by_series.items():
        if len(eps) < 3:
            continue
        times = [e["viewed_at"] for e in eps if e["viewed_at"]]
        if not times:
            continue
        first_watch = min(times)
        last_watch  = max(times)
        still_active  = last_watch  >= now - timedelta(days=7)
        started_long  = first_watch <= now - timedelta(days=90)
        if still_active and started_long and len(eps) < 20:
            if _series_recently_covered(series, entries, asked_subjects):
                continue
            return {
                "type": "procrastinator",
                "series": series,
                "days": (last_watch - first_watch).days,
                "episodes": len(eps),
            }
    return None


def detect_guilty_pleasure(entries: list[dict], now: datetime) -> Optional[dict]:
    """Completed something with an objectively terrible TMDB rating (<5.0) in the last 7 days.

    Dormant until WatchHistoryEntry gains a `rating` column populated from TMDB.
    """
    cutoff = now - timedelta(days=7)
    recent = [e for e in entries if e["viewed_at"] and e["viewed_at"] >= cutoff]
    for e in recent:
        rating = e.get("rating")
        if rating is not None and rating < 5.0 and _completion_rate(e) > 0.8:
            return {
                "type": "guilty_pleasure",
                "title": e["series_title"] or e["title"],
                "rating": rating,
                "media_type": e["media_type"],
            }
    return None


# ── TRIGGER RUNNER ────────────────────────────────────────────────────────────

# Canonical list of proactive-message triggers with user-facing copy. Each
# trigger is independently togglable in Settings → Notifications. Order here
# matches priority in _run_all_triggers below.
TRIGGER_TYPES: list[dict] = [
    {"type": "recommendation_followup", "label": "Recommendation follow-up",
     "description": "When you watch something from your Curatarr Recommended playlist, ask how it landed — your verdict feeds back with extra weight."},
    {"type": "rewatch",           "label": "Rewatch suggestions",
     "description": "When you've watched a title several times, surface a thought about coming back to it."},
    {"type": "binge_episode",     "label": "Binge detection",
     "description": "Notice and comment on heavy back-to-back episode watching."},
    {"type": "music_marathon",    "label": "Music marathon",
     "description": "Notice when you spent hours on one artist or album."},
    {"type": "track_obsession",   "label": "Song on repeat",
     "description": "Surface a single song you've played dozens or hundreds of times — at song level, not just the artist."},
    {"type": "series_completion", "label": "Series completion",
     "description": "Acknowledge when you finish an entire series."},
    {"type": "attention_deficit", "label": "Attention deficit",
     "description": "Calls it out when you keep starting new things and dropping them early."},
    {"type": "procrastinator",    "label": "Procrastinator",
     "description": "Reminds you of titles you started months ago and never finished."},
    {"type": "genre_rut",         "label": "Genre rut",
     "description": "Pokes you when you've been deep in one genre for weeks."},
    {"type": "guilty_pleasure",   "label": "Guilty pleasures",
     "description": "Surface low-rated titles you keep coming back to anyway."},
    {"type": "genre_absence",     "label": "Genre absence",
     "description": "Suggest a genre you used to love but haven't touched in a while."},
    {"type": "low_completion",    "label": "Low completion",
     "description": "Observe when you're abandoning more than half of what you start."},
    {"type": "new_genre",         "label": "New genre discovery",
     "description": "Celebrate (or interrogate) when you start exploring a brand-new genre."},
    {"type": "night_owl",         "label": "Night owl",
     "description": "Calls out the late-night-watching pattern when it shows up."},
    {"type": "history_deep_dive", "label": "Deep-history reflection",
     "description": "Occasionally pull a memorable old play forward for reflection."},
]
TRIGGER_TYPE_NAMES: set[str] = {t["type"] for t in TRIGGER_TYPES}


def get_disabled_triggers(user_id: int) -> set[str]:
    """Return the set of trigger types this user has switched off in Settings.

    Stored as JSON in app_state under the per-user key
    ``notif_disabled:user_id=<id>``. Empty / missing = all triggers enabled.
    """
    from src.services.app_state import get_state
    raw = get_state(f"notif_disabled:user_id={user_id}")
    if not raw:
        return set()
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return {str(x) for x in items if isinstance(x, str)}
    except Exception:
        pass
    return set()


def set_disabled_triggers(user_id: int, disabled: set[str]) -> None:
    """Persist the set of disabled trigger types for ``user_id``."""
    from src.services.app_state import set_state
    # Drop unknown trigger names so a stale UI can't poison the storage.
    cleaned = sorted(t for t in disabled if t in TRIGGER_TYPE_NAMES)
    set_state(f"notif_disabled:user_id={user_id}", json.dumps(cleaned))


def detect_recommendation_followup(user_id: int,
                                   asked_subjects: dict | None = None) -> Optional[dict]:
    """The user watched something the curator put on their Curatarr-
    Recommended playlist (queued by plex_sync._process_rec_watch_hits).
    Returns the OLDEST hit not yet asked about.

    PEEKS, never consumes: de-dup comes from asked_subjects (the message we
    send indexes the title), so a failed LLM generation can't lose the hit;
    stale hits age out via the queue's 30-day prune. Feedback given in the
    follow-up thread is stored with elevated weight — this is the highest-
    value trigger, which is why it runs FIRST in the candidates list."""
    from src.services.app_state import get_state
    try:
        queue = json.loads(get_state(f"rec_watch_hits:user_id={user_id}") or "[]")
    except Exception:
        return None
    asked = asked_subjects or {}
    asked_titles = asked.get("titles") or set()
    asked_series = asked.get("series") or {}
    for hit in queue:                      # oldest first
        title = hit.get("title") or ""
        n = normalize_title(title)
        if not n or n in asked_titles or n in asked_series:
            continue
        return {
            "type": "recommendation_followup",
            "trigger_type": "recommendation_followup",
            "title": title,
            "category": hit.get("category"),
            "rec_id": hit.get("rec_id"),
            "watched_at": hit.get("watched_at"),
        }
    return None


def _run_all_triggers(entries: list[dict], now: datetime, user_id: int,
                      recently_fired: set[str],
                      disabled: set[str] | None = None,
                      asked_subjects: dict | None = None) -> Optional[dict]:
    """
    Try each trigger in priority order, skip types that fired recently
    or that the user has disabled in their notification preferences.

    Pass 57: ``user_id`` is now threaded through because
    ``detect_track_obsession`` runs its own SQL aggregate over the full
    history rather than the shared ``entries`` window.

    ``asked_subjects`` (tracks / titles / series we've already messaged about)
    is threaded into the subject-bearing detectors so they rotate to fresh
    subjects instead of repeating a song / series the user already answered on.
    """
    disabled = disabled or set()
    _tracks = (asked_subjects or {}).get("tracks")
    candidates = [
        # FIRST on purpose: the runner returns the first hit, and a watched-
        # recommendation follow-up (elevated-weight feedback) outranks
        # rewatch/binge chatter. The shared 1-day type cooldown paces it to
        # max one per day — deliberate; the hit queue holds the rest.
        ("recommendation_followup",
         lambda: detect_recommendation_followup(user_id, asked_subjects)),
        ("rewatch",           lambda: detect_rewatch(entries, asked_subjects)),
        ("track_obsession",   lambda: detect_track_obsession(user_id, _tracks)),
        ("binge_episode",     lambda: detect_binge(entries, now, asked_subjects)),
        ("music_marathon",    lambda: detect_music_marathon(entries, now)),
        ("series_completion", lambda: detect_series_completion(entries, now, asked_subjects)),
        ("attention_deficit", lambda: detect_attention_deficit(entries, now)),
        ("procrastinator",    lambda: detect_procrastinator(entries, now, asked_subjects)),
        ("genre_rut",         lambda: detect_genre_rut(entries, now)),
        ("guilty_pleasure",   lambda: detect_guilty_pleasure(entries, now)),
        ("genre_absence",     lambda: detect_genre_absence(entries, now)),
        ("low_completion",    lambda: detect_low_completion(entries, now)),
        ("new_genre",         lambda: detect_new_genre(entries, now)),
        ("night_owl",         lambda: detect_night_owl(entries, now)),
        ("history_deep_dive", lambda: detect_history_deep_dive(entries, now, asked_subjects)),
    ]
    for ttype, fn in candidates:
        if ttype in recently_fired or ttype in disabled:
            continue
        result = fn()
        if result:
            return result
    return None


# ── SUBJECT MEMORY (don't repeat the same question) ───────────────────────────

# Subject-bearing trigger types and where their subject title lives in the
# trigger payload. Used to index what we've already asked about.
_SERIES_TRIGGER_TYPES = {"binge_episode", "series_completion", "procrastinator"}


def _handle_rewatch_deep_dive(td: dict, titles: set, series: dict) -> None:
    title = td.get("title")
    if not title:
        return
    if td.get("is_series") or td.get("media_type") in ("show", "anime"):
        series.setdefault(normalize_title(title), td.get("milestone"))
    else:
        titles.add(normalize_title(title))


def _handle_recommendation_followup(td: dict, titles: set, series: dict) -> None:
    # once asked about a recommended title, never re-ask it — the
    # detector peeks its queue and relies on THIS index for de-dup
    title = td.get("title")
    if not title:
        return
    if td.get("category") in ("show", "anime"):
        series.setdefault(normalize_title(title), None)
    else:
        titles.add(normalize_title(title))


def _load_asked_subjects(user_id: int, limit: int = 400) -> dict:
    """Index WHAT the curator has already asked this user about, from the
    proactive-message history, so generation rotates to fresh subjects.

      tracks  — normalized song titles (track_obsession): never re-ask a song.
      titles  — normalized movie / deep-dive titles: never re-ask (no progress).
      series  — normalized series key -> the progress ``milestone`` we asked at
                (or None for pre-milestone messages); re-ask only once advanced.

    Most-recent message wins (``setdefault`` over a newest-first scan), so the
    stored milestone reflects the last position we asked about.
    """
    tracks: set = set()
    titles: set = set()
    series: dict = {}

    with get_db_session() as db:
        rows = (
            db.query(ProactiveMessage.trigger_type, ProactiveMessage.trigger_data)
            .filter(ProactiveMessage.user_id == user_id)
            .order_by(ProactiveMessage.created_at.desc())
            .limit(limit)
            .all()
        )

    for ttype, tdata_raw in rows:
        try:
            td = json.loads(tdata_raw) if tdata_raw else {}
        except (ValueError, TypeError):
            continue

        if not isinstance(td, dict):
            continue

        if ttype == "track_obsession" and td.get("track"):
            tracks.add(normalize_title(td["track"]))
            continue

        if ttype in ("rewatch", "history_deep_dive"):
            _handle_rewatch_deep_dive(td, titles, series)
            continue

        if ttype in _SERIES_TRIGGER_TYPES and td.get("series"):
            series.setdefault(normalize_title(td["series"]), td.get("milestone"))
            continue

        if ttype == "recommendation_followup":
            _handle_recommendation_followup(td, titles, series)
            continue

    return {"tracks": tracks, "titles": titles, "series": series}


def _series_key_of(trigger: dict) -> Optional[str]:
    """The series title a trigger is about, or None if it isn't a series subject."""
    if trigger["type"] in _SERIES_TRIGGER_TYPES:
        return trigger.get("series")
    if trigger["type"] in ("rewatch", "history_deep_dive"):
        if trigger.get("is_series") or trigger.get("media_type") in ("show", "anime"):
            return trigger.get("title")
    return None


# Sonarr season-statistics index, fetched once per generation run and cached
# briefly so a multi-user scheduler sweep reuses it.
_SONARR_INDEX_TTL_S = 300.0
_sonarr_index_cache: dict = {"at": None, "data": None}


def _sonarr_series_stats(s: dict) -> Optional[dict]:
    """Reduce a Sonarr series object to the per-season AIRED episode counts we
    need. Season 0 (specials) is excluded. Returns None if nothing usable."""
    per_season: dict = {}
    for se in (s.get("seasons") or []):
        num = se.get("seasonNumber")
        if num is None or num < 1:
            continue
        st = se.get("statistics") or {}
        aired = st.get("episodeCount")
        if aired is None:
            aired = st.get("totalEpisodeCount") or 0
        per_season[int(num)] = int(aired or 0)
    if not per_season:
        return None
    return {
        "title": s.get("title"),
        "tvdb_id": s.get("tvdbId"),
        "series_type": s.get("seriesType"),
        "status": s.get("status"),
        "total_seasons": sum(1 for v in per_season.values() if v > 0),
        "total_episodes": sum(per_season.values()),
        "per_season": per_season,
    }


async def _get_sonarr_index() -> dict:
    """Build (and briefly cache) an index of Sonarr season statistics, keyed by
    tvdbId and by normalized title, for marrying with the Plex watch history.

    Returns ``{"by_tvdb": {id: stats}, "by_title": {norm: [stats]}}``. Empty on
    any failure (Sonarr down / unconfigured) — series progress then degrades to
    watch-data-only framing."""
    now_ts = datetime.utcnow()
    cached = _sonarr_index_cache
    if (cached["data"] is not None and cached["at"] is not None
            and (now_ts - cached["at"]).total_seconds() < _SONARR_INDEX_TTL_S):
        return cached["data"]

    index: dict = {"by_tvdb": {}, "by_title": {}}
    try:
        from src.services.arr_client import SonarrClient
        url, key = settings.SONARR_URL, settings.SONARR_API_KEY
        if url and key:
            client = SonarrClient(url, key)
            async with client:
                series = await client.get_series()
            for s in (series or []):
                stats = _sonarr_series_stats(s)
                if not stats:
                    continue
                tv = s.get("tvdbId")
                if tv:
                    index["by_tvdb"][int(tv)] = stats
                nt = normalize_title(s.get("title"))
                if nt:
                    index["by_title"].setdefault(nt, []).append(stats)
            logger.debug("[proactive] Sonarr index: %d series by tvdb",
                         len(index["by_tvdb"]))
    except Exception as e:
        logger.debug("[proactive] Sonarr index build failed: %s", e)
    _sonarr_index_cache.update(at=now_ts, data=index)
    return index


def _resolve_sonarr_stats(sonarr_index: dict | None, tvdb_id,
                          series_title: str, media_type: str) -> Optional[dict]:
    """Match a watched series to its Sonarr stats.

    Title-first: Plex and Sonarr both name series from TVDB metadata, so titles
    align in practice — whereas ``MediaIdentity.tvdb_id`` has proven NOT to be
    Sonarr's ``tvdbId`` (different id namespace), so using it as the primary key
    silently mismatched. Collisions on a normalized title (Death Note 2006 anime
    vs the 2015 live-action) are disambiguated by anime-vs-show. tvdbId is kept
    only as a last-resort fallback for genuinely renamed / translated titles."""
    if not sonarr_index:
        return None
    cands = (sonarr_index.get("by_title") or {}).get(normalize_title(series_title)) or []
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        for st in cands:
            cat = "anime" if st.get("series_type") == "anime" else "show"
            if cat == media_type:
                return st
        return cands[0]
    # No title match — fall back to tvdbId (renamed/translated titles only).
    if tvdb_id:
        try:
            return (sonarr_index.get("by_tvdb") or {}).get(int(tvdb_id))
        except (TypeError, ValueError):
            return None
    return None


def _attach_series_progress(trigger: dict, user_id: int,
                            sonarr_index: dict | None = None) -> None:
    """For series-bearing triggers, resolve full progress and stamp the trigger
    with a human ``phrase``, the ``milestone`` (persisted in trigger_data for
    future de-dup) and ``finished`` — so the prompt frames to the user's actual
    position instead of assuming an ending.

    Progress is computed from the user's COMPLETE history for this series via a
    single targeted query — NOT the in-memory 5000-row window. For a music-heavy
    user that window is dominated by Spotify plays and holds only the latest
    episode or two of any series, which would mislabel "finished season 1, now in
    season 2" as "only watched one episode". Season/episode totals come from the
    Sonarr index (matched by tvdbId via MediaIdentity, title fallback)."""
    series_key = _series_key_of(trigger)
    if not series_key:
        return
    mt = trigger.get("media_type") or "show"
    try:
        from sqlalchemy import or_ as _or, and_ as _and
        from src.database.models import MediaIdentity
        tvdb_id = None
        with get_db_session() as db:
            rows = (
                db.query(WatchHistoryEntry)
                .filter(
                    WatchHistoryEntry.user_id == user_id,
                    WatchHistoryEntry.media_type.in_(("show", "anime")),
                    _or(
                        WatchHistoryEntry.series_title == series_key,
                        _and(
                            WatchHistoryEntry.series_title.is_(None),
                            WatchHistoryEntry.title == series_key,
                        ),
                    ),
                )
                .all()
            )
            # Materialise to plain dicts WHILE the session is open — the rows are
            # detached once the `with` closes.
            full = _to_dicts(rows)
            mi = (
                db.query(MediaIdentity.tvdb_id)
                .filter(
                    MediaIdentity.title == series_key,
                    MediaIdentity.media_type == mt,
                )
                .first()
            )
            if mi:
                tvdb_id = mi[0]
        sonarr_stats = _resolve_sonarr_stats(sonarr_index, tvdb_id, series_key, mt)
        prog = get_series_progress(
            full, series_key, mt, sonarr=sonarr_stats, now=datetime.utcnow(),
        )
    except Exception as e:
        logger.debug("[proactive] progress attach failed for %r: %s", series_key, e)
        return
    if not prog:
        return
    trigger["progress_phrase"] = prog["phrase"]
    trigger["milestone"] = progress_milestone(prog)
    trigger["finished"] = prog["finished"]
    if prog.get("total_episodes"):
        trigger["episodes_total"] = prog["total_episodes"]
    if prog.get("furthest_season") is not None:
        trigger["furthest_season"] = prog["furthest_season"]
    if prog.get("furthest_episode") is not None:
        trigger["furthest_episode"] = prog["furthest_episode"]


def _mark_asked(asked_subjects: dict, trigger: dict) -> None:
    """Record a just-generated subject in the in-memory index so the NEXT slot in
    the same generation run doesn't pick it again."""
    t = trigger["type"]
    if t == "track_obsession":
        if trigger.get("track"):
            asked_subjects["tracks"].add(normalize_title(trigger["track"]))
        return
    series_key = _series_key_of(trigger)
    if series_key:
        asked_subjects["series"][normalize_title(series_key)] = trigger.get("milestone")
    elif t in ("rewatch", "history_deep_dive") and trigger.get("title"):
        asked_subjects["titles"].add(normalize_title(trigger["title"]))


# ── MESSAGE GENERATION ────────────────────────────────────────────────────────

_PROVOCATIVE_SUFFIXES = [
    " Don't hold back.",
    " Be brutally honest.",
    " Skip the pleasantries.",
    " No filter.",
]


async def generate_proactive_message(
    trigger: dict, taste_blurb: str, lang_directive: str = "",
) -> Optional[str]:
    """Generate a provocative, personalised proactive message via LLM.

    Pass 40: ``lang_directive`` is a 1-line string from
    ``llm_utils.language_directive(...)`` injected into the system
    prompt. Empty default keeps backwards compatibility for any internal
    callers that don't yet pass it.
    """
    t = trigger["type"]
    taste = f"\nUSER TASTE CONTEXT:\n{taste_blurb[:400]}" if taste_blurb else ""

    # Series-progress framing. When the trigger carries a progress phrase
    # (attached by ``_attach_series_progress``), hand it to the LLM so it asks
    # about the user's ACTUAL position instead of assuming they reached the end.
    prog_phrase = trigger.get("progress_phrase")
    prog_line = (
        f"\nIMPORTANT — where the user actually is in this series: {prog_phrase}. "
        f"Only ask about the ending, their final verdict, or a rewatch if they have "
        f"FINISHED it; otherwise ask about their current point, never the ending.\n"
    ) if prog_phrase else ""

    if t == "recommendation_followup":
        prompt = (
            f"You are Curatarr, an opinionated personal curator. "
            f"You put \"{trigger['title']}\" ({trigger.get('category', 'title')}) on the "
            f"user's Curatarr Recommended playlist — and they actually watched into it."
            f"{taste}\n\n"
            f"Write ONE short message (max 2 sentences): you recommended it, they tried "
            f"it — ask how it landed. Direct, curious, opinionated; invite a real "
            f"verdict, good or bad."
        )

    elif t == "binge_episode":
        prompt = (
            f"You are Curatarr, an opinionated personal curator. "
            f"The user just watched {trigger['count']} episodes of \"{trigger['series']}\" "
            f"in {trigger['hours']} hours straight.{taste}{prog_line}\n\n"
            f"Write a single short message (max 2 sentences): curious, slightly teasing, maybe provocative. "
            f"Ask something specific about the show or their reaction. Reference their taste if relevant."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "music_marathon":
        prompt = (
            f"The user just listened to {trigger['hours']}h of {trigger['artist']} non-stop.{taste}\n\n"
            f"Write ONE short, direct message. Ask what's going on — mood, obsession, nostalgia? "
            f"Be curious and slightly provocative."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "series_completion":
        prompt = (
            f"The user just powered through {trigger['episodes_watched']} episodes of "
            f"\"{trigger['series']}\" in record time.{taste}{prog_line}\n\n"
            f"Write a short, provocative message. Ask if they finally finished it, or if they just lost "
            f"control of their life this weekend. Be direct and slightly teasing. Max 2 sentences."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "rewatch":
        if trigger.get("is_series"):
            # Series: count is play count, not "times watched". Frame on the
            # genuine episode-replays so we never say "watched it 31 times".
            replays = trigger.get("replays", "several")
            prompt = (
                f"The user keeps going back to RE-watch episodes of "
                f"\"{trigger['title']}\" (a {trigger['media_type']}) — "
                f"{replays} episode-replays beyond a first watch.{taste}{prog_line}\n\n"
                f"Write one provocative question about WHY this one specifically pulls "
                f"them back for repeat viewings — comfort, a character, a particular "
                f"scene or episode? Be direct, a little cheeky. Max 2 sentences."
                + random.choice(_PROVOCATIVE_SUFFIXES)
            )
        else:
            prompt = (
                f"The user has watched \"{trigger['title']}\" {trigger['count']} times total. "
                f"It's a {trigger['media_type']}.{taste}\n\n"
                f"Write one provocative question about WHY they keep coming back. "
                f"Is it comfort? A specific character? Nostalgia? Fan service? "
                f"Be direct, maybe a little cheeky. Max 2 sentences."
                + random.choice(_PROVOCATIVE_SUFFIXES)
            )

    elif t == "track_obsession":
        # Pass 57: hybrid framing — lead with the SONG, but hand the LLM
        # the artist total so it can pivot between "this track" and "this
        # artist". The whole point of this trigger is song-level detail,
        # so the prompt explicitly steers away from generic artist talk.
        _track  = trigger["track"]
        _artist = trigger["artist"]
        _tp     = trigger["track_plays"]
        _at     = trigger["artist_total"]
        _ctx = f"\"{_track}\" by {_artist} — played {_tp} times."
        if _at and _at > _tp:
            _ctx += (
                f" (You play {_artist} a lot — {_at} plays across all their songs — "
                f"but THIS one track is {_tp} of them.)"
            )
        prompt = (
            f"The user has a specific song on heavy repeat: {_ctx}{taste}\n\n"
            f"Write ONE short, direct question about THIS SONG specifically — "
            f"not the artist in general. What is it about this exact track? A "
            f"mood it locks in, a memory attached to it, a hook they can't "
            f"shake? Be curious and a little provocative. Max 2 sentences."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "genre_absence":
        prompt = (
            f"The user loves {trigger['genre']} content (watched it {trigger['total_watched']} times) "
            f"but hasn't watched anything in that genre for {trigger['days_absent']}+ days.{taste}\n\n"
            f"Write one short message noticing the absence. Is it a mood thing? Burned out? "
            f"Suggest they might be missing it. Be warm but slightly provocative. Max 2 sentences."
        )

    elif t == "low_completion":
        dropped_titles = ", ".join(f"\"{d['title']}\"" for d in trigger["dropped"][:3])
        prompt = (
            f"The user abandoned {trigger['count']} shows recently without finishing them: "
            f"{dropped_titles}.{taste}\n\n"
            f"Write a direct, slightly confrontational question about why. "
            f"Are these bad picks? Wrong mood? Too much commitment required? "
            f"Max 2 sentences. Reference specific titles."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "history_deep_dive":
        ago = ""
        if trigger.get("viewed_at"):
            try:
                dt = datetime.fromisoformat(trigger["viewed_at"])
                days = (datetime.utcnow() - dt).days
                ago = f" about {days} days ago"
            except Exception:
                pass
        prompt = (
            f"Looking back at the user's watch history, they watched \"{trigger['title']}\" "
            f"({trigger['media_type']}, genres: {trigger['genres'] or 'unknown'}){ago}.{taste}{prog_line}\n\n"
            f"Write one short, curious message asking if they still think about it, "
            f"or how their opinion has changed. Make it feel like genuine curiosity, "
            f"not a questionnaire. Max 2 sentences."
        )

    elif t == "new_genre":
        prompt = (
            f"The user has recently started watching {trigger['count']} things "
            f"in the \"{trigger['genre']}\" genre — this genre barely appeared in their "
            f"history before.{taste}\n\n"
            f"Write a short message noticing this shift. Are they exploring something new? "
            f"Going through something? Be curious and direct. Max 2 sentences."
        )

    elif t == "night_owl":
        prompt = (
            f"The user has been watching things late at night: \"{trigger['media_title']}\". "
            f"Context: {trigger['context']}.{taste}\n\n"
            f"Write one short message about this pattern. Insomnia? Comfort watching? "
            f"Something they wouldn't watch with others? Be curious, slightly teasing. Max 2 sentences."
        )

    elif t == "genre_rut":
        prompt = (
            f"The user has watched {trigger['count']} things in the last two weeks, and literally "
            f"EVERY SINGLE ONE was in the '{trigger['genre']}' genre.{taste}\n\n"
            f"Write a short, confrontational message. Are they hiding in a comfort zone? "
            f"Do they need background noise that doesn't challenge them? "
            f"Call out this obsessive streak. Max 2 sentences."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "attention_deficit":
        prompt = (
            f"The user just started and immediately quit 4 different things in a row "
            f"(including {', '.join(trigger['titles'])}), barely making it past the 10-minute mark "
            f"on any of them.{taste}\n\n"
            f"Write a sharp, direct message. Is everything they pick garbage, or is their attention "
            f"span just completely fried today? Max 2 sentences."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "procrastinator":
        prompt = (
            f"The user has been dragging out watching \"{trigger['series']}\". "
            f"They started it {trigger['days']} days ago but have only managed to watch "
            f"{trigger['episodes']} episodes.{taste}{prog_line}\n\n"
            f"Write a short, teasing message. Why are they forcing themselves to finish it? "
            f"If it was actually good, they would have binged it by now. "
            f"Tell them it's okay to drop it. Max 2 sentences."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "guilty_pleasure":
        prompt = (
            f"The user just watched \"{trigger['title']}\" all the way through, even though it has "
            f"a terrible global rating of {trigger['rating']}/10.{taste}\n\n"
            f"Write a highly provocative message. Are they hate-watching this? "
            f"Is it a secret trash kink? Call out the horrible quality of the content. Max 2 sentences."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    else:
        return None

    # NAME THE SUBJECT — appended to every trigger prompt. A message that only
    # gestures at a mood ("a bit of whimsical escapism?") is unanswerable: the
    # user asked "what exactly are we talking about?" and the follow-up chat
    # couldn't say either. The concrete entity is what makes the ping useful.
    prompt += (
        "\nIMPORTANT: NAME the specific title/artist/track this message is "
        "about, verbatim, inside the message itself — never allude to it "
        "only as a mood or category."
    )

    # Proactive messages use the big curator model — route the generation
    # through the curator gate so a scheduled message can't collide with a
    # user's chat on the single GPU (it queues for the slot like the rest).
    from src.services.llm_priority import curator_priority
    async with curator_priority("proactive message"):
        for model in [settings.CURATOR_MODEL, settings.BASE_CURATOR_MODEL]:
            if not model:
                continue
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{settings.effective_ollama}/api/chat",
                        json={
                            "model": model,
                            "messages": [
                                {"role": "system", "content": _build_system_prompt(
                                    lang_directive or "Respond in English."
                                )},
                                {"role": "user", "content": prompt},
                            ],
                            "stream": False,
                            "keep_alive": CURATOR_KEEP_ALIVE,
                            **curator_options(temperature=0.85, num_predict=800),
                        },
                    )
                if resp.status_code == 200:
                    content = strip_think_tags(
                        resp.json().get("message", {}).get("content", "").strip()
                    )
                    if content:
                        return content
                    # Empty content — try the fallback model rather than store an empty message.
                    logger.debug("Proactive message empty response from %s, trying fallback", model)
                    continue
                # Any non-200 (404, 500, 502, 503, …) → try the next model.
                logger.debug("Proactive message HTTP %s from %s, trying fallback",
                             resp.status_code, model)
                continue
            except Exception as e:
                # Includes timeouts, connection errors — try the next model.
                logger.warning("Proactive message LLM failed (%s): %s", model, e)

    return None


# ── MAIN SCHEDULER FUNCTION ───────────────────────────────────────────────────

async def check_and_generate_messages(user_id: int) -> int:
    """
    Fill the unread message cache up to _CACHE_TARGET.
    Called by the background scheduler every 30 min.
    Returns number of new messages generated.
    """
    now = datetime.utcnow()

    with get_db_session() as db:
        user_obj = db.query(User).filter(User.id == user_id, User.is_active == True).first()
        if not user_obj:
            return 0

        # Count unread cached messages — don't generate more if cache is full
        unread_count = db.query(ProactiveMessage).filter(
            ProactiveMessage.user_id == user_id,
            ProactiveMessage.read == False,
        ).count()

        if unread_count >= _CACHE_TARGET:
            logger.debug("[proactive] Cache full (%d unread), skipping generation", unread_count)
            return 0

        # Which trigger types fired recently (avoid same trigger repeating)?
        cooldown_cutoff = now - timedelta(days=_TRIGGER_COOLDOWN_DAYS)
        recent_triggers = {
            r[0]
            for r in db.query(ProactiveMessage.trigger_type)
            .filter(
                ProactiveMessage.user_id == user_id,
                ProactiveMessage.created_at >= cooldown_cutoff,
            )
            .all()
        }

        tv = db.query(TasteVectorEntry).filter(TasteVectorEntry.user_id == user_id).first()
        taste_blurb = (tv.summary_text or "") if tv else ""

        entries_raw = (
            db.query(WatchHistoryEntry)
            .filter(WatchHistoryEntry.user_id == user_id)
            .order_by(WatchHistoryEntry.viewed_at.desc())
            .limit(5000)
            .all()
        )
        entries = _to_dicts(entries_raw)

    if not entries:
        return 0

    # Read user's per-trigger notification preferences once before the loop.
    disabled_triggers = get_disabled_triggers(user_id)
    if disabled_triggers:
        logger.debug("[proactive] User %d has %d trigger(s) disabled: %s",
                     user_id, len(disabled_triggers), sorted(disabled_triggers))

    # What have we already asked about? Index it so the generator rotates to
    # fresh subjects instead of repeating a song / series already answered.
    asked_subjects = _load_asked_subjects(user_id)

    # Sonarr season/episode totals, fetched once per run (TTL-cached across the
    # scheduler sweep) and married with the Plex watch history for "season X of
    # Y" framing. Empty on any failure -> watch-data-only framing.
    sonarr_index = await _get_sonarr_index()

    slots = min(_CACHE_TARGET - unread_count, _MAX_PER_RUN)
    generated = 0

    for _ in range(slots):
        trigger = _run_all_triggers(
            entries, now, user_id,
            recently_fired=recent_triggers,
            disabled=disabled_triggers,
            asked_subjects=asked_subjects,
        )
        if not trigger:
            break

        # Frame series triggers to the user's actual position and stamp the
        # progress milestone (persisted in trigger_data for future de-dup).
        _attach_series_progress(trigger, user_id, sonarr_index=sonarr_index)

        logger.info("[proactive] Trigger '%s' for user %d", trigger["type"], user_id)
        # Pass 40: detect the user's chat language so the proactive nudge
        # arrives in the same language they've been writing in. Falls back
        # to English when no recent user chat exists.
        from src.services.llm_utils import detect_user_language, language_directive
        with get_db_session() as _ld_db:
            lang = detect_user_language(user_id, _ld_db)
        directive = language_directive(lang)
        message = await generate_proactive_message(trigger, taste_blurb, lang_directive=directive)
        if not message:
            recent_triggers.add(trigger["type"])
            continue

        with get_db_session() as db:
            db.add(ProactiveMessage(
                user_id=user_id,
                trigger_type=trigger["type"],
                trigger_data=json.dumps(trigger, default=str),
                message=message,
                read=False,
                created_at=now + timedelta(seconds=generated),
                # A week is the whole shelf life: a nudge about last
                # weekend's binge is stale by the next one, and the same
                # trigger can re-fire fresh after the cooldown anyway.
                expires_at=now + timedelta(days=7),
            ))
            db.commit()

        recent_triggers.add(trigger["type"])
        _mark_asked(asked_subjects, trigger)
        generated += 1

    if generated:
        logger.info("[proactive] Generated %d messages for user %d", generated, user_id)
    return generated


async def get_unread_messages(user_id: int) -> dict:
    """
    Returns the next unread message and the total unread count.
    Only one message is surfaced at a time — the user reads or skips it,
    then the next one becomes visible. Skipping (mark_message_read) removes
    it from the queue; the same trigger type can re-fire after the cooldown.
    """
    now = datetime.utcnow()
    with get_db_session() as db:
        # Ignored bubbles decay: a message past its TTL, or surfaced past
        # the impression cap without ever being clicked, is retired (marked
        # read) instead of squatting at the head of the queue for days —
        # the owner's words: the same bubble he hasn't clicked in three
        # days serves nobody.
        stale = (
            db.query(ProactiveMessage)
            .filter(ProactiveMessage.user_id == user_id,
                    ProactiveMessage.read == False)
            .filter((ProactiveMessage.expires_at != None)
                    & (ProactiveMessage.expires_at < now)
                    | (ProactiveMessage.impressions >= 40))
            .all()
        )
        for m in stale:
            m.read = True
        if stale:
            db.commit()

        all_unread = (
            db.query(ProactiveMessage)
            .filter(ProactiveMessage.user_id == user_id, ProactiveMessage.read == False)
            .order_by(ProactiveMessage.created_at.asc())
            .all()
        )
        total = len(all_unread)
        if not all_unread:
            return {"message": None, "total": 0}

        m = all_unread[0]
        m.impressions = (m.impressions or 0) + 1
        db.commit()
        return {
            "message": {
                "id": m.id,
                "message": m.message,
                "trigger_type": m.trigger_type,
                "created_at": m.created_at.isoformat(),
            },
            "total": total,
        }


async def mark_message_read(message_id: int, user_id: int) -> bool:
    with get_db_session() as db:
        msg = db.query(ProactiveMessage).filter(
            ProactiveMessage.id == message_id,
            ProactiveMessage.user_id == user_id,
        ).first()
        if msg:
            msg.read = True
            db.commit()
            return True
    return False
