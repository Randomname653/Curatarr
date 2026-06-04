"""
Curatarr 1.0 - Series Progress Awareness

Read-only helpers that tell the curator HOW FAR a user is through a TV/anime
series, so proactive messages and chat can:

  (1) FRAME questions to the user's actual position — never ask about "the
      ending" of a show they only watched one episode of, and never assume a
      series "ended" when more exists than they have seen; and

  (2) DECIDE whether re-engaging a series is worthwhile — only when the user's
      position has MEANINGFULLY advanced since the curator last asked (reached a
      new season, or finally finished it). Asking "how was the season 1 ending?"
      twice is noise; asking once at S1 and again once they're into S2 is not.

Three layers, deliberately separated so the watch-side stays pure + unit-testable:

  compute_watch_progress(entries, series_key)  — PURE, in-memory. Aggregates the
      furthest (season, episode) reached, distinct episodes watched, last view
      time and finale-completion from the watch-history dicts the proactive
      generator ALREADY loads (no extra DB query, no network).

  lookup_series_total(series_title, media_type) — best-effort total episode count
      from the EXISTING enrichment cache (MediaIdentity -> ``enriched:*`` key).
      Degrades silently to None: totals are an enhancement, never a requirement.
      Total SEASON count is intentionally NOT sourced here — the enrichment
      profile doesn't persist it — so "furthest season" comes purely from the
      user's own watch data.

  get_series_progress(...)                      — combines the two, derives
      ``finished`` (conservative: only True when a total is KNOWN and fully
      watched), a human ``phrase`` for prompts, and a ``milestone`` signature for
      the re-ask decision (``should_reengage_series``).
"""

import logging
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

SERIES_TYPES = ("show", "anime")

# A jump of this many *additional* distinct episodes since the last time we
# asked counts as "meaningfully advanced" even when season numbers are missing
# (some Plex libraries don't tag parentIndex). One cour is ~12 episodes; we set
# the bar at a half-cour so a couple of stray episodes don't re-trigger, but a
# genuine binge does. Reaching a NEW SEASON or FINISHING always re-triggers
# regardless of this threshold.
_REENGAGE_EPISODE_JUMP = 6


def normalize_title(s: Optional[str]) -> str:
    """Loose, case/space-insensitive key for matching a title against the
    'already asked about' history. Not a fuzzy matcher — just collapses the
    incidental differences (case, surrounding whitespace, repeated spaces)."""
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _ep_sort_key(e: dict) -> tuple:
    """Order watch entries by (season, episode), treating missing numbers as 0
    so an unlabelled play never out-sorts a real S01E05."""
    s = e.get("season")
    ep = e.get("episode")
    return (s if s is not None else 0, ep if ep is not None else 0)


def compute_watch_progress(entries: list[dict], series_key: str) -> Optional[dict]:
    """Aggregate one series' progress from already-loaded watch-history dicts.

    ``series_key`` is matched against ``e["series_title"] or e["title"]`` — the
    same key the proactive detectors group on. ``entries`` are the dicts produced
    by ``proactive_messages._to_dicts`` (must now include ``season``).

    Returns None when the user has no show/anime plays for this series. Otherwise
    a dict describing where they are — see keys below. Pure: no DB, no network.
    """
    mine = [
        e for e in entries
        if e.get("media_type") in SERIES_TYPES
        and (e.get("series_title") or e.get("title")) == series_key
    ]
    if not mine:
        return None

    # Distinct episodes by (season, episode). Falls back to play count when a
    # library tags neither (rare, but then "distinct episodes" is meaningless
    # and the row count is the best signal we have).
    distinct = {
        (e.get("season"), e.get("episode"))
        for e in mine
        if e.get("episode") is not None
    }
    distinct_episodes = len(distinct) if distinct else len(mine)

    # Distinct episodes watched PER season — lets the curator say "finished
    # season 1, three episodes into season 2" once Sonarr supplies per-season
    # episode counts.
    per_season_watched: dict = {}
    for s, _e in distinct:
        if s is not None:
            per_season_watched[s] = per_season_watched.get(s, 0) + 1

    furthest = max(mine, key=_ep_sort_key)
    furthest_season = furthest.get("season")
    furthest_episode = furthest.get("episode")

    # Was the furthest-reached episode actually finished (vs. bailed mid-way)?
    # Mirrors proactive_messages._completion_rate without importing it (keeps
    # this module dependency-light): completed flag, or >=90% of runtime.
    dur = furthest.get("duration_ms") or 0
    off = furthest.get("view_offset_ms") or 0
    furthest_completed = bool(
        furthest.get("completed")
        or (dur > 0 and off / dur >= 0.9)
    )

    views = [e.get("viewed_at") for e in mine if e.get("viewed_at")]
    last_viewed_at = max(views) if views else None

    return {
        "series_title": series_key,
        "media_type": furthest.get("media_type"),
        "furthest_season": furthest_season,
        "furthest_episode": furthest_episode,
        "distinct_episodes": distinct_episodes,
        "per_season_watched": per_season_watched,
        "total_plays": len(mine),
        "furthest_completed": furthest_completed,
        "last_viewed_at": last_viewed_at,
    }


