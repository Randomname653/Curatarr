"""The Wrap-Up's narrative facts (stage 3.2 of docs/UX_RETENTION_DESIGN.md):
ritual, comfort genre, of the month, night shift, first of the period -
computed in code on the member's clock and offered to the writer as cards,
each claim the fact itself. Nothing here asks a model.

Every title here is invented.

    python tests/test_wrapup_narrative.py
"""
from _harness import app_db, check  # noqa: I001 - first: temp stores

from datetime import datetime, timedelta

conn = app_db()

from src.database.models import User, WatchHistoryEntry  # noqa: E402
from src.services import wrapup_facts as wf  # noqa: E402
from src.services import wrapup_insights as wi  # noqa: E402
from src.services import wrapup_narrative as wn  # noqa: E402

H = 3_600_000


def row(d, h, wk, mo, k, mt="anime", genres="", hours=1.0, day=None):
    return wn.Row(d, h, wk, mo, k, mt, genres, int(hours * H), day or f"{mo}-01")


# ── pure rules ──────────────────────────────────────────────────────────────

weeks = [f"2026-{w:02d}" for w in range(40, 46)]                       # six weeks
tuesdays = [row(2, 21, wk, "2026-10", "Starfall Archive") for wk in weeks[:5]]
# one film a week, each on another day: no slot of its own
noise = [row(i % 7, 14 if i % 2 else 9, wk, "2026-10", f"Film {i}", "movie") for i, wk in enumerate(weeks)]
r = wn.ritual(tuesdays + noise)
check("a ritual: the same weekday evening, the same title, on most weeks",
      r == {"weekday": "Tuesday", "daypart": "evening", "title": "Starfall Archive", "media_type": "anime",
            "weeks": 5, "of_weeks": 6, "hours": 5.0}, r)
mixed = [row(2, 21, wk, "2026-10", f"Show {i}", "anime") for i, wk in enumerate(weeks[:4])] + noise
check("no single title, but the same kind of title: a ritual of that kind",
      wn.ritual(mixed)["title"] is None and wn.ritual(mixed)["media_type"] == "anime")
check("two weeks are no ritual", wn.ritual(tuesdays[:2]) == {})
check("a slot on under half the weeks is no ritual", wn.ritual(tuesdays[:2] + noise) == {})

rows = [row(6, 20, "w1", "2026-10", "The Lantern Room", "movie", "Mystery, Drama", day="2026-10-03"),
        row(6, 20, "w2", "2026-10", "Gull Point", "movie", "Mystery", hours=2, day="2026-10-10"),
        row(6, 20, "w3", "2026-10", "Gull Point", "movie", "Mystery", day="2026-10-17"),
        row(6, 20, "w4", "2026-10", "Gull Point", "movie", "Mystery, Animation", day="2026-10-24"),
        row(2, 20, "w4", "2026-10", "Tide Clock", "movie", "Comedy", hours=2, day="2026-10-20")]
check("a comfort genre: across two titles, on four days, most of the hours - no rewatch needed",
      wn.comfort_genre(rows) == {"genre": "Mystery", "plays": 4, "share": 0.71,
                                 "titles": ["Gull Point", "The Lantern Room"]}, wn.comfort_genre(rows))
check("one title is the comfort fact, not a genre",
      wn.comfort_genre([r for r in rows if r.key != "The Lantern Room"]) == {})
check("three days are no coming back", wn.comfort_genre(rows[:3] + rows[4:]) == {})
check("a medium is not a genre: 'Animation' never wins",
      wn.comfort_genre([row(1, 20, "w", "2026-10", f"Show {i}", "anime", "Animation", day=f"2026-10-0{i + 1}")
                        for i in range(6)]) == {})

months = ([row(1, 20, "w", "2026-09", "The Hollow Pines", "music", hours=6)]
          + [row(1, 20, "w", "2026-09", "Other", "music", hours=2)]
          + [row(1, 20, "w", "2026-10", f"T{i}", "movie", hours=1) for i in range(5)])
check("of the month: the month one title owned; a month nobody owned is left out",
      wn.of_the_month(months) == [{"month": "2026-09", "label": "September", "year": 2026,
                                   "title": "The Hollow Pines", "media_type": "music", "hours": 6.0,
                                   "share": 0.75}])
