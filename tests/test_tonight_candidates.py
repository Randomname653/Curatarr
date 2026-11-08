"""[TONIGHT CANDIDATES] (stage 2 of docs/UX_RETENTION_DESIGN.md, C2): what
the member could start tonight goes into the chat prompt - the series they
are in the middle of, their stored recommendations in the library lanes and
their Swipe "want to watch" answers - instead of the vector neighbours of
the sentence "what should I watch tonight?". No model call, no new source,
nothing the member has played since it was stored.

Every title here is invented.

    python tests/test_tonight_candidates.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

import json
from datetime import datetime, timedelta

conn = app_db()

from src.database.models import (  # noqa: E402
    CachedRecommendation, SwipeRating, User, WatchHistoryEntry)
from src.services import chat_prompt as cp  # noqa: E402
from src.services import tonight  # noqa: E402

NOW = datetime(2026, 11, 10, 19, 0)
HOUR = 3_600_000

with conn.get_db_session() as db:
    u = User(plex_user_id="tn-1", plex_username="Member")
    db.add(u)
    db.commit()
    UID = u.id

    def play(title, mt, days, series=None, season=None, episode=None, key=None):
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="tn-1", plex_item_id=key or f"{title}-{days}",
                                 title=title, series_title=series, media_type=mt, season=season,
                                 episode=episode, duration_ms=HOUR, view_offset_ms=HOUR, completed=True,
                                 viewed_at=NOW - timedelta(days=days)))

    for n in (1, 2, 3, 4):                                    # in the middle of it
        play(f"Episode {n}", "anime", 4 - n * 0.5, series="Starfall Archive", season=1, episode=n)
    for n in (1, 2):                                          # two plays: not "in progress"
        play(f"Episode {n}", "show", 2, series="Harbour Lights", season=1, episode=n)
    play("Gull Point", "movie", 1)                            # played since it was recommended
    play("The Quiet Harbour", "movie", 900)                   # loved once, long ago

    def rec(title, lane, cat="movie", conf=0.8, anchors=None, cached=NOW - timedelta(days=3), reason=None):
        sf = json.dumps({"kind": "recommendation", "lane": lane, "anchors": anchors,
                         "factors": []}) if anchors else None
        db.add(CachedRecommendation(user_id=UID, category=cat, lane=lane, title=title,
                                    reason=reason or f"Stored reason for {title}.", confidence=conf,
                                    year=2019, cached_at=cached, score_factors=sf))

    rec("The Lantern Room", "because", anchors=["Gull Point", "Tide Clock", "Salt Road"], conf=0.95)
    rec("Gull Point", "library", conf=0.9)                    # played since: out
    rec("North Signal", "library", conf=0.85, reason="A patient coastal mystery " + "with a long tail " * 20)
    rec("The Quiet Harbour", "rediscover", conf=0.8, cached=NOW - timedelta(days=3))
    rec("Far Away Store", "discovery", conf=0.99)             # not in the library: out
    rec("The Hollow Pines", "library", cat="music", conf=0.99)  # music only when asked for music
    for i in range(5):
        rec(f"Extra Film {i}", "library", conf=0.5 - i / 100)

    def swipe(title, want=True, seen=False, days=5, cat="movie"):
        db.add(SwipeRating(user_id=UID, category=cat, doc_id=f"doc-{title}", title=title, seen=seen,
                           want=want, updated_at=NOW - timedelta(days=days), created_at=NOW - timedelta(days=days)))

    swipe("Tide Clock")
    swipe("Gull Point")                                       # played: out
    swipe("Salt Road", days=400)                              # a want from over half a year ago: out
    swipe("Paper Moon Hotel", want=False, seen=True)          # an answer, not a want
    db.commit()

got = tonight.candidates(UID, now=NOW)
titles = [c["title"] for c in got]
check("the series in progress leads, with how far the member is", got[0]["title"] == "Starfall Archive"
      and got[0]["source"] == "in progress" and "episode 4" in got[0]["note"], got[0])
check("two plays are not a series in progress", "Harbour Lights" not in titles, titles)
check("the stored lanes in turn - because, library, rediscover - at most four",
      [c["title"] for c in got if c["source"].startswith(("recommended", "rediscover"))]
      == ["The Lantern Room", "North Signal", "The Quiet Harbour", "Extra Film 0"], titles)
check("a because pick names the three titles it is close to",
      next(c for c in got if c["title"] == "The Lantern Room")["source"]
      == "recommended because they watched Gull Point, Tide Clock and Salt Road")
check("a rediscover pick stays though it was played - before it was stored",
      "The Quiet Harbour" in titles)
check("a title played since it was stored, the discovery lane and music are left out",
      not {"Gull Point", "Far Away Store", "The Hollow Pines"} & set(titles), titles)
check("the stored reason is kept short", all(len(c["note"]) <= 160 for c in got))
check("the Swipe wants: the recent one, not the played one, the old one or a seen answer",
      [c["title"] for c in got if "Swipe" in c["source"]] == ["Tide Clock"], titles)
check("only music when the chat is about music",
      [c["title"] for c in tonight.candidates(UID, domain="music", now=NOW)] == ["The Hollow Pines"])
check("a domain narrows it: films only for a film question",
      {c["category"] for c in tonight.candidates(UID, domain="movie", now=NOW)} == {"movie"})

text = tonight.block(got)
lines = text.splitlines()
check("the block: the header says when to use it, one line per candidate",
      lines[0] == tonight.HEADER and len(lines) == len(got) + 1
      and lines[2] == "- The Lantern Room (2019, film) - recommended because they watched Gull Point, "
                      "Tide Clock and Salt Road: Stored reason for The Lantern Room.", lines[:3])
check("no candidates, no block", tonight.block([]) == ""
      and tonight.tonight_block(424242, now=NOW) == "")
sp = cp.build_system_prompt(voice="neutral", user_lang="en", tonight_context=text)
check("the chat prompt carries it after the library items",
      text in sp and sp.index("RELEVANT LIBRARY ITEMS") < sp.index("[TONIGHT CANDIDATES]"))

check.done()
