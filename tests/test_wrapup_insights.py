"""Wrap-up v2, step 1: sections on the new telemetry, and the insight miner.

The v1 fact sheet was ~40 equally weighted lines: nothing said which facts
were remarkable, every tile rendered because data existed, and the number
check rejected honest rounding ("about 58 hours" for 58.3). The new sections
read the debate outcomes, the library ledger, the arrival dates and the
credits index; the miner ranks every fact into a claim card with a score, a
plain "why" and the approved renderings of its numbers, and drops what is
not worth saying.

    python tests/test_wrapup_insights.py
"""
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.services.wrapup_insights as wi  # noqa: E402
import src.services.wrapup_sections as sec  # noqa: E402
from src.database.models import (Base, CuratorResolutionLog, LibraryEvent, LibraryItem,  # noqa: E402
                                 MediaCompany, MediaCredit, MediaWork, WatchHistoryEntry)
from src.services.wrapup_facts import resolve_period  # noqa: E402
from src.services.wrapup_story import numbers_in, render_fact_sheet  # noqa: E402

from _checks import check  # noqa: E402


# ── renderings ───────────────────────────────────────────────────────────────
check("58.3 hours may be said as 58 hours or nearly 60 hours",
      {"58.3 hours", "58 hours", "nearly 60 hours"} <= set(wi.say_hours(58.3)))
check("60 hours may be said as two and a half days", "two and a half days" in wi.say_hours(60))
check("118 minutes may be said as nearly 2 hours", "nearly 2 hours" in wi.say_minutes(118))
check("shares get their words: half, two in five", "half" in wi.say_share(0.5)
      and "two in five" in wi.say_share(0.4) and "a third" in wi.say_share(0.33))
check("412 days may be said as over a year", "over a year" in wi.say_days(412))
check("1,500 GB may be said in TB", "1.5 TB" in wi.say_gb(1500))
check("small counts get their words", wi.say_count(7, "titles") == ["7 titles", "seven titles"])

# ── the miner ────────────────────────────────────────────────────────────────
FACTS = {
    "scope": "all", "period": {"label": "2026 in review"},
    "tempo": {"hours": 58.3, "prev_hours": 30.0, "plays": 90, "active_days": 40,
              "longest_streak_days": 9, "dayparts": {"night": 0.34}, "peak_hour": 22,
              "weekend_share": 0.3,
              "biggest_day": {"date": "2026-03-14", "hours": 8.0, "plays": 9}},
    "top": [{"title": "Severance", "media_type": "show", "hours": 20.0, "plays": 18},
            {"title": "Heat", "media_type": "movie", "hours": 5.6, "plays": 2}],
    "contrarian": {"title": "The Bear", "media_type": "show", "you": 9.0, "community": 6.4,
                   "votes": 2100, "gap": 2.6, "basis": "rated"},
    "court": {"decided": 9, "kept": 5, "deleted": 4, "overrides": 2, "debated": 6, "won": 4,
              "lost": 1, "gb_saved": 120.0, "gb_freed": 300.0, "defiant": 2,
              "boldest_keep": {"title": "Grown Ups 2", "media_type": "movie", "score": 94,
                               "verdict": "CUT", "curator_stance": "disposable"},
              "talked_into": {"title": "Fast X", "media_type": "movie", "turns": 4}},
    "velocity": {"gb_in": 900.0, "gb_out": 300.0, "net_gb": 600.0, "verdict": "hoarder",
                 "biggest_in": {"title": "Dune", "gb": 80}, "biggest_out": None},
    "time_to_watch": {"titles": 6, "median_days": 30, "same_week_share": 0.17,
                      "longest": {"title": "Stalker", "media_type": "movie", "days": 412},
                      "quickest": {"title": "Severance", "media_type": "show", "days": 1}},
    "auteurs": {"director": {"name": "Michael Mann", "hours": 9.5, "count": 3,
                             "titles": ["Collateral", "Heat", "Thief"]}},
    "river": [{"month": "2026-02", "similarity": 0.8, "top_titles": ["Heat"], "hours": 4},
              {"month": "2026-03", "similarity": 0.1, "top_titles": ["Totoro", "Ponyo"], "hours": 6}],
    "discovery": {"new_share": 0.45, "best_new": "Severance", "new_titles": 4, "returning_titles": 6},
}
cards = wi.mine(FACTS)
by_kind = {}
for c in cards:
    by_kind.setdefault(c["slide"], []).append(c)
