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
cache refreshes weekly, a play can come any evening. The block rides only
the evening question the UI starts (discuss_context kind "tonight": the
clock chips, the Tonight card's "Ask the curator"); a typed message goes
without it - measured on real data, the bare list made a targeted ask
answer with the same stored pick every time, whatever the header said.
Rule 8 of the chat prompt shapes the answer.
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

# The owner's measured wording (curator_bench, 2026-11-08): the first one made
# a targeted ask ("a film like X") answer only from this list - refusing,
# sending the user to Swipe, or claiming a fit no stored reason gave.
HEADER = ("[TONIGHT CANDIDATES] Titles the user can start tonight from their own library: the "
          "series they are in the middle of, their stored recommendations (with the reason "
          "stored for each) and what they marked 'want to watch' in Swipe. When they ask what to "
          "watch or listen to without saying what they want, pick from these first and give the "
          "reason shown; when they name a mood, genre or title to match, use one only if its "
          "stored reason matches it, never claim more fit than that reason, and otherwise answer "
          "as you would without this list; on any other turn leave them out.")


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
    from src.database.models import (CachedRecommendation as R, LibraryItem, SwipeRating as S,
                                     WatchHistoryEntry as W)
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
            # The series' own page, not the episode already watched: its key
            # from the library ledger (the item the server lists, rebind-safe).
            series_key = (db.query(LibraryItem.plex_rating_key)
                          .filter(func.lower(LibraryItem.title) == title.lower(),
                                  LibraryItem.media_type == agg["media_type"],
                                  LibraryItem.gone_at.is_(None)).scalar())
            out.append({"title": title, "category": agg["media_type"], "source": "in progress",
                        "note": (p or {}).get("phrase") or "", "key": series_key, "kind": "series",
                        "at": ((p or {}).get("furthest_season"), (p or {}).get("furthest_episode"))})

        def played(title: str, category: str, since: Optional[datetime]) -> bool:
            # A film by its title; a series, and a music recommendation (an
            # artist), by the series title its plays carry.
            col = W.title if category == "movie" else W.series_title
            # ``user_id + 0`` keeps the planner off the user_id index (a walk
            # of the owner's ~380k plays per title) - the lower() indexes
            # answer in under a millisecond (pillars._owner_watch's idiom).
            q = db.query(W.id).filter((W.user_id + 0) == user_id, func.lower(col) == title.lower())
            if since is not None:
                q = q.filter(W.viewed_at >= since)
            return q.first() is not None

        recs = (db.query(R.title, R.category, R.lane, R.reason, R.year, R.cached_at, R.score_factors,
                         R.plex_rating_key, R.genres)
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
                        "kind": r.lane, "anchors": anchors,
                        "genres": [g.strip() for g in (r.genres or "").split(",") if g.strip()][:2],
                        # Rediscover's stored reason IS a fact line, in the
                        # member's language (rediscover_recs.fact_line).
                        "fact": r.reason if r.lane == "rediscover" else None})

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


# The card's words, in the language the member writes to the curator in
# (services/user_language) - en and de, like the chat chips and the follow-up
# answers; any other language reads English. Every slot is a fact.
_WEEKDAYS_DE = {"Monday": "Montag", "Tuesday": "Dienstag", "Wednesday": "Mittwoch",
                "Thursday": "Donnerstag", "Friday": "Freitag", "Saturday": "Samstag",
                "Sunday": "Sonntag"}
