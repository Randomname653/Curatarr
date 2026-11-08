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


def row(d, h, wk, mo, k, mt="anime", genres="", hours=1.0):
    return (d, h, wk, mo, k, mt, genres, int(hours * H))


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

rows = [row(6, 20, "w1", "2026-10", "The Lantern Room", "movie", "Mystery, Drama"),
        row(6, 20, "w2", "2026-10", "Gull Point", "movie", "Mystery"),
        row(6, 20, "w3", "2026-10", "Gull Point", "movie", "Mystery")]
check("a comfort genre: shared by two comfort titles, three plays at least",
      wn.comfort_genre(rows, {"comfort_titles": ["The Lantern Room", "Gull Point"]})
      == {"genre": "Mystery", "plays": 3, "titles": ["Gull Point", "The Lantern Room"]})
check("one comfort title is the comfort fact, not a genre",
      wn.comfort_genre(rows, {"comfort_titles": ["Gull Point"]}) == {})

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
check("night shift: a series most of whose plays were after 23:00 (films are no series)",
      wn.night_shift(late) == {"title": "Starfall Archive", "media_type": "anime", "night_plays": 5,
                               "plays": 6, "share": 0.83})
check("three plays are no shift", wn.night_shift(late[:3]) == {})

# ── the cards ───────────────────────────────────────────────────────────────

facts = {"period": {"key": "year:2026"}, "tempo": {"hours": 40},
         "ritual": r, "comfort_genre": wn.comfort_genre(rows, {"comfort_titles": ["The Lantern Room", "Gull Point"]}),
         "of_the_month": wn.of_the_month(months), "night_shift": wn.night_shift(late),
         "first_last": {"first": {"title": "Tide Clock", "media_type": "movie", "at": "2026-01-02T10:00:00"}}}
cards = {c["claim"]: c for c in wi._narrative(facts)}
check("each fact becomes one card whose claim is the fact, numbers and titles included",
      set(cards) == {"Tuesday evenings were Starfall Archive: 5 of 6 weeks.",
                     "The genre you came back to: Mystery, 3 plays across Gull Point, The Lantern Room.",
                     "September belonged to The Hollow Pines: 75% of the month's hours, 6 hours.",
                     "Starfall Archive was the after-midnight series: 5 of its 6 plays after 23:00.",
                     "It started with Tide Clock."}, sorted(cards))
check("the cards name what they are about (the writer may only name cited titles)",
      cards["Tuesday evenings were Starfall Archive: 5 of 6 weeks."]["refs"] == ["Starfall Archive"]
      and cards["It started with Tide Clock."]["refs"] == ["Tide Clock"])
check("a week has no 'it started with'", not any(c["claim"].startswith("It started")
                                                  for c in wi._narrative({**facts, "period": {"key": "week:2026-W45"}})))
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
      and facts["facts_version"] == wf.FACTS_VERSION == 15, facts.get("ritual"))

check.done()
