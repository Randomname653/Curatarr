"""Plex: what Plex answers must never read as "your history is orphaned".

* The orphan cleanup listed each section's TOP level (a bare ``/all``: shows,
  artists) while history stores episode and track keys — every TV, anime and
  music play read as orphaned and one click deleted all of it.
* A section answering non-200 was skipped and the delete still ran, so that
  section's history went.
* One 404 on a section deleted its LibraryConfig for good — a misrouting
  proxy or a Plex still starting answers exactly that.

In-memory SQLite and a fake Plex; no network, no real data touched.

    python tests/test_plex_history_safety.py
"""
import asyncio
import sys
import types
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import src.services.plex_sync as ps
from src.database.models import Base, LibraryConfig, User, WatchHistoryEntry

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False},
                       poolclass=StaticPool)
Base.metadata.create_all(engine)
Session = sessionmaker(bind=engine, autoflush=False)


@contextmanager
def fake_db_session():
    db = Session()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


state: dict = {}
ps.get_db_session = fake_db_session
ps.get_state = lambda k: state.get(k)
ps.set_state = lambda k, v: state.__setitem__(k, v)
ps.set_datetime = lambda k, v: None
ps.settings = types.SimpleNamespace(effective_plex_url="http://plex",
                                    effective_plex_token="tok", PLEX_CLIENT_ID="test")

# ── fake Plex ────────────────────────────────────────────────────────────────
# Section 1: movies. Section 2: one show (key 200) with episodes 201..210.
# Section 3: one artist (key 300) with tracks 301..310.
LIB = {
    "1": {"top": ["101", "102", "103"], "1": ["101", "102", "103"]},
    "2": {"top": ["200"], "4": [str(k) for k in range(201, 211)]},
    "3": {"top": ["300"], "10": [str(k) for k in range(301, 311)]},
}
calls: list = []
section_status: dict = {}          # sec -> forced HTTP status
section_empty: set = set()
listing = {"status": 200, "dirs": [{"key": "1"}, {"key": "2"}, {"key": "3"}]}


class FakeResp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None, params=None):
        params = params or {}
        headers = headers or {}
        calls.append((url, dict(params), dict(headers)))
        if url.endswith("/library/sections"):
            return FakeResp(listing["status"], {"MediaContainer": {"Directory": listing["dirs"]}})
        sec = url.rstrip("/").split("/")[-2]
        if sec in section_status:
            return FakeResp(section_status[sec])
        keys = [] if sec in section_empty else LIB[sec].get(params.get("type", "top"), [])
        start = int(headers.get("X-Plex-Container-Start", 0))
        size = int(headers.get("X-Plex-Container-Size", 10 ** 6))
        page = keys[start:start + size]
        return FakeResp(200, {"MediaContainer": {
            "totalSize": len(keys), "Metadata": [{"ratingKey": k} for k in page]}})


ps.httpx = types.SimpleNamespace(AsyncClient=FakeClient, Timeout=lambda *a, **k: None)

with fake_db_session() as db:
    db.add(User(plex_user_id="1", plex_username="owner", is_admin=True))
    db.add(LibraryConfig(plex_section_key="1", plex_section_title="Movies",
                         plex_section_type="movie", media_category="movie"))
    db.add(LibraryConfig(plex_section_key="2", plex_section_title="TV",
                         plex_section_type="show", media_category="show"))
    db.add(LibraryConfig(plex_section_key="3", plex_section_title="Music",
                         plex_section_type="artist", media_category="music"))


def seed(extra_orphans=0):
    from datetime import datetime
    with fake_db_session() as db:
        db.query(WatchHistoryEntry).delete()
        keys = LIB["1"]["1"] + LIB["2"]["4"] + LIB["3"]["10"]
        keys += [str(900 + i) for i in range(extra_orphans)]
        for k in keys:
            db.add(WatchHistoryEntry(user_id=1, plex_user_id="1", plex_item_id=k,
                                     title=f"t{k}", media_type="movie",
                                     viewed_at=datetime(2026, 1, 1)))


def rows():
    with fake_db_session() as db:
        return db.query(WatchHistoryEntry).count()


def run(**kw):
    calls.clear()
    return asyncio.run(ps.cleanup_orphan_watch_history(**kw))


# ── 1. the wipe itself ───────────────────────────────────────────────────────
print("\n-- orphan cleanup reads the item type history stores --")
seed()
r = run()
check("healthy Plex: no episode or track history hidden", r.get("archived") == 0 and rows() == 23)
types_asked = {c[0].split("/")[-2]: c[1].get("type") for c in calls if c[0].endswith("/all")}
check("each section asked for its history type (1 / 4 / 10)",
      types_asked == {"1": "1", "2": "4", "3": "10"})