_TEXT = {
    "en": {
        "label": {"weeknight": "Tonight", "film_night": "Film night", "sunday": "Sunday",
                  "late": "Late", "later": "For later"},
        "series_late": "Still up? One more of {t}", "series_weeknight": "A {wd} evening: carry on with {t}",
        "series": "Carry on with {t}", "want_later": "For later: {t}, you wanted to watch it",
        "want": "You wanted to watch {t}", "rediscover_sunday": "Sunday rewatch? {t}",
        "rediscover": "Back to an old favourite: {t}", "film_night": "{wd} - room for a film: {t}",
        "tonight": "For tonight: {t}",
        "ask": "What should I watch tonight?", "ask_later": "What could I put on later today?",
        "why_series": "You are in the middle of it", "why_upto": "up to {where}",
        "season_ep": "season {s}, episode {e}", "ep": "episode {e}",
        "why_want": "You marked it 'Want to watch' in Swipe",
        "why_because": "Because you watched {names}", "why_because_plain": "Close to what you watched lately",
        "why_library": "In your library, not watched yet", "why_rediscover": "In your library, loved long ago",
        "and": "and",
    },
    "de": {
        "label": {"weeknight": "Heute Abend", "film_night": "Filmabend", "sunday": "Sonntag",
                  "late": "Spät", "later": "Für später"},
        "series_late": "Noch wach? Eine Folge {t}", "series_weeknight": "{wd}abend: weiter mit {t}",
        "series": "Weiter mit {t}", "want_later": "Für später: {t}, das wolltest du sehen",
        "want": "Du wolltest {t} sehen", "rediscover_sunday": "Sonntag - Zeit für ein Wiedersehen? {t}",
        "rediscover": "Wieder mal ein alter Liebling: {t}", "film_night": "{wd} - Zeit für einen Film: {t}",
        "tonight": "Für heute Abend: {t}",
        "ask": "Was soll ich heute Abend schauen?", "ask_later": "Was könnte ich später am Tag schauen?",
        "why_series": "Du bist mittendrin", "why_upto": "bis {where}",
        "season_ep": "Staffel {s}, Folge {e}", "ep": "Folge {e}",
        "why_want": "Du hast es in Swipe als 'Want to watch' markiert",
        "why_because": "Weil du {names} geschaut hast", "why_because_plain": "Nah an dem, was du zuletzt geschaut hast",
        "why_library": "In deiner Bibliothek, noch nicht gesehen", "why_rediscover": "In deiner Bibliothek, lange nicht gesehen",
        "and": "und",
    },
}


def _t(lang: str) -> dict:
    return _TEXT.get(lang) or _TEXT["en"]


def _names(names: list, lang: str) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + f" {_t(lang)['and']} " + names[-1]


def headline(c: dict, moment: str, weekday: str, lang: str = "en") -> str:
    """The card's line for ``c`` at this moment, in ``lang``. Plain text; the
    title is marked with ** for the client to set in bold."""
    tx = _t(lang)
    t = f"**{c['title']}**"
    wd = _WEEKDAYS_DE.get(weekday, weekday) if lang == "de" else weekday
    kind = c.get("kind")
    if kind == "series":
        key = {"late": "series_late", "weeknight": "series_weeknight"}.get(moment, "series")
        return tx[key].format(t=t, wd=wd)
    if kind == "want":
        return tx["want_later" if moment == "later" else "want"].format(t=t)
    if kind == "rediscover":
        return tx["rediscover_sunday" if moment == "sunday" else "rediscover"].format(t=t)
    if moment == "film_night" and c.get("category") == "movie":
        return tx["film_night"].format(t=t, wd=wd)
    return tx["tonight"].format(t=t)


def member_why(c: dict, lang: str = "en") -> str:
    """The card's second line, said TO the member and built from facts only:
    how far they are, the three titles a because-pick is close to, year and
    genres, Rediscover's own fact line. Never the stored recommendation text
    (the curator's pitch, in the curator's voice) and never the candidates'
    ``source`` / ``note`` (written for the model)."""
    tx = _t(lang)
    kind = c.get("kind")
    if kind == "series":
        season, ep = c.get("at") or (None, None)
        where = (tx["season_ep"].format(s=season, e=ep) if season and ep
                 else tx["ep"].format(e=ep) if ep else "")
        return tx["why_series"] + (f" - {tx['why_upto'].format(where=where)}" if where else "") + "."
    if kind == "want":
        return tx["why_want"] + "."
    if kind == "rediscover":
        return c.get("fact") or tx["why_rediscover"]
    lead = (tx["why_because"].format(names=_names(c["anchors"], lang)) if kind == "because" and c.get("anchors")
            else tx["why_because_plain"] if kind == "because" else tx["why_library"])
    facts = [str(c["year"])] if c.get("year") else []
    facts += [", ".join(c.get("genres") or [])] if c.get("genres") else []
    return " · ".join([lead] + facts)


def tonight_card(user_id: int, now: Optional[datetime] = None, zone=None,
                 lang: Optional[str] = None) -> Optional[dict]:
    """{"moment", "weekday", "label", "ask", "items": [...]} - the candidates
    ordered for the member's moment, each with its ``headline`` and ``why``
    in the member's language, and ``ask``: the question the card's "Ask the
    curator" sends (as the evening question, kind "tonight"). None without
    any. Blocking."""
    from src.services.chat_starters import local_now
    from src.services.user_language import user_language
    from src.services.user_time import user_zone_by_id
    now = now or datetime.utcnow()
    lang = lang or user_language(user_id)
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
                      "headline": headline(c, moment, weekday, lang), "why": member_why(c, lang)})
    tx = _t(lang)
    return {"moment": moment, "weekday": weekday, "label": tx["label"][moment],
            "ask": tx["ask_later"] if moment == "later" else tx["ask"], "items": items}