check("one month is not 'of the month'", wn.of_the_month(months[:2]) == [])

late = [row(3, 23, "w", "m", "Starfall Archive") for _ in range(4)] + [row(3, 2, "w", "m", "Starfall Archive")] \
    + [row(3, 20, "w", "m", "Starfall Archive")] + [row(3, 23, "w", "m", "Night Film", "movie")] * 6
check("night shift: the series that held the late hours (films are no series)",
      wn.night_shift(late) == {"title": "Starfall Archive", "media_type": "anime", "night_plays": 5,
                               "plays": 6, "share": 0.83})
check("two night plays are no shift", wn.night_shift(late[:2]) == {})
# a series watched mostly in the evening, which still held the nights
held = [row(1, 23, "w", "m", "Hollow Tide", "show")] * 4 + [row(1, 20, "w", "m", "Hollow Tide", "show")] * 9 \
    + [row(1, 23, "w", "m", "Night Film", "movie")] * 2
check("...also a series with a minority of its plays at night, when the nights were its",
      wn.night_shift(held) == {"title": "Hollow Tide", "media_type": "show", "night_plays": 4, "plays": 13,
                               "share": 0.31}, wn.night_shift(held))
check("...but not one that only passed through the night", wn.night_shift(
    [row(1, 23, "w", "m", "Hollow Tide", "show")] * 3 + [row(1, 20, "w", "m", "Hollow Tide", "show")] * 12) == {})

# ── the cards ───────────────────────────────────────────────────────────────

facts = {"period": {"key": "year:2026"}, "tempo": {"hours": 40},
         "ritual": r, "comfort_genre": wn.comfort_genre(rows),
         "of_the_month": wn.of_the_month(months), "night_shift": wn.night_shift(late),
         "first_last": {"first": {"title": "Tide Clock", "media_type": "movie", "at": "2026-01-02T10:00:00"}}}
cards = {c["claim"]: c for c in wi._narrative(facts)}
check("each fact becomes one card whose claim is the fact, numbers and titles included",
      set(cards) == {"Tuesday evenings were Starfall Archive: 5 of 6 weeks.",
                     "The genre you came back to: Mystery, 4 plays across Gull Point, The Lantern Room.",
                     "September belonged to The Hollow Pines: 75% of the month's hours, 6 hours.",
                     "Starfall Archive was the after-midnight series: 5 of its 6 plays after 23:00.",
                     "It started with Tide Clock."}, sorted(cards))
check("the cards name what they are about (the writer may only name cited titles)",
      cards["Tuesday evenings were Starfall Archive: 5 of 6 weeks."]["refs"] == ["Starfall Archive"]
      and cards["It started with Tide Clock."]["refs"] == ["Tide Clock"])
check("a week has no 'it started with'", not any(c["claim"].startswith("It started")
                                                  for c in wi._narrative({**facts, "period": {"key": "week:2026-W45"}})))
check("a week across New Year is one ISO week", wn._iso_week("2026-12-31") == wn._iso_week("2027-01-03") == "2026-W53"
      and wn._iso_week("2027-01-04") == "2027-W01")
check("the narrative cards pass the floor and join the ranked set",
      any(c["claim"].startswith("Tuesday evenings") for c in wi.mine({**facts, "top": []})))

# ── from a member's rows, on their clock ────────────────────────────────────

with conn.get_db_session() as db:
    u = User(plex_user_id="wn-1", plex_username="Member", timezone="Europe/Berlin")
    db.add(u)
    db.commit()
    UID = u.id
    # Tuesdays 22:30 in Berlin (21:30 UTC in winter) for five weeks
    first_tue = datetime(2026, 10, 27, 21, 30)
    for i in range(5):
        db.add(WatchHistoryEntry(user_id=UID, plex_user_id="wn-1", plex_item_id=f"k{i}", title=f"Episode {i + 1}",
                                 series_title="Starfall Archive", media_type="anime", season=1, episode=i + 1,
                                 duration_ms=H, view_offset_ms=H, completed=True,
                                 viewed_at=first_tue + timedelta(weeks=i)))
    db.commit()
