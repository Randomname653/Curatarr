"""Curatarr — the Wrap-Up's narrative facts (UX design record, stage 3.2).

"You watched 42 hours" is a number; "Tuesday nights were anime" is a story.
These facts are the second kind, and like every other wrap-up fact they are
decided here, in SQL and plain Python over the member's own rows, on the
member's own clock (``local``: services/user_time.local_sql). The curator
only phrases them, from the insight cards they become
(services/wrapup_insights._narrative); it invents none.

* ritual: one weekday and part of the day held the same title - or the
  same kind of title - on most weeks of the period;
* comfort genre: the genre the member keeps coming back to, across titles
  (the comfort fact is one title; a genre needs two or more);
* of the month: in a period of several months, the month one title owned;
* night shift: a series most of whose plays were after 23:00;
* first of the period: the title it started with (from ``first_last``).

A fact with too little behind it is left out, never zeroed. Blocking - the
caller holds the session.
"""
from collections import Counter, defaultdict
from typing import Optional

_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
RITUAL_MIN_WEEKS = 3
RITUAL_MIN_SHARE = 0.5          # of the period's weeks
COMFORT_GENRE_MIN_REPLAYS = 3
MONTH_MIN_HOURS = 3.0
MONTH_MIN_SHARE = 0.35
NIGHT_MIN_PLAYS = 4
NIGHT_MIN_SHARE = 0.5


def daypart(hour: int) -> str:
    """morning 5-11, afternoon 11-17, evening 17-23, night 23-5 - the
    wrap-up's own dayparts (wrapup_facts._DAYPARTS)."""
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 23:
        return "evening"
    return "night"


def _rows(db, W, ms, base, local) -> list:
    """(weekday 0=Sun, hour, ISO-ish week, month, key, media_type, genres, ms)
    for every timed play - one query; a play known by its day only (an
    import without an hour) has no daypart to belong to."""
    from sqlalchemy import func
    timed = func.coalesce(W.date_only, False) == False     # noqa: E712
    key = func.coalesce(W.series_title, W.title)
    q = db.query(func.strftime("%w", local), func.strftime("%H", local), func.strftime("%Y-%W", local),
                 func.strftime("%Y-%m", local), key, W.media_type, W.genres, ms).filter(*base, timed)
    return [(int(d), int(h), wk, mo, k, mt, g or "", int(v or 0))
            for d, h, wk, mo, k, mt, g, v in q if d is not None and h is not None and k]


def ritual(rows: list) -> dict:
    """The weekday x daypart that held one title (else one kind of title,
    film / series / anime / music) on at least RITUAL_MIN_WEEKS weeks and at
    least half the period's weeks; {} without one. Pure."""
    weeks = {wk for _, _, wk, *_ in rows}
    if len(weeks) < RITUAL_MIN_WEEKS:
        return {}
    by_title: dict = defaultdict(set)
    by_type: dict = defaultdict(set)
    hours: Counter = Counter()
    for d, h, wk, _, k, mt, _, v in rows:
        slot = (d, daypart(h))
        by_title[(slot, k, mt)].add(wk)
        by_type[(slot, mt)].add(wk)
        hours[(slot, k, mt)] += v
        hours[(slot, None, mt)] += v
    need = max(RITUAL_MIN_WEEKS, round(len(weeks) * RITUAL_MIN_SHARE))
    best = None
    for (slot, k, mt), wks in by_title.items():
        if len(wks) >= need and (best is None or (len(wks), hours[(slot, k, mt)]) > best[0]):
            best = ((len(wks), hours[(slot, k, mt)]), slot, k, mt)
    if best is None:
        for (slot, mt), wks in by_type.items():
            if len(wks) >= need and (best is None or (len(wks), hours[(slot, None, mt)]) > best[0]):
                best = ((len(wks), hours[(slot, None, mt)]), slot, None, mt)
    if best is None:
        return {}
    (n, v), (d, part), k, mt = best
    return {"weekday": _WEEKDAYS[d], "daypart": part, "title": k, "media_type": mt,
            "weeks": n, "of_weeks": len(weeks), "hours": round(v / 3_600_000, 1)}


