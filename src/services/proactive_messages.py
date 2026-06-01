"""
Curatarr 1.0 - Proactive Messaging Service

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
from src.services.llm_utils import strip_think_tags, ollama_options, CURATOR_KEEP_ALIVE

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

def detect_binge(entries: list[dict], now: datetime) -> Optional[dict]:
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


def detect_series_completion(entries: list[dict], now: datetime) -> Optional[dict]:
    cutoff = now - timedelta(hours=48)
    recent = [e for e in entries
              if e["media_type"] in ("show", "anime") and e["viewed_at"] and e["viewed_at"] >= cutoff]
    by_series: dict = {}
    for e in recent:
        key = e["series_title"] or e["title"]
        by_series.setdefault(key, set()).add(e.get("episode"))
    for series, eps in by_series.items():
        if len(eps) >= 8:
            return {"type": "series_completion", "series": series, "episodes_watched": len(eps)}
    return None


def detect_rewatch(entries: list[dict]) -> Optional[dict]:
    """Item watched 3+ times across all history.

    Pass 57: music is excluded here. It was keyed on ``series_title``
    (= the artist for music rows), so "137 plays of Deftones" collapsed
    every Deftones song into one count and hid which TRACK was actually
    on repeat. Song-level music replays are now handled by
    ``detect_track_obsession`` instead.
    """
    counter: Counter = Counter()
    for e in entries:
        if e["media_type"] == "music":
            continue
        key = e["series_title"] or e["title"]
        counter[key] += 1
    for title, count in counter.most_common(5):
        if count >= 3:
            # Pick the most-rewatched item — explicitly non-music so a
            # same-named artist can't shadow the real show/movie row.
            sample = next(
                e for e in entries
                if e["media_type"] != "music"
                and (e["series_title"] or e["title"]) == title
            )
            return {
                "type": "rewatch",
                "title": title,
                "count": count,
                "media_type": sample["media_type"],
                "genres": sample.get("genres", ""),
            }
    return None


def detect_track_obsession(user_id: int) -> Optional[dict]:
    """A single SONG played far more than the rest — surfaced at song
    granularity, not collapsed to the artist.

    Pass 57: unlike the other detectors this runs a SQL aggregate over
    the user's FULL music history, not the shared 5000-row in-memory
    window. Spotify libraries blow past 5000 plays fast, so a long-tail
    favourite played hundreds of times over years would be undercounted
    (or invisible) in the windowed view.

    Returns a hybrid payload: the obsession track + its play count, AND
    the artist's total across all their songs — so the message can pivot
    between "this song specifically" and "this artist generally".
    """
    from sqlalchemy import func as _func
    with get_db_session() as db:
        row = (
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
            .first()
        )
        if not row:
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

    # Count genre frequency across all history
    genre_counter: Counter = Counter()
    for e in entries:
        for g in (e.get("genres") or "").split(","):
            g = g.strip()
            if g:
                genre_counter[g] += 1

    if not genre_counter:
        return None

    top_genres = [g for g, _ in genre_counter.most_common(5)]
    cutoff_30d = now - timedelta(days=30)

    # Find which of those genres appeared in recent 30d
    recent_genres: set = set()
    for e in entries:
        if e["viewed_at"] and e["viewed_at"] >= cutoff_30d:
            for g in (e.get("genres") or "").split(","):
                g = g.strip()
                if g:
                    recent_genres.add(g)

    for genre in top_genres:
        if genre not in recent_genres:
            total = genre_counter[genre]
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


def detect_history_deep_dive(entries: list[dict], now: datetime) -> Optional[dict]:
    """Pick a random memorable item from history older than 60 days."""
    old = [e for e in entries
           if e["viewed_at"] and e["viewed_at"] < now - timedelta(days=60)]
    if not old:
        return None

    # Prefer highly rewatched or completed items
    completed = [e for e in old if e.get("completed") or _completion_rate(e) >= 0.9]
    pool = completed if completed else old
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
            return {
                "type": "night_owl",
                "trigger_type": "night_owl",
                "media_title": e.get("title", "something"),
                "context": f"Up late watching {e.get('title', 'something')}"
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


def detect_procrastinator(entries: list[dict], now: datetime) -> Optional[dict]:
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


def _run_all_triggers(entries: list[dict], now: datetime, user_id: int,
                      recently_fired: set[str],
                      disabled: set[str] | None = None) -> Optional[dict]:
    """
    Try each trigger in priority order, skip types that fired recently
    or that the user has disabled in their notification preferences.

    Pass 57: ``user_id`` is now threaded through because
    ``detect_track_obsession`` runs its own SQL aggregate over the full
    history rather than the shared ``entries`` window.
    """
    disabled = disabled or set()
    candidates = [
        ("rewatch",           lambda: detect_rewatch(entries)),
        ("track_obsession",   lambda: detect_track_obsession(user_id)),
        ("binge_episode",     lambda: detect_binge(entries, now)),
        ("music_marathon",    lambda: detect_music_marathon(entries, now)),
        ("series_completion", lambda: detect_series_completion(entries, now)),
        ("attention_deficit", lambda: detect_attention_deficit(entries, now)),
        ("procrastinator",    lambda: detect_procrastinator(entries, now)),
        ("genre_rut",         lambda: detect_genre_rut(entries, now)),
        ("guilty_pleasure",   lambda: detect_guilty_pleasure(entries, now)),
        ("genre_absence",     lambda: detect_genre_absence(entries, now)),
        ("low_completion",    lambda: detect_low_completion(entries, now)),
        ("new_genre",         lambda: detect_new_genre(entries, now)),
        ("night_owl",         lambda: detect_night_owl(entries, now)),
        ("history_deep_dive", lambda: detect_history_deep_dive(entries, now)),
    ]
    for ttype, fn in candidates:
        if ttype in recently_fired or ttype in disabled:
            continue
        result = fn()
        if result:
            return result
    return None


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

    if t == "binge_episode":
        prompt = (
            f"You are Curatarr, an opinionated personal curator. "
            f"The user just watched {trigger['count']} episodes of \"{trigger['series']}\" "
            f"in {trigger['hours']} hours straight.{taste}\n\n"
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
            f"\"{trigger['series']}\" in record time.{taste}\n\n"
            f"Write a short, provocative message. Ask if they finally finished it, or if they just lost "
            f"control of their life this weekend. Be direct and slightly teasing. Max 2 sentences."
            + random.choice(_PROVOCATIVE_SUFFIXES)
        )

    elif t == "rewatch":
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
            f"({trigger['media_type']}, genres: {trigger['genres'] or 'unknown'}){ago}.{taste}\n\n"
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
            f"{trigger['episodes']} episodes.{taste}\n\n"
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

    # Proactive messages use the big curator model — route the generation
    # through the curator gate so a scheduled message can't collide with a
    # user's chat on the single GPU (it queues for the slot like the rest).
    from src.services.llm_priority import curator_priority
    async with curator_priority():
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
                            **ollama_options(temperature=0.85, num_predict=800),
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

    slots = min(_CACHE_TARGET - unread_count, _MAX_PER_RUN)
    generated = 0

    for _ in range(slots):
        trigger = _run_all_triggers(
            entries, now, user_id,
            recently_fired=recent_triggers,
            disabled=disabled_triggers,
        )
        if not trigger:
            break

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
            ))
            db.commit()

        recent_triggers.add(trigger["type"])
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
