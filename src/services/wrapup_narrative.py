"""Curatarr — the Wrap-Up's narrative facts (UX design record, stage 3.2).

"You watched 42 hours" is a number; "Tuesday nights were anime" is a story.
These facts are the second kind, and like every other wrap-up fact they are
decided here, in SQL and plain Python over the member's own rows, on the
member's own clock (``local``: services/user_time.local_sql). The curator
only phrases them, from the insight cards they become
(services/wrapup_insights._narrative); it invents none.

* ritual: one weekday and part of the day held the same title - or the
  same kind of title - on most weeks of the period;
* comfort genre: the genre the member keeps coming back to - across two
  titles at least, on several days, most of the hours (a medium such as
  "Animation" is no genre of a story);
* of the month: in a period of several months, the month one title owned;
* night shift: the series that held the late hours - most of the plays
  after 23:00, and a real part of its own;
* first of the period: the title it started with (from ``first_last``).

A fact with too little behind it is left out, never zeroed. Blocking - the
caller holds the session.
"""
from collections import Counter, defaultdict, namedtuple
from datetime import date
from typing import Optional

_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August",
           "September", "October", "November", "December")
RITUAL_MIN_WEEKS = 3
RITUAL_MIN_SHARE = 0.5          # of the period's weeks
# Tuned on real months (owner's review, 2026-11): a month of ~40 hours,
# a quarter after 23:00 and one genre most of the time found none of these
# with the first thresholds - the comfort genre read only rewatched titles,
# which a month rarely has, and the night series had to be mostly a night
# series, where the late hours are usually one series among others.
COMFORT_GENRE_MIN_TITLES = 2
COMFORT_GENRE_MIN_DAYS = 4
COMFORT_GENRE_MIN_SHARE = 0.4   # of the timed hours
FORMAT_GENRES = {"animation", "anime", "short", "tv movie", "documentary"}
MONTH_MIN_HOURS = 3.0
MONTH_MIN_SHARE = 0.35
NIGHT_MIN_PLAYS = 3             # night plays of the series
NIGHT_MIN_HOLD = 0.4            # of all the night plays
NIGHT_MIN_SHARE = 0.25          # of its own plays

# One timed play on the member's clock.
Row = namedtuple("Row", "wd hour week month key mt genres ms day")


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
    """[Row] for every timed play - one query; a play known by its day only
    (an import without an hour) has no daypart to belong to. ``week`` is
    the ISO week, so a week across New Year is one week, not two."""
    from sqlalchemy import func
    timed = func.coalesce(W.date_only, False) == False     # noqa: E712
    key = func.coalesce(W.series_title, W.title)
    q = db.query(func.strftime("%w", local), func.strftime("%H", local), func.strftime("%Y-%m-%d", local),
                 key, W.media_type, W.genres, ms).filter(*base, timed)
    return [Row(int(d), int(h), _iso_week(day), day[:7], k, mt, g or "", int(v or 0), day)
            for d, h, day, k, mt, g, v in q if d is not None and h is not None and day and k]


def _iso_week(day: str) -> str:
    """'2026-12-31' → '2026-W53': the ISO week, in Python - SQLite's %V is
    newer than the SQLite many installs bundle."""
    y, w, _ = date.fromisoformat(day).isocalendar()
    return f"{y}-W{w:02d}"


def ritual(rows: list) -> dict:
    """The weekday x daypart that held one title (else one kind of title,
    film / series / anime / music) on at least RITUAL_MIN_WEEKS weeks and at
    least half the period's weeks; {} without one. Pure."""
    weeks = {r.week for r in rows}
    if len(weeks) < RITUAL_MIN_WEEKS:
        return {}
    by_title: dict = defaultdict(set)
    by_type: dict = defaultdict(set)
    hours: Counter = Counter()
    for r in rows:
        d, h, wk, k, mt, v = r.wd, r.hour, r.week, r.key, r.mt, r.ms
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


def comfort_genre(rows: list, comfort: Optional[dict] = None) -> dict:
    """The genre the member came back to: the most hours of the period,
    across COMFORT_GENRE_MIN_TITLES titles at least, on
    COMFORT_GENRE_MIN_DAYS days at least, holding COMFORT_GENRE_MIN_SHARE of
    the timed hours. A medium ("Animation") says how, not what - it is
    skipped. ``comfort`` (the rewatched titles) breaks a tie. {} without
    one. Pure."""
    total = sum(r.ms for r in rows)
    if not total:
        return {}
    hours: Counter = Counter()
    plays: Counter = Counter()
    holders: dict = defaultdict(set)
    days: dict = defaultdict(set)
    rewatched = set((comfort or {}).get("comfort_titles") or [])
    for r in rows:
        for g in {x.strip() for x in r.genres.split(",") if x.strip()}:
            if g.lower() in FORMAT_GENRES:
                continue
            hours[g] += r.ms
            plays[g] += 1
            holders[g].add(r.key)
            days[g].add(r.day)
    ranked = sorted((g for g in hours if len(holders[g]) >= COMFORT_GENRE_MIN_TITLES
                     and len(days[g]) >= COMFORT_GENRE_MIN_DAYS and hours[g] / total >= COMFORT_GENRE_MIN_SHARE),
                    key=lambda g: (-hours[g], -len(holders[g] & rewatched), g))
    if not ranked:
        return {}
    g = ranked[0]
    by_hours = Counter()
    for r in rows:
        if r.key in holders[g]:
            by_hours[r.key] += r.ms
    return {"genre": g, "plays": plays[g], "share": round(hours[g] / total, 2),
            "titles": [k for k, _ in by_hours.most_common(3)]}


def of_the_month(rows: list) -> list:
    """For a period of two or more months: each month one title owned (at
    least MONTH_MIN_SHARE of the month's hours, MONTH_MIN_HOURS hours),
    oldest first. [] otherwise. Pure."""
    months: dict = defaultdict(Counter)
    kinds: dict = {}
    for r in rows:
        months[r.month][r.key] += r.ms
        kinds[r.key] = r.mt
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
    """The series that held the late hours (after 23:00, or before 05:00, on
    the member's clock): the most night plays, NIGHT_MIN_PLAYS at least,
    NIGHT_MIN_HOLD of all the night plays and NIGHT_MIN_SHARE of its own.
    {} without one. Pure."""
    plays: Counter = Counter()
    night: Counter = Counter()
    kinds: dict = {}
    all_night = 0
    for r in rows:
        late = daypart(r.hour) == "night"
        all_night += late
        if r.mt not in ("show", "anime"):
            continue
        plays[r.key] += 1
        kinds[r.key] = r.mt
        night[r.key] += late
    best = max(((k, n) for k, n in night.items()
                if n >= NIGHT_MIN_PLAYS and n / all_night >= NIGHT_MIN_HOLD and n / plays[k] >= NIGHT_MIN_SHARE),
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