def _humanize_age(days: int) -> str:
    """Rough, human recency for the progress phrase ('~3 weeks ago')."""
    if days <= 1:
        return "today"
    if days < 14:
        return f"~{days} days ago"
    if days < 60:
        return f"~{days // 7} weeks ago"
    if days < 365:
        return f"~{max(1, days // 30)} months ago"
    years = days / 365
    return f"~{years:.0f} year{'s' if years >= 1.5 else ''} ago"


def _multiseason_phrase(p: dict) -> str:
    """Season-aware description for multi-season series (Sonarr-backed): which
    seasons are finished, where they are in the current one, out of how many."""
    ts = p.get("total_seasons")
    te = p.get("total_episodes")
    sc = p.get("seasons_completed") or []
    cur_s = p.get("furthest_season")
    cur_e = p.get("furthest_episode")
    psw = p.get("per_season_watched") or {}
    pst = p.get("per_season_total") or {}
    watched_main = p.get("watched_main") or p.get("distinct_episodes") or 0
    caught_up = p.get("caught_up")
    status = p.get("status")

    if p.get("finished"):
        return f"has finished the ENTIRE series — all {te} episodes across {ts} seasons"

    bits: list[str] = []
    if sc:
        label = ", ".join(str(s) for s in sc)
        bits.append(f"finished season{'s' if len(sc) > 1 else ''} {label}")

    if cur_s is not None and cur_s not in sc:
        of_ts = f" of {ts}" if ts else ""
        cur_w = psw.get(cur_s, 0)
        cur_t = pst.get(cur_s)
        if cur_w <= 1:
            bits.append(f"just started season {cur_s}{of_ts} (episode {cur_e or 1})")
        elif cur_t:
            bits.append(f"{cur_w} of {cur_t} episodes into season {cur_s}{of_ts}")
        else:
            bits.append(f"{cur_w} episodes into season {cur_s}{of_ts}")

    if caught_up and status and status != "ended":
        bits.append("caught up with every aired episode — the series is still ongoing")
    elif te:
        bits.append(f"has NOT finished — {watched_main} of {te} aired episodes watched")

    return "; ".join(bits) if bits else f"watched {watched_main} episodes"


def _progress_phrase(p: dict, now: Optional[datetime] = None) -> str:
    """One-line, English, factual description of the user's position — fed to the
    LLM so it frames the question correctly instead of assuming completion.

    Uses Sonarr-derived season/episode counts when present (``finished season 1;
    3 of 10 episodes into season 2 of 2``); otherwise a plain total or an honest
    no-total description. ``now`` (optional) appends a recency hint so the curator
    can tell "currently bingeing" from "dropped it months ago"."""
    season = p.get("furthest_season")
    ep = p.get("furthest_episode")
    distinct = p.get("distinct_episodes") or 0
    total = p.get("total_episodes")
    total_seasons = p.get("total_seasons")
    finished = p.get("finished")

    def _point() -> str:
        if season and ep:
            return f"season {season}, episode {ep}"
        if ep:
            return f"episode {ep}"
        return "the start"

    if total_seasons and total_seasons > 1:
        core = _multiseason_phrase(p)
    elif finished is True:
        core = (f"finished the whole series (all {total} episodes)"
                if total else "finished the series")
    elif distinct <= 1:
        # Single episode and stopped — the Nukitashi case.
        core = f"only watched {_point()} and then stopped — never went further"
    elif total:
        core = (f"watched {distinct} of {total} episodes, up to {_point()} "
                f"(has NOT finished the series — more episodes remain)")
    else:
        # No total known: describe the furthest point honestly, claim nothing
        # about an ending.
        core = (f"watched {distinct} episodes, furthest is {_point()} "
                f"(whether the series is finished is unknown — do NOT assume "
                f"they reached the end)")

    last = p.get("last_viewed_at")
    if now and last:
        try:
            core += f"; last watched {_humanize_age((now - last).days)}"
        except (TypeError, ValueError):
            pass
    return core


