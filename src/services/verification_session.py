"""
Curatarr - Taste Verification Session Service

After enrichment, Curatarr identifies things it's uncertain about
and generates targeted questions to ask the user — one at a time,
spread over multiple sessions.

Verification targets:
  - Items abandoned at low completion (<40%) — did you drop it or just pause?
  - Genres appearing in both affinity AND aversion — conflicted signals
  - High-replay items — what specifically do you like about them?
  - Recently completed series — what did you think?
  - Outliers — items that don't fit the general taste pattern
"""

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from src.database.connection import get_db_session
from src.database.runner import run_read, run_write
from src.database.models import (
    User, WatchHistoryEntry, TasteVectorEntry,
    ProactiveMessage, EpisodicMemory
)
from src.config import settings
from src.services.episodic_memory import write_memory
from src.services.llm_utils import (
    clean_llm_text, strip_think_tags, ollama_options, SUMMARIZER_KEEP_ALIVE,
    detect_user_language, language_directive,
)
from src.services.http_client import tls_context

logger = logging.getLogger(__name__)


# Pass 45: pattern hints feed Stage-2 LLM composition. Each trigger kind
# describes the INTENT of the question (what to ask, what tone), not a
# fixed English template. The LLM fills in the language + voice using
# the trigger data + per-user language directive.
_PATTERN_HINTS = {
    "dropped": (
        "User dropped this {media_type} at {pct}%. Ask what killed it — "
        "pacing, characters, vibe, or something else."
    ),
    "high_replay_episode": (
        "User rewatched a specific episode ({ep_ref}) of this series "
        "{plays} times. Ask what about THAT episode specifically keeps "
        "pulling them back."
    ),
    "high_replay_track": (
        "User played this song {plays} times{artist_clause}. Ask what "
        "it's giving them — mood, memory, or just a banger they can't "
        "get enough of."
    ),
    "binge": (
        "User binged {eps} episodes of this {media_type} in a single "
        "day. Ask if it was unputdownable, or just default-fill because "
        "nothing else was going on."
    ),
    "completed_series": (
        "User just finished this {media_type}. Ask whether the ending "
        "landed or fell flat, and whether they'd rewatch."
    ),
    "repeat_movie": (
        "User watched this movie {plays} times. Ask what it gives them "
        "that keeps pulling them back."
    ),
    "conflicted_genre": (
        "User has conflicted signals about {genre} {media_type} — "
        "sometimes loves it, sometimes drops early. Ask what the "
        "deciding factor is for them."
    ),
}


def _format_pattern_hint(trigger: dict) -> str:
    """Render the pattern-specific instruction line for the LLM prompt."""
    kind = trigger.get("kind")
    tmpl = _PATTERN_HINTS.get(kind)
    if not tmpl:
        return ""
    # ``artist_clause`` is conditional on the high_replay_track template —
    # other templates ignore the kwarg via ``str.format`` because
    # ``format_map`` here would need a defaultdict. Keep it simple: build
    # a sanitized dict with every placeholder filled (empty string if
    # absent) so ``str.format`` never KeyErrors.
    artist = trigger.get("artist")
    fmt_args = {
        "media_type": trigger.get("media_type") or "title",
        "pct":        trigger.get("pct", 0),
        "ep_ref":     trigger.get("ep_ref", ""),
        "plays":      trigger.get("plays", 0),
        "eps":        trigger.get("eps", 0),
        "genre":      trigger.get("genre", ""),
        "artist_clause": f" by {artist}" if artist else "",
    }
    try:
        return tmpl.format(**fmt_args)
    except (KeyError, IndexError):
        return ""


# ── QUESTION GENERATION ───────────────────────────────────────────────────────

async def generate_verification_questions(user_id: int) -> list:
    """
    Analyze the taste data and generate specific, provocative verification questions.
    Focuses on patterns that reveal real taste — not generic music artist questions.
    """
    return await run_read(_verification_questions, user_id)


