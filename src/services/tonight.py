"""Curatarr — what the member could start tonight, for the chat.

"What should I watch tonight?" in free chat searched the vector store for
that sentence (chat._get_rag_context on the literal message): the five
library items nearest to the words "what should I watch tonight", watched
or not. The member's real candidates were never in the prompt (UX audit
2026-11-08, C2). This block puts them there, from data Curatarr already
keeps - verified data in, no model call, no new source:

* the series they are in the middle of (chat_starters.active_series, the
  openers' own rule) with how far they are (series_progress);
* the stored recommendation lanes the member owns: "because you watched",
  "from your library", "rediscover" (CachedRecommendation, the curator's own
  stored reason); the discovery lane is left out - not in the library;
* their Swipe "want to watch" answers (swipe_ratings, the record of what
  went to their watchlist).

A title the member played since it was stored is dropped: the recommendation
cache refreshes weekly, a play can come any evening. The block says when to
use it; rule 8 of the chat prompt shapes the answer.
"""
from datetime import datetime, timedelta
from typing import Optional

from src.database.connection import get_db_session

MAX_SERIES = 2
MAX_LANE = 4
MAX_WANTS = 2
_REASON_CHARS = 160
_LANES = ("because", "library", "rediscover")
_NOUN = {"movie": "film", "show": "series", "anime": "anime", "music": "music"}
_VIDEO = ("movie", "show", "anime")

HEADER = ("[TONIGHT CANDIDATES] Titles the user can start tonight from their own library: the "
          "series they are in the middle of, their stored recommendations (with the reason "
          "stored for each) and what they marked 'want to watch' in Swipe. When they ask what to "
          "watch or listen to, pick from these first and give the reason shown; otherwise leave "
          "them out.")


def _short(text: Optional[str]) -> str:
    t = " ".join((text or "").split())
    return t if len(t) <= _REASON_CHARS else t[:_REASON_CHARS - 1].rsplit(" ", 1)[0] + "…"


def _anchors(raw: Optional[str]) -> list:
    """The three titles a because-lane pick is close to (because_recs)."""
    import json
    try:
        data = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return []
    a = data.get("anchors") if isinstance(data, dict) else None
    return [str(x) for x in a if x] if isinstance(a, list) else []


def _and(names: list) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def candidates(user_id: int, domain: Optional[str] = None,
               now: Optional[datetime] = None) -> list[dict]:
    """[{title, category, source, note}] - at most MAX_SERIES series, MAX_LANE
    stored recommendations and MAX_WANTS Swipe wants, nothing the member has
    played since. ``domain`` (movie / show / anime / music) narrows it; without
    one, films, series and anime. Blocking."""
    from sqlalchemy import func
    from src.database.models import CachedRecommendation as R, SwipeRating as S, WatchHistoryEntry as W
    from src.services.chat_starters import active_series
    from src.services.series_progress import get_series_progress
    now = now or datetime.utcnow()
    cats = (domain,) if domain in _VIDEO + ("music",) else _VIDEO
    out: list[dict] = []
    with get_db_session() as db:
        video = [{"title": r.title, "series_title": r.series_title, "media_type": r.media_type,
                  "season": r.season, "episode": r.episode, "viewed_at": r.viewed_at,
                  "duration_ms": r.duration_ms, "view_offset_ms": r.view_offset_ms,
                  "completed": bool(r.completed)}
                 for r in (db.query(W).filter(W.user_id == user_id, W.media_type.in_(_VIDEO))
                           .order_by(W.viewed_at.desc()).limit(200).all())]
        for title, agg in active_series(video, now)[:MAX_SERIES]:
            if agg["media_type"] not in cats:
                continue
            p = get_series_progress(video, title, agg["media_type"], now=now)
            out.append({"title": title, "category": agg["media_type"], "source": "in progress",
                        "note": (p or {}).get("phrase") or ""})

        def played(title: str, category: str, since: Optional[datetime]) -> bool:
            # A film by its title; a series, and a music recommendation (an
            # artist), by the series title its plays carry.
            col = W.title if category == "movie" else W.series_title
            q = db.query(W.id).filter(W.user_id == user_id, func.lower(col) == title.lower())
            if since is not None:
                q = q.filter(W.viewed_at >= since)
            return q.first() is not None

        recs = (db.query(R.title, R.category, R.lane, R.reason, R.year, R.cached_at, R.score_factors)
                .filter(R.user_id == user_id, R.lane.in_(_LANES), R.category.in_(cats))
                .order_by(R.lifted_at.desc(), R.confidence.desc()).limit(40).all())
        seen = {c["title"].lower() for c in out}
        by_lane = {lane: [r for r in recs if r.lane == lane] for lane in _LANES}
        picked: list = []
        while len(picked) < MAX_LANE and any(by_lane.values()):
            for lane in _LANES:                      # round robin: one of each lane in turn
                while by_lane[lane] and len(picked) < MAX_LANE:
                    r = by_lane[lane].pop(0)
                    # A rediscover pick is a rewatch by definition: only a play
                    # since it was stored retires it. The others: any play. A
                    # retired pick does not cost its lane the turn.
                    since = r.cached_at if lane == "rediscover" else None
                    if r.title.lower() in seen or played(r.title, r.category, since):
                        continue
                    seen.add(r.title.lower())
                    picked.append(r)
                    break
        noun = {"because": "recommended because they watched similar titles",
                "library": "recommended, from their library",
                "rediscover": "rediscover: loved once, untouched for two years"}
        for r in picked:
            source = noun[r.lane]
            anchors = _anchors(r.score_factors) if r.lane == "because" else []
            if anchors:
                source = "recommended because they watched " + _and(anchors)
            out.append({"title": r.title, "category": r.category, "year": r.year,
                        "source": source, "note": _short(r.reason)})

        wants = (db.query(S.title, S.category)
                 .filter(S.user_id == user_id, S.want == True, S.seen == False,  # noqa: E712
                         S.category.in_(cats), S.title.isnot(None),
                         S.updated_at >= now - timedelta(days=180))
                 .order_by(S.updated_at.desc()).limit(10).all())
        n = 0
        for title, category in wants:
            if n >= MAX_WANTS or title.lower() in seen or played(title, category, None):
                continue
            seen.add(title.lower())
            out.append({"title": title, "category": category,
                        "source": "marked 'want to watch' in Swipe", "note": ""})
            n += 1
    return out


def block(cands: list[dict]) -> str:
    """The system-prompt block for ``candidates`` output; "" for none."""
    if not cands:
        return ""
    lines = [HEADER]
    for c in cands:
        what = ", ".join(x for x in (str(c["year"]) if c.get("year") else "",
                                     _NOUN.get(c["category"], c["category"])) if x)
        tail = f": {c['note']}" if c.get("note") else ""
        lines.append(f"- {c['title']} ({what}) - {c['source']}{tail}")
    return "\n".join(lines)


def tonight_block(user_id: int, domain: Optional[str] = None,
                  now: Optional[datetime] = None) -> str:
    """``block(candidates(...))``. Blocking."""
    return block(candidates(user_id, domain, now))
