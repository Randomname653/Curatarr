"""Curatarr — wrap-up sections built on the new telemetry.

Each section reads one of the durable records added for the wrap-up and
answers one question about the period. ``build_wrapup_facts`` calls them in
its session; each returns {} when there is too little to say, and the
section is then left out.

* ``court_record`` — the debates (CuratorResolutionLog with the judge's
  score, verdict, GB and who convinced whom, services/debate_outcome);
* ``library_velocity`` — GB in vs GB out (services/library_ledger), for
  the owner only: the server is theirs to fill and prune;
* ``time_to_watch`` — how long the period's new titles waited on the
  server before the first play (LibraryItem.added_at);
* ``auteurs`` — the director, studio and actor with the most of the period's
  hours (services/credits_index);
* ``nanogenres`` — the period's hours per named nanogenre, and the ones new to
  the user (services/nanogenres).
"""
import statistics
from collections import Counter, defaultdict
from datetime import datetime
from typing import Optional

_DEFIANT_SCORE = 80        # the judge was this sure, and the title stayed anyway
_TTW_MIN_TITLES = 3
_SAME_WEEK_DAYS = 7
_AUTEUR_MIN_TITLES = 2
_ACTOR_TOP_BILLING = 5     # cast below this billing does not make someone "your actor"


def _in(col, period):
    return (col >= period.start, col < period.end)


# ── the court record ─────────────────────────────────────────────────────────

def court_record(db, user_id: int, period, scope: str) -> dict:
    from src.database.models import CuratorResolutionLog as L
    q = db.query(L).filter(L.user_id == user_id, *_in(L.created_at, period))
    if scope != "all":
        q = q.filter(L.category == scope)
    rows = q.all()
    if not rows:
        return {}
    kept = [r for r in rows if r.outcome == "kept"]
    deleted = [r for r in rows if r.outcome == "deleted"]
    labelled = [r for r in rows if r.persuader_basis in ("rule", "llm")]
    who = Counter(r.persuader for r in labelled)
    out = {
        "decided": len(rows), "kept": len(kept), "deleted": len(deleted),
        "overrides": sum(1 for r in kept if r.resolution_type == "override"),
        "debated": sum(1 for r in rows if (r.user_turns or 0) > 0),
        "won": who.get("user", 0), "lost": who.get("curator", 0),
        "gb_saved": round(sum(r.storage_mb or 0 for r in kept) / 1024, 1),
        "gb_freed": round(sum(r.storage_mb or 0 for r in deleted) / 1024, 1),
    }
    defiant = sorted((r for r in kept if (r.score or 0) >= _DEFIANT_SCORE),
                     key=lambda r: -(r.score or 0))
    out["defiant"] = len(defiant)
    if defiant:
        d = defiant[0]
        out["boldest_keep"] = {"title": d.title, "media_type": d.category,
                               "score": round(d.score), "verdict": d.verdict,
                               "curator_stance": (d.curator_stance or "")[:140] or None}
    talked = [r for r in deleted if r.persuader == "curator"]
    if talked:
        t = max(talked, key=lambda r: r.user_turns or 0)
        out["talked_into"] = {"title": t.title, "media_type": t.category,
                              "turns": t.user_turns or 0}
    won = [r for r in kept if r.persuader == "user"]
    if won:
        w = max(won, key=lambda r: (r.score or 0, r.user_turns or 0))
        out["talked_out_of"] = {"title": w.title, "media_type": w.category,
                                "score": round(w.score) if w.score is not None else None,
                                "turns": w.user_turns or 0}
    return out


# ── GB in vs GB out ──────────────────────────────────────────────────────────

def library_velocity(db, period, types: list) -> dict:
    from src.services.library_ledger import velocity
    v = velocity(period.start, period.end, [t for t in types if t != "music"], db=db)
    if not (v["gb_in"] or v["gb_out"]):
        return {}
    v["verdict"] = ("pruner" if v["gb_out"] > v["gb_in"] * 1.1 else
                    "hoarder" if v["gb_in"] > max(v["gb_out"], 1) * 1.5 else "balanced")
    return v


# ── how long the new titles waited ───────────────────────────────────────────