def _verification_questions(user_id: int) -> list:
    # Seven history queries; on a worker thread (see the coroutine above).
    questions = []
    now = datetime.utcnow()

    with get_db_session() as db:
        from sqlalchemy import func as _func

        # 1. Recently abandoned items (dropped < 40%, within last 30 days)
        recent_cutoff = now - timedelta(days=30)
        abandoned = db.query(WatchHistoryEntry).filter(
            WatchHistoryEntry.user_id == user_id,
            WatchHistoryEntry.completed == False,
            WatchHistoryEntry.viewed_at >= recent_cutoff,
            WatchHistoryEntry.media_type.in_(["show", "anime", "movie"]),
        ).order_by(WatchHistoryEntry.viewed_at.desc()).limit(10).all()

        for item in abandoned:
            if item.duration_ms and item.view_offset_ms:
                pct = int(100 * item.view_offset_ms / item.duration_ms)
                title = item.series_title or item.title
                if pct < 40:
                    questions.append({
                        "type": "dropped",
                        "priority": 1,
                        "title": title,
                        # Pass 45: ``trigger`` carries the structured pattern
                        # for Stage-2 LLM composition (in user's language).
                        # ``question`` stays as the English worst-case
                        # fallback if the rephrase call fails.
                        "trigger": {"kind": "dropped", "title": title, "pct": pct,
                                    "media_type": item.media_type},
                        "question": f"You bailed on {title!r} at {pct}% — what killed it for you? Pacing, characters, or something else?",
                        "context": {"plex_item_id": item.plex_item_id, "media_type": item.media_type},
                    })

        # 2. High-replay EPISODES (specific episodes watched 5+ times)
        high_replay_eps = db.query(
            WatchHistoryEntry.title,
            WatchHistoryEntry.series_title,
            WatchHistoryEntry.season,
            WatchHistoryEntry.episode,
            WatchHistoryEntry.media_type,
            _func.count(WatchHistoryEntry.id).label("plays"),
        ).filter(
            WatchHistoryEntry.user_id == user_id,
            WatchHistoryEntry.media_type.in_(["show", "anime"]),
            WatchHistoryEntry.episode != None,
        ).group_by(
            WatchHistoryEntry.series_title,
            WatchHistoryEntry.season,
            WatchHistoryEntry.episode,
        ).having(_func.count(WatchHistoryEntry.id) >= 5).order_by(
            _func.count(WatchHistoryEntry.id).desc()
        ).limit(3).all()

        for row in high_replay_eps:
            series = row.series_title or row.title
            ep_ref = f"S{row.season}E{row.episode}" if row.season and row.episode else row.title
            questions.append({
                "type": "high_replay_episode",
                "priority": 1,
                "title": series,
                "plays": row.plays,
                "trigger": {"kind": "high_replay_episode", "title": series,
                            "ep_ref": ep_ref, "plays": row.plays,
                            "media_type": row.media_type},
                "question": f"You've rewatched {series!r} {ep_ref} {row.plays} times. What's in that episode that keeps pulling you back?",
                "context": {"media_type": row.media_type, "episode": ep_ref},
            })

        # 3. High-replay TRACKS (specific songs 10+ plays)
        high_replay_tracks = db.query(
            WatchHistoryEntry.title,
            WatchHistoryEntry.series_title,
            _func.count(WatchHistoryEntry.id).label("plays"),
        ).filter(
            WatchHistoryEntry.user_id == user_id,
            WatchHistoryEntry.media_type == "music",
        ).group_by(
            WatchHistoryEntry.title,
            WatchHistoryEntry.series_title,
        ).having(_func.count(WatchHistoryEntry.id) >= 10).order_by(
            _func.count(WatchHistoryEntry.id).desc()
        ).limit(3).all()

        for row in high_replay_tracks:
            questions.append({
                "type": "high_replay_track",
                "priority": 1,
                "title": row.title,
                "plays": row.plays,
                "trigger": {"kind": "high_replay_track", "title": row.title,
                            "artist": row.series_title or None, "plays": row.plays},
                "question": f"You've played {row.title!r} by {row.series_title or '?'} {row.plays} times. What's it doing for you — mood, memories, or just a banger?",
                "context": {"media_type": "music"},
            })

        # 4. Binge sessions (series watched 3+ eps in one day). date(), not
        # CAST(... AS DATE): SQLite casts a timestamp to its YEAR (2026), so
        # the old query grouped by year and the Date column choked on the int.
        day = _func.date(WatchHistoryEntry.viewed_at)
        binge_sessions = db.query(
            WatchHistoryEntry.series_title,
            WatchHistoryEntry.media_type,
            day.label("day"),
            _func.count(WatchHistoryEntry.id).label("eps"),
        ).filter(
            WatchHistoryEntry.user_id == user_id,
            WatchHistoryEntry.media_type.in_(["show", "anime"]),
            WatchHistoryEntry.series_title != None,
            WatchHistoryEntry.viewed_at >= now - timedelta(days=14),
        ).group_by(
            WatchHistoryEntry.series_title,
            WatchHistoryEntry.media_type,
            day,
        ).having(_func.count(WatchHistoryEntry.id) >= 3).order_by(
            _func.count(WatchHistoryEntry.id).desc()
        ).limit(3).all()

        for row in binge_sessions:
            questions.append({
                "type": "binge",
                "priority": 2,
                "title": row.series_title,
                "plays": row.eps,
                "trigger": {"kind": "binge", "title": row.series_title,
                            "eps": row.eps, "media_type": row.media_type},
                "question": f"You smashed through {row.eps} episodes of {row.series_title!r} in a single day. Was that because you couldn't stop, or because you had nothing else going on?",
                "context": {"media_type": row.media_type},
            })

        # 5. Recently completed series (finished within last 7 days)
        seen_series = set()
        recent_completed = db.query(WatchHistoryEntry).filter(
            WatchHistoryEntry.user_id == user_id,
            WatchHistoryEntry.completed == True,
            WatchHistoryEntry.viewed_at >= now - timedelta(days=7),
            WatchHistoryEntry.media_type.in_(["show", "anime"]),
        ).order_by(WatchHistoryEntry.viewed_at.desc()).limit(20).all()

        for item in recent_completed:
            series = item.series_title
            if series and series not in seen_series:
                seen_series.add(series)
                questions.append({
                    "type": "completed_series",
                    "priority": 2,
                    "title": series,
                    "trigger": {"kind": "completed_series", "title": series,
                                "media_type": item.media_type},
                    "question": f"You just finished {series!r}. Was the ending satisfying, or did it leave you cold? Would you rewatch it?",
                    "context": {"media_type": item.media_type},
                })

        # 6. Long watch history outliers (movie/show with unusually high play count)
        marathon_movies = db.query(
            WatchHistoryEntry.title,
            WatchHistoryEntry.media_type,
            _func.count(WatchHistoryEntry.id).label("plays"),
        ).filter(
            WatchHistoryEntry.user_id == user_id,
            WatchHistoryEntry.media_type == "movie",
        ).group_by(
            WatchHistoryEntry.title,
        ).having(_func.count(WatchHistoryEntry.id) >= 3).order_by(
            _func.count(WatchHistoryEntry.id).desc()
        ).limit(2).all()

        for row in marathon_movies:
            questions.append({
                "type": "repeat_movie",
                "priority": 2,
                "title": row.title,
                "plays": row.plays,
                "trigger": {"kind": "repeat_movie", "title": row.title,
                            "plays": row.plays},
                "question": f"You've watched {row.title!r} {row.plays} times. What does that movie give you that you keep coming back for?",
                "context": {"media_type": "movie"},
            })

        # 7. Conflicted genre signals from taste vector
        tv = db.query(TasteVectorEntry).filter(
            TasteVectorEntry.user_id == user_id
        ).first()
        if tv and tv.genre_affinity:
            type_data = json.loads(tv.genre_affinity)
            for cat, ts in type_data.items():
                if not isinstance(ts, dict):
                    continue
                affinity = set((ts.get("genre_affinity") or {}).keys())
                aversion = set((ts.get("genre_aversion") or {}).keys())
                conflicted = affinity & aversion
                for genre in list(conflicted)[:2]:
                    questions.append({
                        "type": "conflicted_genre",
                        "priority": 3,
                        "genre": genre,
                        "trigger": {"kind": "conflicted_genre", "genre": genre,
                                    "media_type": cat},
                        "question": f"You have a strange relationship with {genre} {cat} — sometimes you love it, sometimes you quit early. What's the deciding factor for you?",
                        "context": {"media_type": cat, "genre": genre},
                    })

    questions.sort(key=lambda q: q["priority"])
    return questions[:10]