def get_series_progress(
    entries: list[dict],
    series_key: str,
    media_type: Optional[str] = None,
    *,
    sonarr: Optional[dict] = None,
    total_episodes: Optional[int] = None,
    now: Optional[datetime] = None,
) -> Optional[dict]:
    """Full progress picture for one series: watch-side aggregate + (optional)
    Sonarr season/episode totals + derived ``finished`` / ``phrase`` /
    ``milestone``.

    ``sonarr`` is the per-series stats dict the generator builds from the Sonarr
    library: ``total_seasons``, ``total_episodes`` (aired), ``per_season`` (aired
    episode count per season number), ``status`` ("ended"/"continuing"). When
    present it drives the season-aware framing and a confident ``finished``.
    ``total_episodes`` may instead be passed directly (tests / simple callers).
    With neither, ``finished`` stays None — the curator then never claims an
    ending it cannot verify.
    """
    base = compute_watch_progress(entries, series_key)
    if not base:
        return None

    status = None
    total_seasons = None
    per_season_total: dict = {}
    if sonarr:
        total_episodes = sonarr.get("total_episodes") or total_episodes
        total_seasons = sonarr.get("total_seasons")
        per_season_total = sonarr.get("per_season") or {}
        status = sonarr.get("status")

    psw = base.get("per_season_watched") or {}
    # Episodes watched within real seasons (>=1) — excludes specials (season 0),
    # which is the number to compare against the aired total.
    watched_main = sum(c for s, c in psw.items() if s and s >= 1)
    if not watched_main:
        watched_main = base["distinct_episodes"]

    seasons_completed: list = []
    if per_season_total:
        for s, tot in per_season_total.items():
            if tot and tot > 0 and psw.get(s, 0) >= tot:
                seasons_completed.append(s)
        seasons_completed.sort()

    # "Caught up" = watched everything that has AIRED. "Finished" additionally
    # requires the series to be over (status "ended") — you can't have finished a
    # show that is still releasing episodes (it would just be "caught up").
    caught_up = bool(
        total_episodes and watched_main >= total_episodes and base["furthest_completed"]
    )
    if sonarr and total_episodes:
        finished: Optional[bool] = bool(caught_up and status == "ended")
    elif total_episodes:
        finished = bool(
            base["distinct_episodes"] >= total_episodes and base["furthest_completed"]
        )
    else:
        finished = None

    base["total_episodes"] = total_episodes
    base["total_seasons"] = total_seasons
    base["per_season_total"] = per_season_total
    base["seasons_completed"] = seasons_completed
    base["watched_main"] = watched_main
    base["caught_up"] = caught_up
    base["status"] = status
    base["finished"] = finished
    base["phrase"] = _progress_phrase(base, now=now)
    base["milestone"] = progress_milestone(base)
    return base


def progress_milestone(p: dict) -> dict:
    """Compact, JSON-serialisable snapshot of the position, stored in a proactive
    message's ``trigger_data`` so a future run can tell whether the user has
    moved on enough to be worth asking again. (Kept small on purpose.)"""
    return {
        "furthest_season": p.get("furthest_season"),
        "furthest_episode": p.get("furthest_episode"),
        "distinct_episodes": p.get("distinct_episodes"),
        "finished": p.get("finished"),
    }


def should_reengage_series(
    current: dict,
    last_asked: Optional[dict],
) -> bool:
    """Has the user advanced enough since the last proactive message about this
    series to justify asking again?

    ``last_asked`` is the ``milestone`` dict stored on the most recent prior
    proactive message for this series (or None if we've never asked).

    Re-engage when ANY holds:
      * never asked before;
      * reached a NEW SEASON (furthest_season increased);
      * just FINISHED it (finished flipped True);
      * watched a meaningful chunk more episodes (``_REENGAGE_EPISODE_JUMP``),
        the fallback signal for libraries without season tags.
    Otherwise stay quiet — same position, same (already-answered) question.
    """
    if not last_asked:
        return True

    cur_season = current.get("furthest_season")
    old_season = last_asked.get("furthest_season")
    if cur_season is not None and (old_season is None or cur_season > old_season):
        return True

    if current.get("finished") and not last_asked.get("finished"):
        return True

    cur_eps = current.get("distinct_episodes") or 0
    old_eps = last_asked.get("distinct_episodes") or 0
    if cur_eps - old_eps >= _REENGAGE_EPISODE_JUMP:
        return True

    return False
