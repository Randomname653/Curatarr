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

from sqlalchemy.orm import sessionmaker  # noqa: E402

from _harness import check  # noqa: E402  (first: temp stores and .env, never the live data/)
import src.services.wrapup_facts as wf  # noqa: E402
import src.services.wrapup_store as ws  # noqa: E402
from src.database.models import (Base, CachedRecommendation, CuratorResolutionLog,  # noqa: E402
                                 DeletionProposal, MediaTechProfile, SwipeRating, User,
                                 WatchHistoryEntry)


from _checks import memory_engine  # noqa: E402
engine = memory_engine()
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
import src.services.user_time as ut  # noqa: E402
from datetime import timezone as _tz  # noqa: E402
ut.server_zone = lambda: _tz.utc               # the fixture is written in UTC

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
    db.flush()                  # foreign keys are enforced: the users first

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

print("\nwatch-data generation")
check("facts carry the generation of the watch data they were built on",
      f.get("facts_version") == wf.FACTS_VERSION)
from src.services.viewing_sessions import _watched_enough  # noqa: E402
check("an unfinished play of unknown length is not watched",
      not _watched_enough({"completed": False, "duration_ms": None, "view_offset_ms": 60_000}))
check("a finished play of unknown length is", _watched_enough({"completed": True}))
import asyncio  # noqa: E402
import types  # noqa: E402
import src.routers.wrapup as wr  # noqa: E402

ws.save_facts(1, "all", "year:2025", {"facts_version": 1, "scope": "all", "facts_hash": "old"})
_built = []
_real_build, _real_posters = wf.build_wrapup_facts, wr.attach_posters
wf.build_wrapup_facts = lambda *a, **k: _built.append(a) or {
    "facts_version": wf.FACTS_VERSION, "scope": "all", "facts_hash": "new"}


async def _no_posters(facts):
    return None


wr.attach_posters = _no_posters
try:
    _me = types.SimpleNamespace(id=1, is_admin=True)
    thawed, _ = asyncio.run(wr.current_facts(_me, "all", "year:2025"))
    again, _ = asyncio.run(wr.current_facts(_me, "all", "year:2025"))
finally:
    wf.build_wrapup_facts, wr.attach_posters = _real_build, _real_posters
check("a closed year built on older watch data is rebuilt once",
      thawed["facts_hash"] == "new" and ws.load(1, "all", "year:2025")["facts"]["facts_hash"] == "new")
check("...and frozen again after that", again["facts_hash"] == "new" and len(_built) == 1)

print("\nhousehold isolation")
# Every user-scoped section filters by user on its own (comebacks, discovery,
# prior completions, dismissals, the video rows, music, swipes, the curation
# angle, look-ahead, time-to-watch, nanogenres, consensus, drift, ...). A twin
# member with overlapping, noisy history in every one of those tables proves
# each filter, not only the base one: user 2 had no plays before, so a dropped
# filter in any sub-query stayed green. User 1 first gets the little data the
# sections above never needed, so each section has something to leak into.
from src.database.models import (ConversationMessage, CuratorPrinciple,  # noqa: E402
                                 JudgeVerdict, LibraryItem, MusicSkip, Nanogenre,
                                 PlexRating, ProtectedMedia, TitleNanogenre)

VERIFIED["Arrival"] = {"year": 2016, "rating": 7.9, "votes": 700_000, "runtime_min": 116}
with fake_session() as db:
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="alien-old", title="Alien Old",
                             media_type="movie", viewed_at=datetime(2022, 2, 1, 20),
                             duration_ms=100 * MIN, completed=True))     # old-only: comeback bait
    db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id="arrival", title="Arrival",
                             media_type="movie", viewed_at=datetime(2026, 10, 14, 20),
                             duration_ms=116 * MIN, completed=True, genres="Sci-Fi"))
    for i, title in enumerate(("Cats", "Old Film", "Dark", "Arrival", "Avatar")):
        db.add(LibraryItem(plex_rating_key=f"li{i}", media_type="show" if title == "Dark" else "movie",
                           title=title, added_at=datetime(2026, 9, 1)))   # time-to-watch
    db.add_all([Nanogenre(id=1, grp="video", idx=0, name="heist slow-burn", size=1,
                          centroid=b"\0", version=1),
                Nanogenre(id=2, grp="video", idx=1, name="time-loop mystery", size=1,
                          centroid=b"\0", version=1)])      # new to user 1 this period
    db.add_all([TitleNanogenre(parent="radarr:heat", title="Heat", domain="movie", nanogenre_id=1),
                TitleNanogenre(parent="sonarr:dark", title="Dark", domain="show", nanogenre_id=2)])
period30 = wf.resolve_period("30d", NOW)
before = wf.build_wrapup_facts(1, "all", "30d", now=NOW, is_admin=True)
before_av, before_drift = wf.available(1), wf._drift_rows(1, "movie", period30)
_present = [k for k in ("time_to_watch", "nanogenres", "consensus", "comebacks", "music") if before.get(k)]
check(f"isolation: user 1 has the sections a leak could land in {_present}", len(_present) == 5)