def time_to_watch(db, W, user_id: int, period, types: list) -> dict:
    """The period's first-ever plays of a title against the day it reached
    the server: median wait, the longest, and how many went within a week."""
    from sqlalchemy import func
    from src.database.models import LibraryItem as LI
    from src.services.wrapup_facts import a_play
    video = [t for t in types if t != "music"]
    if not video or period.key == "all":
        return {}
    key = func.coalesce(W.series_title, W.title)
    first = dict(db.query(key, func.min(W.viewed_at)).filter(
        W.user_id == user_id, W.media_type.in_(video), a_play(W), *_in(W.viewed_at, period))
        .group_by(key).all())
    if not first:
        return {}
    names = [k for k in first if k]
    before = {k for (k,) in db.query(key).filter(
        W.user_id == user_id, W.media_type.in_(video), W.viewed_at < period.start,
        key.in_(names)).distinct()}
    fresh = {k.lower(): (k, at) for k, at in first.items() if k and k not in before}
    if not fresh:
        return {}
    waits = []
    for title, mtype, added in db.query(LI.title, LI.media_type, LI.added_at).filter(
            func.lower(LI.title).in_(list(fresh)), LI.media_type.in_(video),
            LI.added_at.isnot(None)):
        name, at = fresh.get((title or "").lower(), (None, None))
        if name and added < at:
            waits.append(((at - added).days, name, mtype))
    if len(waits) < _TTW_MIN_TITLES:
        return {}
    waits.sort()
    longest = waits[-1]
    return {"titles": len(waits),
            "median_days": int(statistics.median(d for d, _, _ in waits)),
            "same_week_share": round(sum(1 for d, _, _ in waits if d <= _SAME_WEEK_DAYS) / len(waits), 2),
            "longest": {"title": longest[1], "media_type": longest[2], "days": longest[0]},
            "quickest": {"title": waits[0][1], "media_type": waits[0][2], "days": waits[0][0]}}


# ── who made what you watched ────────────────────────────────────────────────

def _work_of(db, t: dict):
    from src.database.models import MediaWork
    mtype = t.get("media_type")
    tries = ([("anilist", t.get("anilist_id")), ("tmdb", t.get("tmdb_id"))] if mtype == "anime"
             else [("tmdb", t.get("tmdb_id"))])
    for src, wid in tries:
        if wid:
            w = db.query(MediaWork).filter_by(category=mtype, id_source=src, work_id=str(wid)).first()
            if w is not None:
                return w
    return None


def auteurs(db, titles: list) -> dict:
    """The director (or creator), the studio (or production company) and the
    actor (top billing, cast or voice) with the most of these hours, each on
    at least two titles. ``titles``: hour-ranked, with ids (``_with_ids``)."""
    from src.database.models import MediaCompany, MediaCredit
    people = {"director": defaultdict(lambda: [0.0, set(), ""]),
              "actor": defaultdict(lambda: [0.0, set(), ""])}
    studios = defaultdict(lambda: [0.0, set(), ""])
    for t in titles:
        w = _work_of(db, t)
        if w is None:
            continue
        h = t.get("hours") or 0
        seen = set()
        for c in db.query(MediaCredit).filter(MediaCredit.work_ref == w.id):
            if c.role in ("director", "creator"):
                bucket = "director"
            elif c.role in ("cast", "voice") and (c.ord or 0) < _ACTOR_TOP_BILLING:
                bucket = "actor"
            else:
                continue
            if (bucket, c.name_key) in seen:
                continue
            seen.add((bucket, c.name_key))
            acc = people[bucket][c.name_key]
            acc[0] += h
            acc[1].add(t["title"])
            acc[2] = acc[2] or c.name
        kinds = {c.kind: c for c in db.query(MediaCompany).filter(MediaCompany.work_ref == w.id)}
        for c in db.query(MediaCompany).filter(MediaCompany.work_ref == w.id,
                                               MediaCompany.kind.in_(("studio", "production"))):
            # an anime's studio is the studio; for the rest the first production company
            if "studio" in kinds and c.kind != "studio":
                continue
            acc = studios[c.name_key]
            if t["title"] in acc[1]:
                continue
            acc[0] += h
            acc[1].add(t["title"])
            acc[2] = acc[2] or c.name

    def best(table) -> Optional[dict]:
        cands = [(v[0], len(v[1]), v) for v in table.values() if len(v[1]) >= _AUTEUR_MIN_TITLES]
        if not cands:
            return None
        hours, n, v = max(cands, key=lambda c: (c[0], c[1]))
        return {"name": v[2], "hours": round(hours, 1), "titles": sorted(v[1])[:4], "count": n}

    out = {k: best(people[k]) for k in ("director", "actor")}
    out["studio"] = best(studios)
    return {k: v for k, v in out.items() if v}


# ── nanogenres ───────────────────────────────────────────────────────────────

_NANO_TOP = 6
_NANO_RIVER = 5            # nanogenres drawn in the river
_NANO_MIN_SHARE = 0.25     # under this share of the hours mapped, say nothing