# Users with a verification question being composed right now. The
# custodian's recompute and the end of an enrichment run can both start a
# session; both passed the 12-hour check before either stored its question,
# and the bell showed the same question twice.
_STARTING: set = set()

# An unanswered verification question leaves the bell after this long. It
# had no expiry: opening it leaves it unread on purpose (so it can still be
# answered), and an ignored one stayed at the front of the queue.
_VERIFICATION_TTL = timedelta(days=7)


def _asked_recently(db, user_id: int) -> bool:
    return db.query(ProactiveMessage).filter(
        ProactiveMessage.user_id == user_id,
        ProactiveMessage.trigger_type == "verification",
        ProactiveMessage.created_at >= datetime.utcnow() - timedelta(hours=12),
    ).first() is not None


async def start_verification_session(user_id: int) -> Optional[str]:
    """
    Pick the highest-priority unasked question and send it as a proactive message.
    Returns the question text or None if nothing to ask.
    """
    if user_id in _STARTING:
        return None
    _STARTING.add(user_id)
    try:
        return await _start_verification_session(user_id)
    finally:
        _STARTING.discard(user_id)


async def _start_verification_session(user_id: int) -> Optional[str]:
    # Check we haven't already started a verification session recently
    def _recent() -> bool:
        with get_db_session() as db:
            return _asked_recently(db, user_id)

    if await run_read(_recent):
        return None  # don't spam

    questions = await generate_verification_questions(user_id)
    if not questions:
        return None

    # Check which questions have already been asked (stored in memories)
    def _asked_titles() -> set:
        asked_titles = set()
        with get_db_session() as db:
            memories = db.query(EpisodicMemory).filter(
                EpisodicMemory.user_id == user_id,
                EpisodicMemory.memory_type == "verification_asked",
            ).all()
            for m in memories:
                meta = json.loads(m.metadata_json or "{}")
                if meta.get("title"):
                    asked_titles.add(meta["title"])
        return asked_titles

    asked_titles = await run_read(_asked_titles)

    # Find first unasked question
    question = None
    for q in questions:
        title = q.get("title") or q.get("genre", "")
        if title not in asked_titles:
            question = q
            break

    if not question:
        return None

    # Pass 45: compose the question from the structured trigger pattern in
    # the user's detected language. The English ``question`` field stays
    # as worst-case fallback if the LLM call fails. Pattern-Hint tells
    # the LLM what the question is FOR, not how to phrase it — that's
    # the LLM's job, in the right language and tone.
    def _user_lang():
        with get_db_session() as db:
            return detect_user_language(user_id, db)

    user_lang = await run_read(_user_lang)
    lang_line = language_directive(user_lang)

    trigger = question.get("trigger") or {}
    pattern_hint = _format_pattern_hint(trigger)
    title_or_genre = question.get("title") or question.get("genre", "")

    prompt = f"""[MODE: VERIFICATION QUESTION]
{lang_line}

You noticed a pattern in the user's watch history. Compose a SHORT (1-2
sentence) question to ask them about it. Speak directly to the user (du/you).
Be direct and a little playful — like a friend who actually pays attention.
Don't start with "Hey" or "Hi". Don't preface with meta-commentary.
Keep proper nouns (titles, artist names) in their original form.

PATTERN: {pattern_hint}
SUBJECT: {title_or_genre}

Output ONLY the question text — no quotes, no labels, no explanation."""

    rephrased = question["question"]  # English worst-case fallback
    # Small-model call — wait for the user to go quiet and clear the idle
    # curator first: on a 24 GB card the 8B load would unload it under the
    # user's next message (llm_priority.make_room_for_summarizer).
    from src.services.llm_priority import make_room_for_summarizer
    await make_room_for_summarizer()
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=30) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": settings.SUMMARIZER_MODEL or settings.BASE_SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "keep_alive": SUMMARIZER_KEEP_ALIVE,
                    **ollama_options(temperature=0.8, num_predict=700),
                },
            )
        if r.status_code == 200:
            llm_text = strip_think_tags(r.json().get("message", {}).get("content", "").strip())
            # Guard against the LLM returning an empty/whitespace-only response —
            # that would silently overwrite the original question with nothing.
            if llm_text:
                rephrased = llm_text
    except (httpx.HTTPError, ValueError) as e:
        # the English original is asked instead
        logger.debug("Question rephrase failed: %s", e)
    except Exception as e:
        logger.warning("Question rephrase failed: %r", e, exc_info=True)

    # Store the message and mark the question as asked
    def _store() -> bool:
        with get_db_session() as db:
            # Re-checked at write time: another process (a second worker)
            # may have stored one during the LLM call above.
            if _asked_recently(db, user_id):
                return False
            now = datetime.utcnow()
            db.add(ProactiveMessage(
                user_id=user_id,
                trigger_type="verification",
                trigger_data=json.dumps(question),
                message=rephrased,
                read=False,
                created_at=now,
                expires_at=now + _VERIFICATION_TTL,
            ))
            db.commit()
            return True

    if not await run_write(_store):
        return None

    await write_memory(
        user_id=user_id,
        memory_type="verification_asked",
        content=f"Asked about: {question.get('title') or question.get('genre')}",
        metadata={"title": question.get("title") or question.get("genre"),
                  "question_type": question["type"]},
    )

    logger.info("Verification question sent to user %d: %s", user_id, question["type"])
    return rephrased


