"""The deletion-proposal pool (2026-11-01).

Each category keeps up to DELETION_POOL_SIZE open proposals and the view
pages through them ten at a time. Live before: a run wrote 30 anime
proposals, and after a restart the Anime tab showed 9 - the read was
limited to 30 across ALL categories and the tab sliced that list in the
browser. And every run re-judged the whole category, replacing the lot.

    python tests/test_deletion_pool.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta

conn = app_db()
from src.config import settings  # noqa: E402
from src.database.models import DeletionProposal, ProtectedMedia, User  # noqa: E402
import src.routers.recommendations as rec  # noqa: E402
import src.services.recommendations_engine as eng  # noqa: E402

NOW = datetime.utcnow()


with conn.get_db_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    db.commit()


def seed(rows):
    with conn.get_db_session() as db:
        db.query(DeletionProposal).delete()
        db.query(ProtectedMedia).delete()
        db.add_all(rows)
        db.commit()


def prop(mid, cat, conf, status="pending", title=None, age_days=0, svc=None, on=None):
    return DeletionProposal(
        user_id=1, media_id=str(mid), title=title or f"{cat} {mid}", category=cat,
        service=svc or {"movie": "radarr", "music": "lidarr"}.get(cat, "sonarr"),
        confidence=conf, status=status, storage_mb=1024.0,
        created_at=NOW - timedelta(days=age_days), judged_on=on)


# ── the page ────────────────────────────────────────────────────────────────
# 50 anime ranked below 40 shows and 40 movies: the old global top 30 held
# no anime at all.
seed([prop(i, "anime", 0.30 + i / 1000) for i in range(50)]
     + [prop(100 + i, "show", 0.80 + i / 1000) for i in range(40)]
     + [prop(200 + i, "movie", 0.70 + i / 1000) for i in range(40)]
     + [prop(300, "anime", 0.99, status="limbo", title="Parked Anime"),
        prop(301, "anime", 0.95, status="rejected"),
        prop(302, "anime", 0.95, status="superseded")])


def page(category=None, offset=0, limit=10, recent=False):
    return rec._proposal_page(1, category, recent, 7, offset, limit)


p1 = page("anime")
check("the Anime tab reads its own pool: 51 open (50 pending + 1 parked), not the anime "
      "among the top 30 of everything", p1["total"] == 51 and len(p1["proposals"]) == 10)
check("...ten at a time, most confident first, the parked one included",
      p1["proposals"][0]["title"] == "Parked Anime"
      and [p["confidence"] for p in p1["proposals"]] == sorted(
          (p["confidence"] for p in p1["proposals"]), reverse=True))
ids = [p["id"] for q in range(6) for p in page("anime", offset=q * 10)["proposals"]]
check("paging walks the whole pool once, no repeats, no gaps", len(ids) == 51 == len(set(ids)))
check("the All tab pages everything and counts per category",
      page()["total"] == 131 and page()["counts"] == {"anime": 51, "show": 40, "movie": 40})
check("...and the savings are the whole tab's, not the page's", page("show")["total_mb"] == 40 * 1024.0)

with conn.get_db_session() as db:
    db.add_all([ProtectedMedia(user_id=1, identifier=f"anime {i}", category="anime") for i in range(40, 50)])
    db.commit()
p_prot = page("anime")
check("protected titles are left out before the page is cut, so it is still full",
      p_prot["total"] == 41 and len(p_prot["proposals"]) == 10
      and not any(p["title"] in {f"anime {i}" for i in range(40, 50)} for p in p_prot["proposals"]))

# ── what a run keeps ────────────────────────────────────────────────────────
seed([prop(1, "anime", 0.5, on="large"), prop(2, "anime", 0.5, on="small"),
      prop(3, "anime", 0.5, age_days=31), prop(4, "show", 0.5),
      prop(5, "anime", 0.5, status="rejected")])
check("the pool is the category's pending proposals younger than 30 days, with the "
      "profile that judged each", eng._pooled(1, "anime") == {"1": "large", "2": "small"})


def cand(arr_id):
    return {"item": {"arr_id": arr_id, "title": f"t{arr_id}"}, "score": 50}


saved = settings.DELETION_POOL_SIZE
try:
    settings.DELETION_POOL_SIZE = 12
    kept, rest, cuts, cap = eng._pool_plan([cand(1), cand(7), cand(8)], {"1": None, "2": None})
    check("a stored proposal that is still a candidate stands; one that is not is not kept",
          kept == {"1"})
    check("...the run judges only the others, for the open slots, six verdicts a slot",
          [c["item"]["arr_id"] for c in rest] == [7, 8] and (cuts, cap) == (11, 66))
    kept, rest, cuts, cap = eng._pool_plan([cand(i) for i in range(1, 15)],
                                           dict.fromkeys(map(str, range(1, 13))))
    check("a full pool judges nothing", (cuts, cap) == (0, 0) and len(kept) == 12)

    # The node back from abroad: what the server's smaller model judged
    # while it was away is judged again; its own verdicts and older rows
    # (no profile) stand. The server keeps the node's verdicts.
    pool = {"1": "small", "2": "large", "3": None, "4": "small"}
    scored = [cand(i) for i in range(1, 6)]
    kept, rest, cuts, cap = eng._pool_plan(scored, pool, "large")
    check("the node re-judges the server's verdicts; its own and unknown ones stand",
          kept == {"2", "3"} and [c["item"]["arr_id"] for c in rest] == [1, 4, 5]
          and cuts == 10)
    kept, *_ = eng._pool_plan(scored, pool, "small")
    check("...the server keeps every verdict, the node's included", kept == {"1", "2", "3", "4"})
    kept, *_ = eng._pool_plan(scored, pool, None)
    check("...and with no judge host at all (the node away, no fallback yet) all stand",
          kept == {"1", "2", "3", "4"})
    settings.DELETION_POOL_SIZE = 5000
    check("the pool size is clamped (10-200)", eng.pool_size() == 200)
finally:
    settings.DELETION_POOL_SIZE = saved


def retired(category, proposed=()):
    """media_ids a save of ``category`` would retire."""
    cond = rec._replace_scope(category, "sonarr", list(proposed))
    if cond is None:
        return set()
    with conn.get_db_session() as db:
        return {r.media_id for r in db.query(DeletionProposal).filter(
            DeletionProposal.status == "pending", cond)}


seed([prop(1, "anime", 0.5), prop(2, "anime", 0.5), prop(3, "anime", 0.5),
      prop(4, "show", 0.5), prop(9, None, 0.5, svc="sonarr")])
eng.DELETION_RUN_SUMMARY.clear()
eng._record_deletion_run("anime", judged=4, flagged=1, kept={"1"})
check("a finished pool run retires what it did not keep, the category's legacy rows "
      "included, and never another category", retired("anime") == {"2", "3", "9"})
eng._record_deletion_run("anime", judged=0, kept=set())
check("...nothing kept retires the whole category", retired("anime") == {"1", "2", "3", "9"})
eng._record_deletion_run("anime", stopped="waiting for Big Rig")
check("a stopped run retires only what it proposed again", retired("anime", ["2"]) == {"2"}
      and eng.pool_kept("anime") is None)
eng._record_deletion_run("anime", judged=10, flagged=3)
check("a finished run without a pool (the legacy pitch) replaces the category",
      retired("anime") == {"1", "2", "3", "9"})
check("the run banner counts what the pool kept", eng.DELETION_RUN_SUMMARY.get("anime", {}).get("kept") == 0)
eng.DELETION_RUN_SUMMARY.clear()

check.done()