check("cards are numbered in rank order, best first",
      [c["id"] for c in cards] == [f"I{i:02d}" for i in range(1, len(cards) + 1)]
      and [c["score"] for c in cards] == sorted((c["score"] for c in cards), reverse=True))
check("the boldest keep against a 94% judge ranks near the top",
      any(c["slide"] == "court" and "94%" in c["claim"] for c in cards[:4]))
check("a 9-vs-6.4 rating gap is a twist with its why",
      any(c["slide"] == "twist" and c["refs"] == ["The Bear"] and "gap" in c["why"] for c in cards))
check("the month the taste turned is found in the river",
      any("2026-03 was the month it turned" in c["claim"] for c in cards))
check("a 412-day wait is a confession with its renderings",
      any(c["refs"] == ["Stalker"] and "over a year" in c["say"]["days"] for c in cards))
check("the server's balance names the hoarder", any("a hoarder" in c["claim"] for c in cards))
check("an unremarkable new-title share (45%) is not worth a card",
      not any("new to you" in c["claim"] for c in cards))
weak = wi.mine({"tempo": {"hours": 5, "prev_hours": 5, "active_days": 3, "longest_streak_days": 1,
                          "dayparts": {"night": 0.05}, "biggest_day": {"date": "x", "hours": 1, "plays": 1}},
                "contrarian": {"title": "Meh", "you": 8.0, "community": 6.8, "votes": 300, "gap": 0.2,
                               "basis": "rewatched"}})
check("a quiet period yields few cards, none under the floor",
      all(c["score"] >= wi.FLOOR for c in weak) and len(weak) <= 1)
check("an empty period yields none", wi.mine({"empty": True}) == [])

# ── the fact sheet carries them, and the renderings pass the number check ───
sheet, idx = render_fact_sheet({**FACTS, "traits": []})
check("the sheet ends with the ranked block", "WORTH SAYING (ranked" in sheet
      and sheet.index("WORTH SAYING") > sheet.index("Debates decided"))
check("an honest rounding of a sheet number is allowed now",
      numbers_in("nearly 60 hours, over 55 hours") - {"55"} <= idx["numbers"])
check("the new sections are facts with their titles as references",
      any(v["title"] == "Grown Ups 2" for v in idx["items"].values())
      and any(v["title"] == "Stalker" for v in idx["items"].values())
      and "Most-watched director: Michael Mann" in sheet)

# ── the sections, on a database ──────────────────────────────────────────────
from _checks import memory_engine, with_users  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
with_users(engine, 1)
Session = sessionmaker(bind=engine, autoflush=False)


@contextmanager
def session():
    db = Session()
    try:
        yield db
        db.commit()          # the contract of get_db_session: commit on a clean exit
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


P = resolve_period("year:2026", datetime(2026, 12, 31))
with session() as db:
    for title, outcome, rtype, score, who, gb, turns in (
            ("Grown Ups 2", "kept", "override", 94.0, "none", 30_000, 0),
            ("Twilight", "kept", "consensus", 70.0, "user", 20_000, 3),
            ("Fast X", "deleted", "consensus", 85.0, "curator", 60_000, 4),
            ("Cats", "deleted", "consensus", 90.0, "none", 10_000, 0)):
        db.add(CuratorResolutionLog(user_id=1, title=title, category="movie", outcome=outcome,
                                    resolution_type=rtype, score=score, persuader=who,
                                    persuader_basis="rule", storage_mb=gb, user_turns=turns,
                                    created_at=datetime(2026, 5, 1)))
    db.add(CuratorResolutionLog(user_id=1, title="Old", category="movie", outcome="kept",
                                resolution_type="override", created_at=datetime(2025, 5, 1)))
    db.commit()
    cr = sec.court_record(db, 1, P, "all")
check("the court record counts the year's decisions, wins and losses",
      (cr["decided"], cr["kept"], cr["deleted"], cr["overrides"], cr["won"], cr["lost"])
      == (4, 2, 2, 1, 1, 1))
check("...the boldest keep is the one the judge was surest about",
      cr["boldest_keep"]["title"] == "Grown Ups 2" and cr["defiant"] == 1)
