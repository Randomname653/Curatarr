"""Wrap-up facts (services/wrapup_facts) over a throwaway in-memory DB.

The collector decides every number a wrap-up shows, so the numbers are
pinned here: played time (not item length — the old report summed
duration_ms, so Spotify and imported history counted zero and a ten-minute
abandon counted as the whole film), comfort rewatches, binges, drops,
comebacks, eras, the contrarian gem, the Curatarr angle, and that a section
with nothing to say is left out.

    python tests/test_wrapup_facts.py
"""
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.services.wrapup_facts as wf  # noqa: E402
import src.services.wrapup_store as ws  # noqa: E402
from src.database.models import (Base, CachedRecommendation, CuratorResolutionLog,  # noqa: E402
                                 DeletionProposal, MediaTechProfile, SwipeRating, User,
                                 WatchHistoryEntry)

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = Session()
    try:
        yield db
        db.commit()
    finally:
        db.close()


wf.get_db_session = fake_session
ws.get_db_session = fake_session
wf._local_offset = lambda: "+0 minutes"       # the fixture is written in UTC

VERIFIED = {
    "Heat": {"year": 1995, "rating": 6.0, "votes": 5000, "runtime_min": 170},
    "Cats": {"year": 2019, "rating": 8.5, "votes": 50000, "runtime_min": 110},
    "Old Film": {"year": 1950, "rating": 7.0, "votes": 300, "runtime_min": 95},
    "Dark": {"year": 2017, "rating": 6.0, "votes": 1000},
    "Bebop": {"year": 1998, "rating": 8.9, "votes": 90000},
}
wf._verified_lookup = lambda items: {i["title"]: VERIFIED.get(i["title"], {}) for i in items}

NOW = datetime(2026, 10, 20, 12, 0)
MIN = 60_000

with fake_session() as db:
    db.add_all([User(id=1, plex_user_id="1", plex_username="owner", is_admin=True),
                User(id=2, plex_user_id="2", plex_username="guest")])

    def play(title, mt, at, dur_min=None, off_min=None, done=True, series=None,
             season=None, ep=None, source=None):
        db.add(WatchHistoryEntry(
            user_id=1, plex_user_id="1", plex_item_id=f"{series or title}:{season}:{ep}",
            title=title, media_type=mt, series_title=series, season=season, episode=ep,
            viewed_at=at, duration_ms=dur_min and int(dur_min * MIN),
            view_offset_ms=off_min and int(off_min * MIN), completed=done,
            genres="Crime,Drama" if mt == "movie" else "Sci-Fi", source=source))

    play("Heat", "movie", datetime(2025, 1, 1, 20), 170)                 # before the period
    play("Heat", "movie", datetime(2026, 10, 1, 20), 170)                # comfort rewatch 1
    play("Heat", "movie", datetime(2026, 10, 10, 20), 170)               # comfort rewatch 2
    play("Cats", "movie", datetime(2026, 9, 28, 20), 110, 10, done=False)  # a drop: 10 min of 110
    play("Old Film", "movie", datetime(2026, 10, 5, 15))                 # history import: no duration
    for i in range(6):                                                    # a six-episode sitting
        play(f"Ep {i + 1}", "show", datetime(2026, 10, 12, 21) + timedelta(minutes=55 * i),
             50, series="Dark", season=1, ep=i + 1)
    play("Session 1", "anime", datetime(2024, 5, 1, 22), 24, series="Bebop", season=1, ep=1)
    play("Session 2", "anime", datetime(2026, 10, 2, 22), 24, series="Bebop", season=1, ep=2)
    for i in range(10):                                                   # Spotify on repeat
        play("Song", "music", datetime(2026, 10, 15, 9) + timedelta(minutes=4 * i),
             off_min=200 / 60, series="Artist", source="spotify")

    db.add(SwipeRating(user_id=1, category="show", doc_id="sonarr:1", title="Dark",
                       seen=True, stars=9))
    db.add(DeletionProposal(user_id=1, media_id="1", title="Junk", service="radarr",
                            storage_mb=2048, status="deleted", category="movie",
                            resolved_at=datetime(2026, 10, 3)))
    db.add(CuratorResolutionLog(user_id=1, title="Guilty Pleasure", category="movie",
                                outcome="kept", resolution_type="override",
                                curator_stance="disposable franchise noise",
                                override_reason="Nostalgia", created_at=datetime(2026, 10, 4)))
    db.add(MediaTechProfile(plex_rating_key="a", media_type="movie", title="Heat", size_mb=90000))
    db.add(MediaTechProfile(plex_rating_key="b", media_type="movie", title="Avatar", size_mb=60000))
    db.add(CachedRecommendation(user_id=1, category="movie", lane="library", title="Alien",
                                year=1979, reason="Your kind of dread", confidence=0.9))

f = wf.build_wrapup_facts(1, "all", "30d", now=NOW, is_admin=True)
t = f["tempo"]

print("\nplayed time")
check("played_ms: spotify counts its offset", wf.played_ms(
    {"source": "spotify", "view_offset_ms": 200_000, "completed": True}) == (200_000, False))
check("played_ms: a finished play counts its length", wf.played_ms(
    {"completed": True, "duration_ms": 6_000_000}) == (6_000_000, False))
check("played_ms: a partial play counts what was watched", wf.played_ms(
    {"completed": False, "duration_ms": 6_000_000, "view_offset_ms": 600_000}) == (600_000, False))
check("played_ms: an imported finished film is estimated", wf.played_ms(
    {"completed": True, "media_type": "movie"}) == (105 * MIN, True))