def comfort_genre(rows: list, comfort: Optional[dict]) -> dict:
    """The genre shared by the comfort titles (the ones the member came back
    to - wrapup_facts._comfort), counted in plays of those titles; two titles
    at least, or it is the comfort title, not a genre. {} without one. Pure."""
    titles = set((comfort or {}).get("comfort_titles") or [])
    if len(titles) < 2:
        return {}
    plays: Counter = Counter()
    holders: dict = defaultdict(set)
    for *_, k, mt, genres, v in rows:
        if k not in titles:
            continue
        for g in (x.strip() for x in genres.split(",")):
            if g:
                plays[g] += 1
                holders[g].add(k)
    ranked = [(g, n) for g, n in plays.most_common() if len(holders[g]) >= 2]
    if not ranked or ranked[0][1] < COMFORT_GENRE_MIN_REPLAYS:
        return {}
    g, n = ranked[0]
    return {"genre": g, "plays": n, "titles": sorted(holders[g])[:3]}


def of_the_month(rows: list) -> list:
    """For a period of two or more months: each month one title owned (at
    least MONTH_MIN_SHARE of the month's hours, MONTH_MIN_HOURS hours),
    oldest first. [] otherwise. Pure."""
    months: dict = defaultdict(Counter)
    kinds: dict = {}
    for *_, mo, k, mt, _, v in rows:
        months[mo][k] += v
        kinds[k] = mt
    if len(months) < 2:
        return []
    out = []
    for mo in sorted(months):
        total = sum(months[mo].values())
        k, v = months[mo].most_common(1)[0]
        share = v / total if total else 0
        if v / 3_600_000 >= MONTH_MIN_HOURS and share >= MONTH_MIN_SHARE:
            y, m = mo.split("-")
            out.append({"month": mo, "label": _MONTHS[int(m) - 1], "year": int(y), "title": k,
                        "media_type": kinds[k], "hours": round(v / 3_600_000, 1),
                        "share": round(share, 2)})
    return out


def night_shift(rows: list) -> dict:
    """The series most of whose plays were after 23:00 (or before 05:00) on
    the member's clock: NIGHT_MIN_PLAYS plays at least, NIGHT_MIN_SHARE of
    them at night. {} without one. Pure."""
    plays: Counter = Counter()
    night: Counter = Counter()
    kinds: dict = {}
    for _d, h, _wk, _mo, k, mt, _g, _v in rows:
        if mt not in ("show", "anime"):
            continue
        plays[k] += 1
        kinds[k] = mt
        if daypart(h) == "night":
            night[k] += 1
    best = max(((k, n) for k, n in night.items()
                if plays[k] >= NIGHT_MIN_PLAYS and n / plays[k] >= NIGHT_MIN_SHARE),
               key=lambda kn: (kn[1], kn[1] / plays[kn[0]]), default=None)
    if best is None:
        return {}
    k, n = best
    return {"title": k, "media_type": kinds[k], "night_plays": n, "plays": plays[k],
            "share": round(n / plays[k], 2)}


def narrative(db, W, ms, base, local, period, comfort: Optional[dict]) -> dict:
    """The narrative facts of one wrap-up, keyed as the fact set carries them."""
    rows = _rows(db, W, ms, base, local)
    if not rows:
        return {}
    out = {"ritual": ritual(rows), "comfort_genre": comfort_genre(rows, comfort),
           "night_shift": night_shift(rows)}
    if not period.key.startswith(("week:", "month:")):
        out["of_the_month"] = of_the_month(rows)
    return {k: v for k, v in out.items() if v}


def first_line(first: Optional[dict]) -> Optional[dict]:
    """The title a period started with, as the card wants it. No date: the
    stored moment is UTC, and a date off by one on the member's clock is a
    wrong fact."""
    if not first or not first.get("title"):
        return None
    return {"title": first["title"], "media_type": first.get("media_type")}