def nanogenres(db, W, user_id: int, period, types: list) -> dict:
    """The period's played hours per named nanogenre (a title's nanogenre by
    its title and group), the biggest first, and the ones the user had never
    played before the period."""
    from sqlalchemy import func
    from src.database.models import Nanogenre as N, TitleNanogenre as T
    from src.services.nanogenres import group_of
    from src.services.wrapup_facts import a_play, played_ms_sql
    ms = played_ms_sql(W)
    key = func.coalesce(W.series_title, W.title)
    month = func.strftime("%Y-%m", W.viewed_at)
    per_month = [(k, mt, m, float(v or 0)) for k, mt, m, v in
                 db.query(key, W.media_type, month, func.sum(ms)).filter(
                     W.user_id == user_id, W.media_type.in_(types), a_play(W), *_in(W.viewed_at, period))
                 .group_by(key, W.media_type, month) if k and v]
    rolled: dict = defaultdict(float)
    for k, mt, _, v in per_month:
        rolled[(k, mt)] += v
    played = [(k, mt, v) for (k, mt), v in rolled.items()]
    total = sum(v for _, _, v in played)
    if not played or not total:
        return {}
    names = sorted({k for k, _, _ in played})
    genre_of: dict = {}            # (title, group) -> (nanogenre id, name)
    for i in range(0, len(names), 500):
        for t, d, nid, name in (db.query(T.title, T.domain, N.id, N.name)
                                .join(N, N.id == T.nanogenre_id)
                                .filter(T.title.in_(names[i:i + 500]), N.name.isnot(None))):
            genre_of.setdefault((t, group_of(d)), (nid, name))
    hours: dict = defaultdict(float)
    titles: dict = defaultdict(Counter)
    for k, mt, v in played:
        g = genre_of.get((k, group_of(mt)))
        if g:
            hours[g] += v
            titles[g][k] += v
    mapped = sum(hours.values())
    if not hours or mapped < _NANO_MIN_SHARE * total:
        return {}
    before: set = set()
    if period.key != "all":
        for i in range(0, len(names), 500):
            for (k,) in db.query(key).filter(W.user_id == user_id, W.media_type.in_(types),
                                             W.viewed_at < period.start,
                                             key.in_(names[i:i + 500])).distinct():
                before.add(k)
    new = [g for g in hours if not any(t in before for t in titles[g])]
    top = sorted(hours, key=hours.get, reverse=True)
    # the taste river: the leading nanogenres' hours month by month
    lead = {g[1] for g in top[:_NANO_RIVER]}
    river: dict = defaultdict(lambda: defaultdict(float))
    for k, mt, m, v in per_month:
        g = genre_of.get((k, group_of(mt)))
        if g and g[1] in lead:
            river[m][g[1]] += v / 3_600_000
    return {
        "top": [{"name": g[1], "hours": round(hours[g] / 3_600_000, 1),
                 "share": round(hours[g] / mapped, 3),
                 "titles": [t for t, _ in titles[g].most_common(3)]} for g in top[:_NANO_TOP]],
        "new_to_you": [g[1] for g in sorted(new, key=hours.get, reverse=True)[:3]] if period.key != "all" else [],
        "count": len(hours),
        "coverage": round(mapped / total, 3),
        "by_month": {m: {n: round(h, 1) for n, h in sorted(row.items())}
                     for m, row in sorted(river.items())},
    }


# ── consensus: your hours against the community's score ─────────────────────

_CONSENSUS_MIN = 6         # points for a scatter worth drawing
_CONSENSUS_MAX = 40
CONSENSUS_POOL = _CONSENSUS_MAX
_CONSENSUS_VOTES = 200     # a community score below this mass is noise


def consensus(db, user_id: int, titles: list, enriched: dict) -> dict:
    """Every played title with a community score: its score, your hours on
    it, and the judge's last verdict on it (judge_verdicts) — the scatter's
    top-left is what you loved and the crowd did not."""
    from src.database.models import JudgeVerdict as J
    pts = []
    for t in titles[:_CONSENSUS_MAX]:
        e = enriched.get(t["title"]) or {}
        com, votes = e.get("rating"), e.get("votes") or 0
        # AniList scores carry no vote count (0): a known small count is noise
        if com is None or 0 < votes < _CONSENSUS_VOTES:
            continue
        pts.append({"title": t["title"], "media_type": t["media_type"], "hours": t["hours"],
                    "community": round(float(com), 1), "votes": int(votes)})
    if len(pts) < _CONSENSUS_MIN:
        return {}
    verdict: dict = {}
    names = [p["title"] for p in pts]
    for title, v, at in (db.query(J.title, J.verdict, J.last_at)
                         .filter(J.user_id == user_id, J.title.in_(names))
                         .order_by(J.last_at)):
        verdict[title] = v              # the latest wins
    for p in pts:
        v = (verdict.get(p["title"]) or "").upper()
        p["verdict"] = "cut" if v in ("CUT", "STAGNANT") else ("kept" if v else None)
    hrs = sorted(p["hours"] for p in pts)
    mid_h, mid_c = hrs[len(hrs) // 2], sorted(p["community"] for p in pts)[len(pts) // 2]
    yours = [p["title"] for p in pts if p["hours"] > mid_h and p["community"] < mid_c]
    return {"points": pts, "median_hours": mid_h, "median_community": mid_c,
            "yours_alone": yours[:3]}