with fake_session() as db:
    def twin(title, mt, at, dur_min=50, done=True, series=None, season=None, ep=None,
             off_min=None, source=None):
        db.add(WatchHistoryEntry(
            user_id=2, plex_user_id="2", plex_item_id=f"{series or title}:{season}:{ep}",
            title=title, media_type=mt, series_title=series, season=season, episode=ep,
            viewed_at=at, duration_ms=int(dur_min * MIN),
            view_offset_ms=off_min and int(off_min * MIN), completed=done,
            genres="Horror", source=source))

    twin("Ghost", "movie", datetime(2023, 3, 1, 20))                  # a comeback, user 2's only
    twin("Ghost", "movie", datetime(2026, 10, 6, 20))
    twin("Alien Old", "movie", datetime(2026, 10, 6, 22))             # user 1's old-only title
    twin("Old Film", "movie", datetime(2022, 5, 1, 20))               # makes it a "comeback"
    twin("Heat", "movie", datetime(2026, 10, 7, 20), 170)             # a title user 1 has too
    twin("Heat", "movie", datetime(2026, 9, 1, 20), 170)              # the previous window
    twin("Cats", "movie", datetime(2026, 10, 8, 20), 110)             # user 1 dropped it
    twin("Avatar", "movie", datetime(2026, 10, 8, 23), 160)           # user 1's dust collector
    for i in range(6):                                                # Dark, finished long ago
        twin(f"Ep {i + 1}", "show", datetime(2025, 6, 1, 21) + timedelta(minutes=55 * i),
             series="Dark", season=1, ep=i + 1)
    twin("Junk Movie", "movie", datetime(2026, 10, 9, 20), 120, done=False)
    for i in range(25):                                               # a louder repeat song
        twin("Other Song", "music", datetime(2026, 10, 16, 3) + timedelta(minutes=4 * i),
             dur_min=4, off_min=200 / 60, series="Other Artist", source="spotify")
    db.add(PlexRating(user_id=2, plex_item_id="m1", media_type="music", rating=10,
                      artist_name="Other Artist"))
    db.add_all([MusicSkip(user_id=2, at=datetime(2026, 10, 16, 2, i), title="Other Song",
                          artist="Other Artist") for i in range(4)])
    db.add(DeletionProposal(user_id=2, media_id="9", title="Bebop", service="sonarr",
                            storage_mb=900_000, status="deleted", category="anime",
                            resolved_at=datetime(2026, 10, 9)))       # acclaimed, beats Cats
    db.add(DeletionProposal(user_id=2, media_id="10", title="Returned One", service="radarr",
                            storage_mb=10, status="deleted", category="movie",
                            returned_at=datetime(2026, 10, 10)))
    db.add(SwipeRating(user_id=2, category="show", doc_id="sonarr:1", title="Dark",
                       seen=True, stars=10))                          # same title, other verdict
    db.add(SwipeRating(user_id=2, category="movie", doc_id="radarr:5", title="Heat",
                       seen=True, stars=1))
    db.add(CuratorResolutionLog(user_id=2, title="Other Pleasure", category="movie",
                                outcome="kept", resolution_type="override",
                                curator_stance="noise", override_reason="Spite",
                                created_at=datetime(2026, 10, 5)))
    db.add(ProtectedMedia(user_id=2, identifier="radarr:77", category="movie", source="manual",
                          title="Shielded", created_at=datetime(2026, 10, 5)))
    db.add(CuratorPrinciple(user_id=2, text="Never delete horror.", category="movie",
                            status="active", activated_at=datetime(2026, 10, 5)))
    for i in range(4):
        db.add(ConversationMessage(user_id=2, role="user", content=f"keep it {i}",
                                   thread_id="deletion_proposal:1",
                                   created_at=datetime(2026, 10, 6, 10, i)))
    db.add(CachedRecommendation(user_id=2, category="movie", lane="library", title="Hereditary",
                                year=2018, reason="Your kind of dread", confidence=0.99))
    db.add(JudgeVerdict(user_id=2, category="movie", item_key="radarr:1", title="Heat",
                        verdict="CUT", last_at=datetime(2026, 10, 10)))
after = wf.build_wrapup_facts(1, "all", "30d", now=NOW, is_admin=True)

check("isolation: user 2 really has a wrap-up of their own",
      wf.build_wrapup_facts(2, "all", "30d", now=NOW)["eligible"] is True)
for key in ("tempo", "comebacks", "discovery", "completion", "comfort", "dismissal", "binge",
            "music", "contrarian", "genres", "curatarr", "look_ahead", "court",
            "time_to_watch", "nanogenres", "consensus"):
    check(f"isolation: user 1's {key} ignores user 2's history", before.get(key) == after.get(key))
# velocity is server-wide on purpose — the owner's library, GB in and out
# (wrapup_facts: "the server is the owner's to fill and to prune").
_HOUSEHOLD_WIDE = {"velocity", "generated_at", "facts_hash"}
leaked = sorted(k for k in set(before) | set(after)
                if k not in _HOUSEHOLD_WIDE and before.get(k) != after.get(k))
check(f"isolation: nothing else on user 1's sheet moved {leaked or ''}", leaked == [])
check("isolation: the period selector offers user 1 only their own years and media",
      wf.available(1) == before_av)
check("isolation: ...and user 2, who never watched anime, is not offered it",
      "anime" not in wf.available(2)["scopes"])
check("isolation: a nanogenre is new to user 1 even if user 2 knew it",
      "time-loop mystery" in (after.get("nanogenres") or {}).get("new_to_you", []))
check("isolation: the taste-drift rows are user 1's own",
      wf._drift_rows(1, "movie", period30) == before_drift)
check("isolation: the server-wide section is the owner's alone",
      "velocity" in after and "velocity" not in wf.build_wrapup_facts(2, "all", "30d", now=NOW))

print("\nwiring")
main = (ROOT / "src/main.py").read_text(encoding="utf-8")
check("wrapup router mounted at /api/wrapup", 'prefix="/api/wrapup"' in main)
r = (ROOT / "src/routers/wrapup.py").read_text(encoding="utf-8")
check("every member's own wrap-up: get_current_user, not require_admin",
      "get_current_user" in r and "require_admin" not in r)

check.done()
