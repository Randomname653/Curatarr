"""Curatarr — the member's verdict on something they played.

The landing's follow-up card (proactive_messages.followup_card) asks "how
did it land?" about one title the member just played: a watched
recommendation, or a morning line about exactly one title. Before
2026-11-08 the one-tap answer was sent as a chat message, and only memory
extraction ever read it. Now it is stored here (``post_watch_verdicts``) and
the taste rebuild weighs it - the owner's decision for stage 2 of
docs/UX_RETENTION_DESIGN.md:

* a weight on the play row that is already in the history, never a row of
  its own, so nothing counts twice (like a Plex star rating on a music row,
  taste_engine's ``user_rating_by_item``);
* a film matches by its play's key, a series by its title (the rebuild
  weighs a series as one, its episodes carry keys of their own);
* ``swipe_ratings`` stays the record of what was seen outside the server.

The factors sit on the music rating's scale (a Plex rating / 6: 10 → 1.67,
8 → 1.33, 5 → 0.83). "Not for me" is the one place it differs: a finished
film the member did not like would still PULL the taste toward it at any
positive factor, so it pushes instead (-1.0), the way a drop or a low swipe
does.
"""
import json
import logging
from datetime import datetime
from typing import Optional

from src.database.connection import get_db_session

logger = logging.getLogger(__name__)

VERDICTS = ("loved", "liked", "okay", "not_for_me")
FACTORS = {"loved": 10 / 6, "liked": 8 / 6, "okay": 5 / 6, "not_for_me": -1.0}
# What a verdict can judge: the categories whose plays the rebuild weighs
# per title or per series. A music follow-up stays a chat thread.
CATEGORIES = ("movie", "show", "anime")
LABELS = {
    "en": {"loved": "Loved it", "liked": "Liked it", "okay": "It was okay", "not_for_me": "Not for me"},
    "de": {"loved": "Hat mir sehr gefallen", "liked": "Hat mir gefallen", "okay": "War okay",
           "not_for_me": "Nichts für mich"},
}


class NoPlay(LookupError):
    """The message names nothing the member played - there is no row to weigh."""


def subject_of(trigger_type: str, trigger_data: Optional[str]) -> Optional[dict]:
    """{"title", "category"} a follow-up message is about, or None when it is
    not about exactly one title of a category a verdict can judge."""
    try:
        td = json.loads(trigger_data or "{}")
    except (ValueError, TypeError):
        return None
    if not isinstance(td, dict):
        return None
    if trigger_type == "last_night":
        if td.get("parts") != 1 or td.get("titles") != 1:
            return None
        title, category = td.get("subject"), td.get("subject_type")
    elif trigger_type == "recommendation_followup":
        title, category = td.get("title"), td.get("category")
    else:
        return None
    if not title or category not in CATEGORIES:
        return None
    return {"title": str(title), "category": category}


def _the_play(db, user_id: int, title: str, category: str):
    """The member's newest play of ``title``: the film by its title, a series
    by its series title. A series is a series whichever of show / anime the
    message and the plays file it under - a message naming a show whose plays
    are anime found nothing, and the card came back on every load."""
    from sqlalchemy import func
    from src.database.models import WatchHistoryEntry as W
    col = W.title if category == "movie" else W.series_title
    kinds = ("movie",) if category == "movie" else ("show", "anime")
    return (db.query(W.plex_item_id, W.title, W.series_title, W.media_type)
            .filter(W.user_id == user_id, W.media_type.in_(kinds),
                    func.lower(col) == title.lower())
            .order_by(W.viewed_at.desc()).first())


def has_play(db, user_id: int, subject: dict) -> bool:
    """Whether a verdict on ``subject`` would find a play to weigh."""
    return _the_play(db, user_id, subject["title"], subject["category"]) is not None


def record_from_message(user_id: int, message_id: int, verdict: str,
                        now: Optional[datetime] = None) -> dict:
    """Store the verdict a follow-up message asked for, and retire the message.
    Raises ValueError (unknown verdict), LookupError (no such message of this
    member's, or not one a verdict answers) or NoPlay. Blocking."""
    from src.database.models import PostWatchVerdict, ProactiveMessage
    if verdict not in VERDICTS:
        raise ValueError(f"unknown verdict: {verdict!r}")
    now = now or datetime.utcnow()
    with get_db_session() as db:
        msg = (db.query(ProactiveMessage)
               .filter(ProactiveMessage.id == message_id,
                       ProactiveMessage.user_id == user_id).first())
        if msg is None:
            raise LookupError("no such message")
        subject = subject_of(msg.trigger_type, msg.trigger_data)
        if subject is None:
            raise LookupError("this message takes no verdict")
        play = _the_play(db, user_id, subject["title"], subject["category"])
        if play is None:
            raise NoPlay(subject["title"])
        key, play_title, series_title, play_type = play
        # The plays' own category: the rebuild matches a series by (its
        # media_type, title), so the verdict must carry what the plays say.
        category = play_type or subject["category"]
        title = play_title if category == "movie" else (series_title or subject["title"])
        q = db.query(PostWatchVerdict).filter(PostWatchVerdict.user_id == user_id,
                                              PostWatchVerdict.category == category)
        q = (q.filter(PostWatchVerdict.item_key == key) if category == "movie"
             else q.filter(PostWatchVerdict.title == title))
        row = q.first()
        if row is None:
            row = PostWatchVerdict(user_id=user_id, category=category)
            db.add(row)
        row.item_key, row.title, row.verdict = key, title, verdict
        row.source, row.created_at = "followup_card", now
        msg.read = True
        db.commit()
        logger.info("[post-watch] user %d: %s %r (%s)", user_id, verdict, title, category)
        return {"title": title, "category": category, "verdict": verdict}


def factors_for(user_id: int, category: Optional[str] = None) -> tuple:
    """The rebuild's view: ({film play key: factor}, {(category, series title
    lower-cased): factor}) for ``category`` (None: every one). Blocking."""
    from src.database.models import PostWatchVerdict as V
    by_key: dict = {}
    by_series: dict = {}
    with get_db_session() as db:
        q = db.query(V.category, V.item_key, V.title, V.verdict).filter(V.user_id == user_id)
        if category:
            q = q.filter(V.category == category)
        for cat, key, title, verdict in q.all():
            f = FACTORS.get(verdict)
            if f is None:
                continue
            if cat == "movie":
                if key:
                    by_key[str(key)] = f
            elif title:
                by_series[(cat, title.lower())] = f
    return by_key, by_series


def factor_of(entry: dict, by_key: dict, by_series: dict) -> Optional[float]:
    """The verdict factor for one history row, or None: a film by its key, an
    episode by its series."""
    mt = entry.get("media_type")
    if mt == "movie":
        return by_key.get(str(entry.get("plex_item_id") or ""))
    if mt in ("show", "anime"):
        t = entry.get("series_title") or entry.get("title") or ""
        return by_series.get((mt, t.lower()))
    return None


def answers(lang: str) -> list:
    """The card's one-tap answers in ``lang`` (English for any other)."""
    labels = LABELS.get(lang) or LABELS["en"]
    return [{"verdict": v, "label": labels[v]} for v in VERDICTS]