check("walk is paginated (X-Plex-Container-Start/Size sent)",
      all("X-Plex-Container-Size" in c[2] for c in calls if c[0].endswith("/all")))

seed(extra_orphans=2)
r = run()
check("a real orphan (under the share guard) is hidden", r.get("archived") == 2 and rows() == 23)


def all_rows():
    from src.database import orphaned
    with fake_db_session() as db:
        orphaned.include(db)
        return {k: o is not None for k, o in db.query(WatchHistoryEntry.plex_item_id,
                                                      WatchHistoryEntry.orphaned_at)}


kept = all_rows()
check("...but its plays are kept, marked, for the Wrap-Up",
      len(kept) == 25 and kept["900"] and kept["901"] and not kept[LIB["1"]["1"][0]])
r = run()
check("a second run hides nothing more", r.get("archived") == 0 and r.get("examined") == 23)
LIB["1"]["1"].append("900")             # the item is back in a library
r = run()
check("an item back in a library gets its plays back",
      r.get("returned") == 1 and rows() == 24 and not all_rows()["900"])
LIB["1"]["1"].remove("900")

ps._ORPHAN_PAGE = 3                     # force several pages per section
seed()
r = run()
check("multi-page walk collects every key", r.get("archived") == 0 and rows() == 23)
ps._ORPHAN_PAGE = 2000

# ── 2. unreadable / empty sections ───────────────────────────────────────────
print("\n-- an unreadable or empty section deletes nothing --")
seed(extra_orphans=1)
section_status["2"] = 503
r = run()
check("non-200 section aborts: nothing hidden", r.get("archived") == 0 and "error" in r and rows() == 24)
section_status.clear()

section_empty.add("3")
r = run()
check("section answering 0 items (unmounted share) aborts", r.get("archived") == 0
      and "no items" in r.get("error", "") and rows() == 24)
section_empty.clear()

# ── 3. share guard ───────────────────────────────────────────────────────────
print("\n-- more than a fifth would go: refused unless forced --")
seed(extra_orphans=10)                  # 10 of 33 = 30 %
r = run()
check("refused with the number to confirm", r.get("archived") == 0
      and r.get("would_archive") == 10 and r.get("examined") == 33 and rows() == 33)
r = run(force=True)
check("force=True hides them", r.get("archived") == 10 and rows() == 23)

# ── 4. 404 → library config kept until Plex's own list confirms ──────────────
print("\n-- a 404 alone never removes a library --")
check("listing lacks: only for a usable non-empty listing",
      ps.section_listing_lacks({"MediaContainer": {"Directory": [{"key": "1"}]}}, "2")
      and not ps.section_listing_lacks({"MediaContainer": {"Directory": [{"key": "2"}]}}, "2")
      and not ps.section_listing_lacks({"MediaContainer": {"Directory": []}}, "2")
      and not ps.section_listing_lacks({}, "2")
      and not ps.section_listing_lacks(None, "2"))


async def gone(sec):
    return await ps._section_confirmed_gone(FakeClient(), "http://plex", {}, sec)

state.clear()
check("section still listed: not gone, no streak", not asyncio.run(gone("2"))
      and state.get("plex_section_missing:2") in (None, "0"))
listing["status"] = 503
check("listing errors: not gone, no streak", not asyncio.run(gone("9"))
      and "plex_section_missing:9" not in state)
listing["status"] = 200
listing["dirs"] = []
check("empty listing (Plex starting): not gone", not asyncio.run(gone("9"))
      and "plex_section_missing:9" not in state)
listing["dirs"] = [{"key": "1"}]
seq = [asyncio.run(gone("9")) for _ in range(ps.SECTION_MISS_LIMIT)]
check(f"gone only on the {ps.SECTION_MISS_LIMIT}rd confirmed miss in a row",
      seq == [False] * (ps.SECTION_MISS_LIMIT - 1) + [True])

src = (Path(__file__).resolve().parents[1] / "src/services/plex_sync.py").read_text(encoding="utf-8")
i = src.index("if resp.status_code == 404:")
block = src[i:src.index("if resp.status_code != 200:", i)]
check("sync: an unconfirmed 404 holds the watermark (failed_sections)",
      "_section_confirmed_gone" in block and "failed_sections.append(sec_title)" in block)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
