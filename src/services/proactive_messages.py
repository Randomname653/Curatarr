"""
Curatarr 1.0 - Proactive Messaging Service

Generates unsolicited messages from the curator based on watch/listen patterns.

Trigger types (priority order):
  1. rewatch          — item watched 3+ times total
  2. binge_episode    — 3+ episodes of same series in one session
  3. music_marathon   — 3h+ same artist in one session
  4. series_completion— just finished a series (8+ eps recently)
  5. genre_absence    — loved genre, nothing watched in 30+ days
  6. low_completion   — dropped 3+ shows with <30% completion
  7. history_deep_dive— random observation from older watch history (>60 days ago)
  8. new_genre        — genre appears in recent 30d that wasn't in prior history
  9. night_owl        — 3+ sessions between 00:00-04:00 in the last 14 days
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

logger = logging.getLogger(__name__)

# Target unread cache size — generator fills up to this many
_CACHE_TARGET = 6
# Max messages delivered per hour (prevents overwhelming the user)
_MAX_PER_HOUR = 10
# Won't repeat the same trigger type for this many days
_TRIGGER_COOLDOWN_DAYS = 3


# ── HELPERS ───────────────────────────────────────────────────────────────────

def _to_dicts(entries) -> list[dict]:
    """Convert SQLAlchemy rows to plain dicts."""
    return [
        {
            "title": e.title,
            "series_title": e.series_title,
            "media_type": e.media_type,
            "viewed_at": e.viewed_at,
            "duration_ms": e.duration_ms,
            "view_offset_ms": e.view_offset_ms,
            "completed": e.completed,
            "episode": getattr(e, "episode", None),
            "genres": e.genres,
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
    """Item watched 3+ times across all history."""
    counter: Counter = Counter()
    for e in entries:
        key = e["series_title"] or e["title"]
        counter[key] += 1
    for title, count in counter.most_common(5):
        if count >= 3:
            # Pick the most-rewatched item
            sample = next(e for e in entries if (e["series_title"] or e["title"]) == title)
            return {
                "type": "rewatch",
                "title": title,
                "count": count,
                "media_type": sample["media_type"],
                "genres": sample.get("genres", ""),
            }
    return None


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
    """3+ sessions watched between midnight and 4am in last 14 days."""
    cutoff = now - timedelta(days=14)
    night_sessions = [
        e for e in entries
        if e["viewed_at"] and e["viewed_at"] >= cutoff
        and 0 <= e["viewed_at"].hour < 4
    ]
    if len(night_sessions) >= 3:
        titles = list({e["series_title"] or e["title"] for e in night_sessions})[:3]
        return {
            "type": "night_owl",
            "count": len(night_sessions),
            "titles": titles,
        }
    return None


# ── TRIGGER RUNNER ────────────────────────────────────────────────────────────

def _run_all_triggers(entries: list[dict], now: datetime,
                      recently_fired: set[str]) -> Optional[dict]:
    """
    Try each trigger in priority order, skip types that fired recently.
    `recently_fired` = set of trigger type strings fired in last _TRIGGER_COOLDOWN_DAYS.
    """
    candidates = [
        ("rewatch",           lambda: detect_rewatch(entries)),
        ("binge_episode",     lambda: detect_binge(entries, now)),
        ("music_marathon",    lambda: detect_music_marathon(entries, now)),
        ("series_completion", lambda: detect_series_completion(entries, now)),
        ("genre_absence",     lambda: detect_genre_absence(entries, now)),
        ("low_completion",    lambda: detect_low_completion(entries, now)),
        ("new_genre",         lambda: detect_new_genre(entries, now)),
        ("night_owl",         lambda: detect_night_owl(entries, now)),
        ("history_deep_dive", lambda: detect_history_deep_dive(entries, now)),
    ]
    for ttype, fn in candidates:
        if ttype in recently_fired:
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
    trigger: dict, taste_blurb: str
) -> Optional[str]:
    """Generate a provocative, personalised proactive message via LLM."""
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
            f"The user just finished \"{trigger['series']}\" "
            f"({trigger['episodes_watched']} episodes).{taste}\n\n"
            f"Write a short message asking what they thought. Be direct and specific — "
            f"reference something you might know about this show's reputation or ending."
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
        titles = ", ".join(f"\"{t}\"" for t in trigger["titles"][:2])
        prompt = (
            f"The user has been watching things late at night ({trigger['count']} sessions "
            f"between midnight-4am in the last 2 weeks): {titles}.{taste}\n\n"
            f"Write one short message about this pattern. Insomnia? Comfort watching? "
            f"Something they wouldn't watch with others? Be curious, slightly teasing. Max 2 sentences."
        )

    else:
        return None

    for model in [settings.CURATOR_MODEL, settings.BASE_CURATOR_MODEL]:
        if not model:
            continue
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(
                    f"{settings.effective_ollama}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "stream": False,
                        "options": {"temperature": 0.85, "num_predict": 150},
                    },
                )
            if resp.status_code == 200:
                return resp.json().get("message", {}).get("content", "").strip()
            if resp.status_code == 404:
                continue
        except Exception as e:
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
            m.trigger_type
            for m in db.query(ProactiveMessage)
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

    slots = _CACHE_TARGET - unread_count
    generated = 0

    for _ in range(slots):
        trigger = _run_all_triggers(entries, now, recently_fired=recent_triggers)
        if not trigger:
            break

        logger.info("[proactive] Trigger '%s' for user %d", trigger["type"], user_id)
        message = await generate_proactive_message(trigger, taste_blurb)
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


async def get_unread_messages(user_id: int) -> list:
    """
    Returns all unread messages.
    Delivery rate is self-limiting: the frontend polls this and the user
    reads at their own pace. No server-side throttle needed — the cache
    fills at most every _TRIGGER_COOLDOWN_DAYS per trigger type anyway.
    """
    now = datetime.utcnow()
    with get_db_session() as db:
        # Check how many were read in the last hour — enforce 10/hour cap
        hour_ago = now - timedelta(hours=1)
        read_last_hour = db.query(ProactiveMessage).filter(
            ProactiveMessage.user_id == user_id,
            ProactiveMessage.read == True,
            ProactiveMessage.created_at >= hour_ago,
        ).count()

        msgs = (
            db.query(ProactiveMessage)
            .filter(ProactiveMessage.user_id == user_id, ProactiveMessage.read == False)
            .order_by(ProactiveMessage.created_at.asc())
            .all()
        )

        # If at hourly cap, only return info (no new messages to show)
        if read_last_hour >= _MAX_PER_HOUR:
            return []

        return [
            {
                "id": m.id,
                "message": m.message,
                "trigger_type": m.trigger_type,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ]


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
