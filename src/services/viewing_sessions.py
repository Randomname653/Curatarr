"""Sittings, not windows: how a series was actually watched.

The proactive binge nudge counted rows of one series inside a six-hour
window measured from whenever the scheduler happened to run, and the chat
starters handed the model a fact literally named ``current_binge`` for any
series with three plays in a week. Both turned an episode or two an evening
into "you binged six episodes" (2026-09-22, owner: the six were spread over
several days). This module groups the plays of ONE series into sittings
from their own timestamps and gaps, and writes the rhythm down in words the
prompts can repeat verbatim — "binge" only when one sitting earns it.

A sitting: consecutive plays where each starts within the previous play's
own length plus a slack of SITTING_GAP_MIN minutes. Plays are counted as
episodes when they were at least half watched (an abandoned start is not
an episode; an unknown duration is trusted).

Stdlib only; entries are the plain dicts the callers already have
(viewed_at, duration_ms, view_offset_ms, completed, season, episode, title).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

SITTING_GAP_MIN = 45          # slack between two plays of one sitting, on top of the episode length
DEFAULT_EPISODE_MIN = 45      # assumed length when duration_ms is unknown
WATCHED_ENOUGH = 0.5          # share of an episode that makes the play count as an episode
# A play that follows the previous one faster than half an episode (or 8
# minutes when the length is unknown) cannot have been watched: those rows
# are bulk "mark as watched" clicks or a re-import after a file upgrade
# (12 episodes of Kakegurui inside one second, five of High School DxD in
# 22 minutes after the Blu-ray swap). They stay in the sitting's timeline
# as plays but are not episodes.
PHANTOM_GAP_SHARE = 0.5
PHANTOM_GAP_MIN = 8


def _watched_enough(e: dict) -> bool:
    if e.get("completed"):
        return True
    dur = e.get("duration_ms") or 0
    off = e.get("view_offset_ms") or 0
    if dur <= 0:
        return True                       # unknown length: trust the row
    return off / dur >= WATCHED_ENOUGH


def _episode_key(e: dict):
    if e.get("episode") is not None:
        return (e.get("season"), e.get("episode"))
    return ("title", e.get("title"))


def utc_offset() -> timedelta:
    """Wall clock of this machine minus UTC — the rows carry naive UTC and
    the owner's evening is a local notion."""
    return datetime.now() - datetime.utcnow()


def _local(dt: datetime) -> datetime:
    """UTC row timestamp → wall clock of this machine (viewed_at is naive UTC)."""
    return dt + utc_offset()


def _daypart(hour: int) -> str:
    if 5 <= hour < 11:
        return "morning"
    if 11 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 23:
        return "evening"
    return "night"


def _when(end: datetime, now: datetime) -> str:
    """'this evening', 'yesterday afternoon', 'on Monday night' — local clock."""
    end_l, now_l = _local(end), _local(now)
    part = _daypart(end_l.hour)
    days = (now_l.date() - end_l.date()).days
    if days <= 0:
        return f"this {part}" if part != "night" else "tonight"
    if days == 1:
        return f"yesterday {part}" if part != "night" else "last night"
    return f"on {end_l.strftime('%A')} {part}"


def _span_text(minutes: int) -> str:
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m:02d} min" if m else f"{h} h"


def sittings(entries: list[dict], now: Optional[datetime] = None,
             window_days: int = 7) -> list[dict]:
    """Plays of one series within the window, grouped into sittings, oldest
    first. Each: {"start", "end", "plays", "episodes", "span_min"}."""
    now = now or datetime.utcnow()
    cutoff = now - timedelta(days=window_days)
    plays = sorted((e for e in entries if e.get("viewed_at") and e["viewed_at"] >= cutoff),
                   key=lambda e: e["viewed_at"])
    out: list[dict] = []
    cur: Optional[dict] = None
    prev: Optional[dict] = None
    for e in plays:
        t = e["viewed_at"]
        phantom = False
        if cur is not None and prev is not None:
            prev_ms = prev.get("duration_ms") or 0
            prev_len = timedelta(milliseconds=prev_ms) if prev_ms else timedelta(minutes=DEFAULT_EPISODE_MIN)
            gap = t - prev["viewed_at"]
            joined = gap <= prev_len + timedelta(minutes=SITTING_GAP_MIN)
            least = (timedelta(milliseconds=prev_ms * PHANTOM_GAP_SHARE) if prev_ms
                     else timedelta(minutes=PHANTOM_GAP_MIN))
            phantom = joined and gap < least
        else:
            joined = False
        if not joined:
            cur = {"start": t, "end": t, "plays": 0, "phantoms": 0, "_eps": set()}
            out.append(cur)
        cur["end"] = t
        cur["plays"] += 1
        if phantom:
            cur["phantoms"] += 1
        elif _watched_enough(e):
            cur["_eps"].add(_episode_key(e))
        prev = e
    for s in out:
        s["episodes"] = len(s.pop("_eps"))
        s["span_min"] = int((s["end"] - s["start"]).total_seconds() // 60)
    return out


def rhythm(entries: list[dict], now: Optional[datetime] = None, *,
           window_days: int = 7, binge_threshold: int = 3) -> dict:
    """The viewing rhythm of one series in words, plus the numbers behind it.

    Returns {"episodes", "sittings", "max_in_one_sitting", "binge" (bool),
             "last": {..} | None, "phrase": str}. The phrase says "in one
    sitting" only for a sitting of binge_threshold or more episodes; a
    routine of an episode or two per sitting is called exactly that."""
    now = now or datetime.utcnow()
    ss = sittings(entries, now, window_days)
    episodes = sum(s["episodes"] for s in ss)
    max_sit = max((s["episodes"] for s in ss), default=0)
    last = ss[-1] if ss else None
    binge = max_sit >= binge_threshold
    if not ss:
        phrase = ""
    elif last["episodes"] >= binge_threshold:
        # the latest sitting is the story
        phrase = (f"{last['episodes']} episodes in one sitting {_when(last['end'], now)}"
                  f" ({_span_text(last['span_min'])})")
        if len(ss) > 1:
            phrase += f", {episodes} in the last {window_days} days over {len(ss)} sittings"
    elif len(ss) == 1:
        phrase = (f"{episodes} episode{'s' if episodes != 1 else ''} {_when(last['end'], now)}"
                  + (f" ({_span_text(last['span_min'])})" if last["span_min"] else ""))
    else:
        per = ("one at a time" if max_sit <= 1 else
               "an episode or two at a time" if max_sit <= 2 else
               f"at most {max_sit} in one go")
        phrase = (f"{episodes} episodes over {len(ss)} sittings in the last {window_days} days, "
                  f"{per} — the latest {_when(last['end'], now)}")
        if binge:
            biggest = max(ss, key=lambda s: s["episodes"])
            phrase += f"; the biggest sitting was {biggest['episodes']} episodes {_when(biggest['end'], now)}"
    return {"episodes": episodes, "sittings": len(ss), "max_in_one_sitting": max_sit,
            "binge": binge, "last": last, "phrase": phrase}