facts = wf.build_wrapup_facts(UID, "all", "30d", now=datetime(2026, 11, 26, 12, 0))
check("from the member's plays: a Tuesday-evening ritual on their clock",
      facts.get("ritual", {}).get("weekday") == "Tuesday" and facts["ritual"]["daypart"] == "evening"
      and facts["facts_version"] == wf.FACTS_VERSION == 17, facts.get("ritual"))

# ── a realistic month: ~40 hours, a quarter after 23:00, one genre most of it ─

from zoneinfo import ZoneInfo  # noqa: E402

BERLIN = ZoneInfo("Europe/Berlin")


def at(day, hh, mm=0):
    """A wall-clock moment in Berlin as the stored naive UTC."""
    return datetime(2026, 10, day, hh, mm, tzinfo=BERLIN).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)


plays = []
for d in (1, 8, 15, 22, 29):                                   # Thursdays, 20:00: three episodes
    plays += [("Starfall Archive", "anime", "Animation, Fantasy, Adventure", at(d, 20 + i // 2, 30 * (i % 2)), 0.5) for i in range(3)]
for d in (2, 6, 9, 13, 16, 20, 23):                            # the late series, after 23:00
    plays.append(("Hollow Tide", "show", "Fantasy, Drama", at(d, 23, 20), 1.0))
for d in (5, 7, 12, 14, 19, 21, 26, 27, 28):                   # ...and more of it in the evening
    plays.append(("Hollow Tide", "show", "Fantasy, Drama", at(d, 21, 0), 1.0))
for i, d in enumerate((3, 4, 10, 11, 17, 18, 24)):             # films, weekend afternoons
    plays.append((f"Film {i}", "movie", "Mystery, Thriller", at(d, 15, 0), 1.8))
plays += [("Night Film", "movie", "Mystery", at(24, 23, 15), 1.8), ("Late Film", "movie", "Thriller", at(30, 23, 30), 1.8)]
plays += [("Paper Lantern", "anime", "Animation, Fantasy", at(d, 10, 0), 0.5) for d in (3, 10, 17, 31)]
with conn.get_db_session() as db:
    m = User(plex_user_id="wn-2", plex_username="Month", timezone="Europe/Berlin")
    db.add(m)
    db.commit()
    MID = m.id
    for i, (title, mt, genres, when, hours) in enumerate(plays):
        series = title if mt != "movie" else None
        db.add(WatchHistoryEntry(user_id=MID, plex_user_id="wn-2", plex_item_id=f"r{i}", title=f"Part {i}" if series else title,
                                 series_title=series, media_type=mt, season=1 if series else None,
                                 episode=i if series else None, genres=genres, duration_ms=int(hours * H),
                                 view_offset_ms=int(hours * H), completed=True, viewed_at=when))
    db.commit()
oct_facts = wf.build_wrapup_facts(MID, "all", "month:2026-10", now=datetime(2026, 11, 2, 12, 0))
t = oct_facts["tempo"]
check("the fixture is the month seen on real data: about 40 hours, a quarter of them after 23:00",
      38 <= t["hours"] <= 44 and 0.22 <= (t.get("dayparts") or {}).get("night", 0) <= 0.28, (t["hours"], t.get("dayparts")))
check("...its Thursday evenings are a ritual", (oct_facts.get("ritual") or {}).get("title") == "Starfall Archive"
      and oct_facts["ritual"]["weekday"] == "Thursday" and oct_facts["ritual"]["daypart"] == "evening",
      oct_facts.get("ritual"))
check("...fantasy is the genre it came back to, though nothing was rewatched",
      (oct_facts.get("comfort_genre") or {}).get("genre") == "Fantasy"
      and oct_facts["comfort_genre"]["titles"][0] == "Hollow Tide", oct_facts.get("comfort_genre"))
check("...and the late series is the night shift, with under half its plays at night",
      (oct_facts.get("night_shift") or {}).get("title") == "Hollow Tide"
      and oct_facts["night_shift"]["night_plays"] == 7 and oct_facts["night_shift"]["share"] < 0.5,
      oct_facts.get("night_shift"))
month_cards = wi.mine(oct_facts)
check("...and all three reach the writer as cards, with the period's first title",
      {"ritual_title", "comfort_genre", "night_shift", "first_title"} <= {c["key"] for c in month_cards},
      [c["key"] for c in month_cards])

check.done()