check("...and who talked whom into what", cr["talked_into"]["title"] == "Fast X"
      and cr["talked_out_of"]["title"] == "Twilight" and cr["gb_freed"] == round(70_000 / 1024, 1))

with session() as db:
    for t, added, first in (("Stalker", datetime(2025, 2, 1), datetime(2026, 3, 20)),
                            ("Heat", datetime(2026, 4, 1), datetime(2026, 4, 3)),
                            ("Past Lives", datetime(2026, 5, 1), datetime(2026, 5, 2))):
        db.add(LibraryItem(plex_rating_key=t, media_type="movie", title=t, added_at=added))
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=t, title=t, media_type="movie",
                                 viewed_at=first, completed=True, duration_ms=1))
    db.add(LibraryItem(plex_rating_key="Old Fav", media_type="movie", title="Old Fav",
                       added_at=datetime(2024, 1, 1)))
    for at in (datetime(2024, 6, 1), datetime(2026, 6, 1)):
        db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="of", title="Old Fav",
                                 media_type="movie", viewed_at=at, completed=True, duration_ms=1))
    db.commit()
    ttw = sec.time_to_watch(db, WatchHistoryEntry, 1, P, ["movie", "show", "anime", "music"])
check("time-to-watch: the new titles' waits, the longest, and how many went within a week",
      ttw["titles"] == 3 and ttw["longest"] == {"title": "Stalker", "media_type": "movie", "days": 412}
      and ttw["median_days"] == 2 and ttw["same_week_share"] == round(2 / 3, 2))
check("...a title already seen before the period is no new arrival",
      "Old Fav" not in (ttw["longest"]["title"], ttw["quickest"]["title"]))

with session() as db:
    for i, (title, tmdb) in enumerate((("Heat", 949), ("Collateral", 1538), ("Thief", 11524))):
        w = MediaWork(category="movie", id_source="tmdb", work_id=str(tmdb), title=title)
        db.add(w)
        db.flush()
        db.add(MediaCredit(work_ref=w.id, role="director", name="Michael Mann", name_key="michael mann"))
        db.add(MediaCredit(work_ref=w.id, role="cast", name=f"Lead {i}", name_key=f"lead {i}", ord=0))
        db.add(MediaCompany(work_ref=w.id, kind="production", name="Forward Pass", name_key="forward pass"))
    db.commit()
    au = sec.auteurs(db, [{"title": "Heat", "media_type": "movie", "tmdb_id": 949, "hours": 5.6},
                          {"title": "Collateral", "media_type": "movie", "tmdb_id": 1538, "hours": 2.0},
                          {"title": "Thief", "media_type": "movie", "tmdb_id": 11524, "hours": 2.0},
                          {"title": "Unknown", "media_type": "movie", "tmdb_id": 1, "hours": 9.0}])
check("auteurs: the director behind most of the hours, on at least two titles",
      au["director"] == {"name": "Michael Mann", "hours": 9.6, "titles": ["Collateral", "Heat", "Thief"],
                         "count": 3})
check("...the studio the same way, and no actor on a single title",
      au["studio"]["name"] == "Forward Pass" and "actor" not in au)

with session() as db:
    db.add(LibraryEvent(at=datetime(2026, 2, 1), kind="added", bucket="2026-02", plex_rating_key="d",
                        media_type="movie", title="Dune", size_mb=80 * 1024))
    db.add(LibraryEvent(at=datetime(2026, 3, 1), kind="gone", bucket="x", plex_rating_key="f",
                        media_type="movie", title="Fast X", size_mb=10 * 1024))
    db.commit()
    v = sec.library_velocity(db, P, ["movie", "music"])
check("GB in vs out reads the ledger, music aside, with a verdict",
      v["gb_in"] == 80.0 and v["gb_out"] == 10.0 and v["verdict"] == "hoarder")

src = (Path(__file__).resolve().parents[1] / "src/services/wrapup_facts.py").read_text(encoding="utf-8")
check("the collector builds the sections; the server's balance for the owner only",
      "_sections.court_record(" in src and "_sections.time_to_watch(" in src
      and "_sections.auteurs(" in src and "if is_admin:\n            # the server is the owner's" in src)
r = (Path(__file__).resolve().parents[1] / "src/routers/wrapup.py").read_text(encoding="utf-8")
check("the page gets the ranked cards", '"insights": mine(facts)' in r)

check.done()