_SENTIMENTS = frozenset({"positive", "negative", "neutral", "mixed"})


async def process_verification_response(
    user_id: int,
    user_message: str,
    pending_question: dict,
) -> bool:
    """
    Process a user's response to a verification question.
    Extracts sentiment and updates negative/positive vectors via memory.

    Pass 46 (Bug 1): returns True iff a sentiment memory was actually
    written. The caller in ``chat.py`` uses this to decide whether to
    keep the atomic ``read=True`` claim or revert it so the user can
    answer again. Returning False on ANY failure path (empty pending,
    LLM non-200, JSON parse error, exception) ensures the response is
    never silently lost.
    """
    if not pending_question:
        return False

    prompt = f"""The user was asked: "{pending_question.get('question')}"
They responded: "{user_message}"

Extract:
[MODE: SENTIMENT EXTRACTION]
1. answers_question: true if the response actually answers the question; false for a clarifying question back ("which show?"), filler ("lol", "ok"), or something off-topic
2. sentiment: positive/negative/neutral/mixed
3. key_insight: one sentence summary of what this tells us about their taste
4. update_type: affinity_boost/aversion_boost/ambivalent/context_dependent

Output as JSON only: {{"answers_question": true, "sentiment": "...", "key_insight": "...", "update_type": "..."}}"""

    # Small-model call — wait for the user to go quiet and clear the idle
    # curator first: on a 24 GB card the 8B load would unload it under the
    # user's next message (llm_priority.make_room_for_summarizer).
    from src.services.llm_priority import make_room_for_summarizer
    await make_room_for_summarizer()
    try:
        async with httpx.AsyncClient(verify=tls_context(), timeout=30) as client:
            r = await client.post(
                f"{settings.effective_ollama}/api/chat",
                json={
                    "model": settings.SUMMARIZER_MODEL or settings.BASE_SUMMARIZER_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                    "keep_alive": SUMMARIZER_KEEP_ALIVE,
                    **ollama_options(temperature=0.1, num_predict=700),
                },
            )
        if r.status_code != 200:
            return False

        result = json.loads(clean_llm_text(r.json().get("message", {}).get("content", "").strip()))
        if not isinstance(result, dict):
            return False

        # Not an answer ("wait, which show?", "yeah lol"): nothing is stored
        # and the caller re-opens the question, so the user's REAL answer on
        # the next turn still finds it. Before, any reply was mined for an
        # insight the model had to invent, and the claim then swallowed the
        # actual answer. Only an explicit yes counts.
        _ans = result.get("answers_question")
        if not (_ans is True or str(_ans).strip().lower() in ("true", "yes")):
            logger.info("Verification reply is not an answer — question kept open")
            return False

        title = pending_question.get("title") or pending_question.get("genre", "")
        insight = (result.get("key_insight") or "").strip()
        if not insight:
            return False
        # A made-up label ("unclear", "") must not become an explicit
        # statement, the type that weighs most in the taste memory.
        sentiment = str(result.get("sentiment") or "").strip().lower()
        if sentiment not in _SENTIMENTS:
            sentiment = "neutral"

        # Store as memory
        memory_type = "explicit_statement" if sentiment != "neutral" else "feedback"
        mid = await write_memory(
            user_id=user_id,
            memory_type=memory_type,
            content=f"{title}: {insight}",
            metadata={
                "title": title,
                "sentiment": sentiment,
                "update_type": result.get("update_type"),
                "source": "verification",
            },
            media_category=pending_question.get("context", {}).get("media_type"),
        )
        if mid is None:
            # Not saved (too short, write failed): returning True would keep
            # the claim and lose the answer for good.
            return False
        logger.info("Verification response processed: %s → %s", title, sentiment)
        return True

    except (httpx.HTTPError, ValueError) as e:
        # Ollama down or no JSON back: the caller re-opens the question.
        logger.debug("Verification response processing failed: %s", e)
        return False
    except Exception as e:
        logger.warning("Verification response processing failed: %r", e, exc_info=True)
        return False