# 2×170 Heat + 10 Cats + 105 Old Film (estimated) + 6×50 Dark + 24 Bebop + 10×200 s Song
expected_min = 340 + 10 + 105 + 300 + 24 + 2000 / 60
check(f"hours are played time ({t['hours']} h)", t["hours"] == round(expected_min / 60, 1))
check("estimated share disclosed", t["hours_estimated_share"] == round(105 / expected_min, 2))
check("hours split by type", t["by_type"]["music"] == round(2000 / 3600, 1) and "movie" in t["by_type"])
check("active days counted", t["active_days"] == 8)
check("biggest day is the Dark sitting", t["biggest_day"]["date"] == "2026-10-12")
check("eligible", f["eligible"] is True)

print("\noutliers")
check("comfort title is the twice-rewatched Heat",
      f["comfort"]["title"] == "Heat" and f["comfort"]["replays"] == 2)
check("48-hour binge: six episodes of Dark", f["binge"]["in_48h"]["episodes"] == 6
      and f["binge"]["in_48h"]["series"] == "Dark")
check("deepest sitting: the same six", f["binge"]["deepest_sitting"]["episodes"] == 6)
check("the abandoned Cats is a drop", "Cats" in f["completion"]["dropped_titles"])
check("comeback: Bebop after two years", f["comebacks"][0]["title"] == "Bebop"
      and f["comebacks"][0]["gap_days"] > 365)
check("discovery: Heat and Bebop were known", f["discovery"]["returning_titles"] == 2)
check("contrarian gem: 9 stars for a 6.0", f["contrarian"]["title"] == "Dark"
      and f["contrarian"]["basis"] == "rated" and f["contrarian"]["gap"] == 3.0)
check("unpopular dismissal: the acclaimed drop", f["dismissal"]["title"] == "Cats"
      and f["dismissal"]["how"] == "dropped")
check("viewing age from release years", 1950 <= f["eras"]["viewing_age_year"] <= 2019
      and f["eras"]["oldest"]["title"] == "Old Film")
check("shortest vs longest film", f["eras"]["shortest"]["title"] == "Old Film"
      and f["eras"]["longest"]["title"] == "Heat")
check("music on repeat", f["music"]["on_repeat"]["plays"] == 10
      and f["music"]["top_artists"][0]["artist"] == "Artist")

print("\nthe Curatarr angle")
cu = f["curatarr"]
check("graveyard: 2 GB freed", cu["graveyard"]["gb_freed"] == 2.0 and cu["graveyard"]["deleted"] == 1)
check("shield: one override, its reason", cu["shield"]["overrides"] == 1
      and cu["shield"]["top_reason"] == "Nostalgia")
check("dust collector: the big unwatched title, not the watched one",
      cu["dust_collector"]["title"] == "Avatar")
check("ledger flag for the admin only", cu.get("ledger") is True)
check("look-ahead offers the owned-unwatched pick", f["look_ahead"][0]["title"] == "Alien")
check("traits are ranked and known", all(tr["key"] in wf.TRAIT_KEYS for tr in f["traits"])
      and f["traits"] == sorted(f["traits"], key=lambda x: -x["strength"]))

print("\nscopes, periods, honesty")
mv = wf.build_wrapup_facts(1, "movie", "30d", now=NOW)
check("movie scope: no music section", "music" not in mv)
check("movie scope: movie hours only", mv["tempo"]["hours"] == round((340 + 10 + 105) / 60, 1))
check("no ledger flag without admin", "ledger" not in (mv.get("curatarr") or {}))
empty = wf.build_wrapup_facts(2, "all", "30d", now=NOW)
check("a user with no plays gets an empty, ineligible wrap-up",
      empty.get("empty") is True and empty["eligible"] is False)
again = wf.build_wrapup_facts(1, "all", "30d", now=NOW, is_admin=True)
check("same numbers → same hash", again["facts_hash"] == f["facts_hash"])
check("artwork does not change the hash",
      wf.facts_hash({**f, "posters": {"Heat": "x"}}) == f["facts_hash"])
y = wf.resolve_period("year:2025", NOW)
check("a past year is closed", y.closed and y.end == datetime(2026, 1, 1))
check("the current year is open", not wf.resolve_period("year:2026", NOW).closed)
check("previous window of a year is the year before", y.previous[0] == datetime(2024, 1, 1))
for bad in ("year:1800", "7d", "", "year:3000"):
    try:
        wf.resolve_period(bad, NOW)
        check(f"rejects period {bad!r}", False)
    except ValueError:
        check(f"rejects period {bad!r}", True)
av = wf.available(1)
check("selector offers 'all' plus every played medium",
      av["scopes"] == ["all", "movie", "show", "anime", "music"] and av["years"][0] == 2026)

print("\nstore")
ws.save_facts(1, "all", "30d", f)
ws.save_story(1, "all", "30d", f["facts_hash"], {"archetype_title": "X"}, "llm", "m")
row = ws.load(1, "all", "30d")
check("facts and story round-trip", row["facts"]["facts_hash"] == f["facts_hash"]
      and row["story"]["archetype_title"] == "X" and row["story_hash"] == f["facts_hash"])
check("one row per (user, scope, period)", ws.load(1, "movie", "30d") is None)

print("\nwiring")
main = (ROOT / "src/main.py").read_text(encoding="utf-8")
check("wrapup router mounted at /api/wrapup", 'prefix="/api/wrapup"' in main)
r = (ROOT / "src/routers/wrapup.py").read_text(encoding="utf-8")
check("every member's own wrap-up: get_current_user, not require_admin",
      "get_current_user" in r and "require_admin" not in r)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
