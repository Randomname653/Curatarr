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
                  "key": r.plex_item_id,
                  "season": r.season, "episode": r.episode, "viewed_at": r.viewed_at,
                  "duration_ms": r.duration_ms, "view_offset_ms": r.view_offset_ms,
                  "completed": bool(r.completed)}
                 for r in (db.query(W).filter(W.user_id == user_id, W.media_type.in_(_VIDEO))
                           .order_by(W.viewed_at.desc()).limit(200).all())]
        for title, agg in active_series(video, now)[:MAX_SERIES]:
            if agg["media_type"] not in cats:
                continue
            p = get_series_progress(video, title, agg["media_type"], now=now)
            last_key = next((v["key"] for v in video
                             if (v["series_title"] or v["title"]) == title and v["key"]), None)
            out.append({"title": title, "category": agg["media_type"], "source": "in progress",
                        "note": (p or {}).get("phrase") or "", "key": last_key, "kind": "series",
                        "at": ((p or {}).get("furthest_season"), (p or {}).get("furthest_episode"))})

        def played(title: str, category: str, since: Optional[datetime]) -> bool:
            # A film by its title; a series, and a music recommendation (an
            # artist), by the series title its plays carry.
            col = W.title if category == "movie" else W.series_title
            q = db.query(W.id).filter(W.user_id == user_id, func.lower(col) == title.lower())
            if since is not None:
                q = q.filter(W.viewed_at >= since)
            return q.first() is not None

        recs = (db.query(R.title, R.category, R.lane, R.reason, R.year, R.cached_at, R.score_factors,
                         R.plex_rating_key)
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
                        "source": source, "note": _short(r.reason), "key": r.plex_rating_key,
                        "kind": r.lane, "anchors": anchors})

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
                        "source": "marked 'want to watch' in Swipe", "note": "", "kind": "want"})
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


# ── the landing's Tonight card (UX audit 2026-11-08, §2.1 A) ───────────────
#
# One pick for this moment of the member's week, from the same candidates,
# with the rest behind "Something else". No model: the line is a template and
# every slot in it is a fact. The moment is the member's own clock.

def _moment(local: datetime) -> str:
    """weeknight | film_night | sunday | late | later - which rule applies."""
    from src.services.chat_starters import current_daypart
    part, wd = current_daypart(local), local.weekday()
    if part == "night":
        return "late"
    if wd == 6 and part in ("day", "evening"):
        return "sunday"
    if part == "evening":
        return "film_night" if wd in (4, 5) else "weeknight"
    return "later"


# Which kinds of candidate each moment puts first, in order; the rest follow.
_FIRST = {
    "weeknight": ("series", "because", "library", "rediscover", "want"),
    "film_night": ("film", "series", "rediscover", "want"),
    "sunday": ("rediscover", "series", "because", "library", "want"),
    "late": ("series", "because", "library", "want", "rediscover"),
    "later": ("want", "because", "library", "series", "rediscover"),
}


def _rank(c: dict, moment: str) -> int:
    order = _FIRST[moment]
    kinds = [c.get("kind")]
    if c.get("category") == "movie" and c.get("kind") in ("because", "library"):
        kinds.insert(0, "film")
    return min((order.index(k) for k in kinds if k in order), default=len(order))


def headline(c: dict, moment: str, weekday: str) -> str:
    """The card's line for ``c`` at this moment. Plain text; the title is
    marked with ** for the client to set in bold."""
    t = f"**{c['title']}**"
    kind = c.get("kind")
    if kind == "series":
        return {"late": f"Still up? One more of {t}",
                "weeknight": f"A {weekday} evening: carry on with {t}"}.get(moment, f"Carry on with {t}")
    if kind == "want":
        return f"For later: {t}, you wanted to watch it" if moment == "later" else f"You wanted to watch {t}"
    if kind == "rediscover":
        return f"Sunday rewatch? {t}" if moment == "sunday" else f"Back to an old favourite: {t}"
    if moment == "film_night" and c.get("category") == "movie":
        return f"{weekday} - room for a film: {t}"
    return f"For tonight: {t}"


def member_why(c: dict) -> str:
    """The card's second line, said TO the member. The candidates' ``source``
    and ``note`` are written for the curator (third person, and a series'
    progress phrase carries instructions to the model); this is built from
    the same facts for the page."""
    kind = c.get("kind")
    if kind == "series":
        season, ep = c.get("at") or (None, None)
        where = (f"season {season}, episode {ep}" if season and ep else f"episode {ep}" if ep else "")
        return f"You are in the middle of it{f' - up to {where}' if where else ''}."
    if kind == "want":
        return "You marked it 'Want to watch' in Swipe."
    lead = {"because": ("Because you watched " + _and(c["anchors"])) if c.get("anchors")
            else "Close to what you watched lately",
            "library": "From your library, still unwatched",
            "rediscover": "Loved once, untouched for two years"}.get(kind, "")
    note = c.get("note") or ""
    return f"{lead}: {note}" if lead and note else (lead or note)


def tonight_card(user_id: int, now: Optional[datetime] = None, zone=None) -> Optional[dict]:
    """{"moment", "weekday", "items": [...]} - the candidates ordered for the
    member's moment, each with its ``headline`` and ``why``; None without
    any. Blocking."""
    from src.services.chat_starters import local_now
    from src.services.user_time import user_zone_by_id
    now = now or datetime.utcnow()
    local = local_now(zone or user_zone_by_id(user_id), now)
    cands = candidates(user_id, now=now)
    if not cands:
        return None
    moment = _moment(local)
    weekday = local.strftime("%A")
    ordered = sorted(enumerate(cands), key=lambda ic: (_rank(ic[1], moment), ic[0]))
    items = []
    for _, c in ordered:
        items.append({"title": c["title"], "category": c["category"], "year": c.get("year"),
                      "kind": c.get("kind"), "key": c.get("key"),
                      "headline": headline(c, moment, weekday), "why": member_why(c)})
    return {"moment": moment, "weekday": weekday, "items": items}
