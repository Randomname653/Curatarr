"""Because you watched A, B and C (services/because_recs).

- a pick must sit near ALL three anchors: the lowest centred similarity
  decides, so a title close to one anchor only is out; centring on the
  corpus mean removes the cone every text embedding shares;
- fewer than two usable anchors, another vector size, or a fit under the
  floor: nothing;
- the anchors are the titles finished most in the last 60 days (a series
  once, a binge capped), most recent first among equals, only finished
  plays, only this member and category;
- the card's "Why?" has the shape every rec uses and names the three.

    python tests/test_because_recs.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta

import numpy as np

app_db()
import src.database.connection as conn_mod  # noqa: E402
from src.database.models import User, WatchHistoryEntry as W  # noqa: E402
from src.services import because_recs as br  # noqa: E402

cone = np.array([10.0, 10.0, 10.0, 0, 0])          # what every vector shares
A, B, C = cone + [1, 0, 0, 1, 0], cone + [0, 1, 0, 1, 0], cone + [0, 0, 1, 1, 0]
near_all = cone + [0.3, 0.3, 0.3, 1.2, 0]
near_one = cone + [2, 0, 0, 0, -1]
apart = cone + [0, 0, 0, -1, 1]
mean = cone / np.linalg.norm(cone)
got = br.pick([A, B, C], [(near_one, "one"), (apart, "apart"), (near_all, "all")], mean=mean)
check("only the title near all three stays; near one anchor is not enough",
      [x[1] for x in got] == ["all"] and got[0][0] > br.MIN_FIT)
raw = br.pick([A, B, C], [(near_one, "one"), (near_all, "all")], mean=None, min_fit=0.0)
check("...uncentred, the shared cone makes everything look near (why the corpus mean is used)",
      len(raw) == 2 and min(f for f, _ in raw) > 0.9)
check("fewer than two anchors with a vector: nothing", br.pick([A, None], [(near_all, "all")], mean=mean) == [])
check("another vector size: skipped", br.pick([A, B, C], [(np.ones(3), "x")], mean=mean) == [])

now = datetime(2026, 11, 8, 20, 0)
with conn_mod.get_db_session() as db:
    db.add_all([User(id=1, plex_user_id="1", plex_username="owner", is_admin=True),
                User(id=2, plex_user_id="2", plex_username="member")])
    db.commit()

    def play(uid, cat, title, days, series=None, done=True):
        db.add(W(user_id=uid, plex_user_id=str(uid), plex_item_id=f"{title}{days}", title=title,
                 series_title=series, media_type=cat, viewed_at=now - timedelta(days=days), completed=done))
    for d in (1, 2, 3, 4, 5, 6, 7):
        play(1, "show", f"ep{d}", d, series="Night Shift")             # a binge: counts, capped at five
    play(1, "show", "pilot", 3, series="Office Blocks")
    play(1, "show", "x", 2, series="Harbor")
    play(1, "show", "x2", 10, series="Harbor")
    play(1, "show", "old", 90, series="Old Island")                    # too long ago
    play(1, "show", "half", 1, series="Half Seen", done=False)    # not finished
    play(2, "show", "m", 1, series="Member Show")                # someone else's
    play(1, "movie", "Heat", 1)                                  # another category
    db.commit()
check("the anchors: finished in the last 60 days, a series once, a binge capped, most recent first",
      br.recent_titles(1, "show", now=now) == ["Night Shift", "Harbor", "Office Blocks"])

f = br.because_factors(["Night Shift", "Harbor", "Office Blocks"], 0.12)
check("the card's Why? names the three in the shape every rec uses",
      f["kind"] == "recommendation" and f["lane"] == "because" and f["anchors"] == ["Night Shift", "Harbor", "Office Blocks"]
      and f["factors"][0]["label"] == "Close to all three of Night Shift, Harbor, Office Blocks")

check.done()
