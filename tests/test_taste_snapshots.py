"""Taste snapshots: what each month of watching looked like.

The taste profile was one row per user, overwritten on every rebuild — no
history of it at all. A snapshot is the played-time-weighted mean of a
month's item vectors per user, category and month, back-filled from the
watch history; the running month is rewritten until it closes, a change of
embedding model rebuilds, and readers centre on the corpus mean.

    python tests/test_taste_snapshots.py
"""
import asyncio
import sys
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.services.taste_engine as te  # noqa: E402
import src.services.taste_snapshots as ts  # noqa: E402
from src.database.models import Base, TasteSnapshot, User, WatchHistoryEntry  # noqa: E402

from _checks import check  # noqa: E402


from _checks import memory_engine  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, autoflush=False)


@contextmanager
def fake_session():
    db = Session()
    try:
        yield db
    finally:
        db.close()


ts.get_db_session = fake_session
model = {"name": "embed-a"}
ts._embed_model = lambda: model["name"]

# Item vectors in a 3-d toy space: the crime films point one way, the
# animation another; one title has no vector at all.
VEC = {"Heat": [1, 0, 0], "Thief": [0.9, 0.1, 0], "Collateral": [0.95, 0, 0.05],
       "Totoro": [0, 1, 0], "Spirited Away": [0.05, 0.95, 0], "Mononoke": [0, 0.9, 0.1],
       "No Vector": None}
asked = []


def fake_vector(entry, profile):
    asked.append(entry["title"])
    return VEC.get(entry["title"])


real_item_vector = te._chroma_item_vector
te._chroma_item_vector = fake_vector

with fake_session() as db:
    db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
    for title, months in (("Heat", ["2026-01", "2026-01"]), ("Thief", ["2026-01"]),
                          ("Collateral", ["2026-01"]), ("No Vector", ["2026-01"]),
                          ("Totoro", ["2026-03"]), ("Spirited Away", ["2026-03"]),
                          ("Mononoke", ["2026-03"]), ("Heat", ["2026-04"])):
        for i, m in enumerate(months):
            y, mo = map(int, m.split("-"))
            db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=f"{title}{i}{m}",
                                     title=title, media_type="movie", completed=True,
                                     duration_ms=120 * 60_000, viewed_at=datetime(y, mo, 10 + i, 20)))
    db.commit()

NOW = datetime(2026, 4, 20)
check("every month with plays is due, oldest first, the running month included",
      ts.due(NOW) == [(1, "movie", "2026-01"), (1, "movie", "2026-03"), (1, "movie", "2026-04")])
check("the first run fills them all", asyncio.run(ts.run_taste_snapshots(now=NOW)) is True)
months = ts.series(1, "movie")
jan = months[0]
check("a month keeps its counts and its titles by played time",
      (jan["month"], jan["plays"], jan["titles"], jan["top_titles"][0]) == ("2026-01", 5, 4, "Heat")
      and jan["hours"] == 10.0)
check("its mean leans the way the month's films do", jan["mean"] and jan["mean"][0] > 0.9)
check("a month under three item vectors keeps its counts but no mean",
      months[2]["month"] == "2026-04" and months[2]["mean"] is None and months[2]["plays"] == 1)
check("closed months are not due again; the running month is",
      ts.due(NOW) == [(1, "movie", "2026-04")])

river = ts.month_over_month(months)
check("month over month: crime in January, animation in March — the taste turned",
      river[0] == ("2026-01", None) and river[1][0] == "2026-03" and river[1][1] < 0.2
      and river[2] == ("2026-04", None))
centre = [0.5, 0.5, 0.0]
check("similarity centres on the corpus mean: two crime months stay close, crime and "
      "animation fall apart further",
      ts.similarity([1, 0, 0], [0.9, 0.1, 0], centre) > 0.9
      and ts.similarity([1, 0, 0], [0, 1, 0], centre) < ts.similarity([1, 0, 0], [0, 1, 0]))
check("mismatched or empty means compare to nothing",
      ts.similarity([1, 0], [1, 0, 0]) is None and ts.similarity(None, [1]) is None)

asked.clear()
model["name"] = "embed-b"
check("a new embedding model makes every month due again",
      [k[2] for k in ts.due(NOW)] == ["2026-01", "2026-03", "2026-04"])
check("...and hides the old space's means until they are rebuilt",
      all(m["mean"] is None for m in ts.series(1, "movie")))
asyncio.run(ts.run_taste_snapshots(now=NOW))
with fake_session() as db:
    n = db.query(TasteSnapshot).count()
check("the rebuild rewrites in place, one row per user, category and month",
      n == 3 and ts.series(1, "movie")[0]["mean"] is not None)

ts._BUDGET = 1
model["name"] = "embed-c"
check("a run stops at its budget and says more is due",
      asyncio.run(ts.run_taste_snapshots(now=NOW)) is False and len(ts.due(NOW)) == 2)

from src.services.data_custodian import _registry  # noqa: E402

job = next((t for t in _registry() if t.job_id == "taste_snapshots"), None)
check("the custodian builds them daily, no model", job is not None and not job.needs_llm)

# ── a series is found by its title (seen live) ──────────────────────────────
# The index files a show under the show's Plex key; a history row has the
# episode's. On the real data most anime months found no vector by id.


class FakeChroma:
    docs = {"382475": ({"title": "Cyberpunk: Edgerunners", "media_type": "anime", "year": 2022}, [0, 3, 4]),
            "sonarr:2739": ({"title": "Cyberpunk: Edgerunners", "media_type": "anime", "year": 2022}, [9, 9, 9]),
            "700": ({"title": "Berserk", "media_type": "anime", "year": 1997}, [1, 0, 0]),
            "701": ({"title": "Berserk", "media_type": "anime", "year": 2016}, [0, 1, 0])}

    def get_by_id(self, doc_id):
        return None

    def get_by_title(self, title, media_type):
        return [{"id": k, "metadata": m, "embedding": e} for k, (m, e) in self.docs.items()
                if m["title"] == title and m["media_type"] == media_type]


import src.vector_store.chromadb_wrapper as cw  # noqa: E402

saved_get = cw.get_chroma_db
cw.get_chroma_db = lambda: FakeChroma()
try:
    ep = {"plex_item_id": "382478", "title": "Let You Down", "series_title": "Cyberpunk: Edgerunners",
          "media_type": "anime"}
    check("an episode row finds its show by title, the library doc before the arr-only one",
          real_item_vector(ep, None) == [0.0, 0.6, 0.8])
    b = {"plex_item_id": "9", "title": "Berserk", "series_title": "Berserk", "media_type": "anime"}
    check("two works of one title: none without a year, the right one with the profile's",
          real_item_vector(b, None) is None and real_item_vector(b, {"year": 2016}) == [0.0, 1.0, 0.0])
    check("a film is never matched by title", real_item_vector(
        {"plex_item_id": "1", "title": "Berserk", "media_type": "movie"}, None) is None)
finally:
    cw.get_chroma_db = saved_get

check.done()
