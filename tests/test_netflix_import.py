"""Netflix viewing history for household members (2026-10-25,
services/netflix_import + services/netflix_match).

A Netflix play counts like a Plex play: the same watch_history row with
source="netflix", its title matched onto the library, its episodes counted
even without a number, its real watched time in the hours, and no Plex
writer that rewrites or hides it.

    python tests/test_netflix_import.py
"""
import asyncio
import io
import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy.orm import sessionmaker  # noqa: E402

import src.database.connection as conn  # noqa: E402
from src.database.models import (ArrEnrichmentStatus as AES, Base, User,  # noqa: E402
                                 WatchHistoryEntry as W)
from src.services import netflix_import as ni, netflix_match as nm  # noqa: E402

from _checks import check  # noqa: E402


# ── the title ───────────────────────────────────────────────────────────────
p = ni.parse_title
check("an episode: show, season, number, episode title",
      p("The Crown: Season 1: Wolferton Splash (Episode 1)")
      == {"media_type": "show", "series_title": "The Crown", "season": 1, "episode": 1, "title": "Wolferton Splash"})
check("German: Staffel and Folge", p("Dark: Staffel 2: Anfänge und Enden (Folge 1)")["season"] == 2
      and p("Dark: Staffel 2: Anfänge und Enden (Folge 1)")["episode"] == 1)
check("a limited series is season 1",
      p("The Queen's Gambit: Limited Series: Openings (Episode 1)")["season"] == 1)
check("a part / book names the season", p("Haus des Geldes: Teil 3: Wir sind zurück")["season"] == 3
      and p("Avatar: The Last Airbender: Book One: Water")["series_title"] == "Avatar: The Last Airbender")
check("a film keeps its colon", p("Star Wars: The Force Awakens")
      == {"media_type": "movie", "series_title": None, "season": None, "episode": None,
          "title": "Star Wars: The Force Awakens"})
check("a custom season name: the show, no season, the number kept",
      p("Stranger Things: Stranger Things 2: Chapter One: MADMAX (Episode 1)")["series_title"] == "Stranger Things"
      and p("Stranger Things: Stranger Things 2: Chapter One: MADMAX (Episode 1)")["season"] is None)
check("the Netflix id is stable and case-blind",
      ni.title_ref("Inception") == ni.title_ref(" inception ") and ni.title_ref("Inception").startswith("netflix:"))

# ── the dates of the simple export ──────────────────────────────────────────
check("German dates", ni.date_format(["25.10.26", "24.10.26"]) == "%d.%m.%y")
check("US dates, newest first", ni.date_format(["10/09/26", "10/08/26", "09/30/26"]) == "%m/%d/%y")
check("day-first slashes, newest first", ni.date_format(["09/10/26", "08/10/26", "30/09/26"]) == "%d/%m/%y")

# ── the full export ─────────────────────────────────────────────────────────
FULL = """Profile Name,Start Time,Duration,Attributes,Title,Supplemental Video Type,Device Type,Bookmark,Latest Bookmark,Country
Anna,2026-10-20 20:00:00,00:50:00,,Dark: Staffel 1: Geheimnisse (Folge 1),,Samsung TV,00:50:00,00:50:00,DE (Germany)
Anna,2026-10-20 21:00:00,00:48:00,,Dark: Staffel 1: Lügen (Folge 2),,Samsung TV,00:48:00,00:48:00,DE (Germany)
Anna,2026-10-20 21:50:00,00:47:00,,Dark: Staffel 1: Vergangenheit und Gegenwart,,Samsung TV,00:47:00,00:47:00,DE (Germany)
Anna,2026-10-19 19:00:00,00:01:30,,Inception,,Samsung TV,00:01:30,Not latest view,DE (Germany)
Anna,2026-10-19 19:05:00,02:20:00,,Inception,,Samsung TV,02:20:00,02:20:00,DE (Germany)
Anna,2026-10-19 18:00:00,00:02:10,,Dark: Staffel 1 (Trailer),TRAILER,Samsung TV,00:02:10,00:02:10,DE (Germany)
Ben,2026-10-18 22:00:00,00:30:00,,Unknown Film,,iPhone,00:30:00,00:30:00,DE (Germany)
"""
r = ni.read_rows(FULL)
check("the full export: profiles, trailers and short starts left out",
      r["kind"] == ni.FULL and r["profiles"] == {"Anna": 4, "Ben": 1}
      and r["dropped"] == {"trailer or preview": 1, "under two minutes": 1})
inc = next(x for x in r["rows"] if x["title"] == "Inception")
check("a play is stamped when it ends, like Plex, with its watched time and device",
      inc["viewed_at"] == datetime(2026, 10, 19, 21, 25) and inc["watched_ms"] == 140 * 60_000
      and inc["device"] == "Samsung TV" and not inc["date_only"])
check("watched through: past the estimate, or 90 % of a known runtime",
      ni.completed(inc) and not ni.completed({**inc, "bookmark_ms": 30 * 60_000})
      and ni.completed({**inc, "bookmark_ms": 95 * 60_000}, runtime_ms=100 * 60_000))

SIMPLE = '"Title","Date"\n"Dark: Staffel 1: Lügen","25.10.26"\n"Inception","24.10.26"\n'
r2 = ni.read_rows(SIMPLE, "NetflixViewingHistory-ab.csv")
check("the simple export: one profile named by its file, the day only",
      r2["kind"] == ni.SIMPLE and r2["profiles"] == {"NetflixViewingHistory-ab.csv": 2}
      and r2["rows"][0]["viewed_at"] == datetime(2026, 10, 25, 12) and r2["rows"][0]["date_only"]
      and ni.completed(r2["rows"][0]))

# ── the upload ──────────────────────────────────────────────────────────────
tmp = Path(tempfile.mkdtemp())
buf = io.BytesIO()
with zipfile.ZipFile(buf, "w") as z:
    z.writestr("netflix-report/CONTENT_INTERACTION/ViewingActivity.csv", FULL)
    z.writestr("netflix-report/ACCOUNT/AccountDetails.csv", "Email\nsomeone\n")
res = ni.save_upload("netflix-report.zip", buf.getvalue(), tmp)
check("the zip: only ViewingActivity.csv is kept, the rest never read",
      len(res["saved"]) == 1 and res["saved"][0].startswith("ViewingActivity-") and not res["rejected"])
res = ni.save_upload("notes.csv", b"a,b\n1,2\n", tmp)
check("a CSV that is not a Netflix history is refused", res["saved"] == [] and len(res["rejected"]) == 1)
ni.save_upload("NetflixViewingHistory.csv", SIMPLE.encode(), tmp)
pend = ni.pending_files(tmp)
check("pending: each file with its kind and profiles",
      sorted(f["kind"] for f in pend) == ["full", "simple"]
      and next(f for f in pend if f["kind"] == "full")["profiles"] == {"Anna": 4, "Ben": 1})

# ── the import, against the library ─────────────────────────────────────────
from _checks import memory_engine  # noqa: E402
engine = memory_engine()
Base.metadata.create_all(engine)
S = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = S()
    try:
        yield db
        db.commit()          # the contract of get_db_session: commit on a clean exit
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


conn.get_db_session = fake_session
db = S()
db.add(User(id=1, plex_user_id="1", plex_username="owner", is_admin=True))
db.add(User(id=2, plex_user_id="22", plex_username="anna"))
db.add(AES(service="radarr", arr_id=5, category="movie", title="Inception", tmdb_id=27205))
db.add(AES(service="sonarr", arr_id=9, category="show", title="Dark", tmdb_id=70523, tvdb_id=334824))
db.add(AES(service="sonarr", arr_id=10, category="show", title="Money Heist", tmdb_id=71446, tvdb_id=327417))
# the owner's Plex play of Inception: its key is the one a Netflix play takes
db.add(W(user_id=1, plex_user_id="1", plex_item_id="4711", title="Inception", media_type="movie",
         viewed_at=datetime(2026, 9, 1), completed=True))
db.commit()
db.close()

searched = []


async def fake_find(kind, name):
    searched.append(name)
    return {"Haus des Geldes": {"tmdb_id": 71446, "tvdb_id": 327417},
            "Unknown Film": {"tmdb_id": 999, "tvdb_id": None}}.get(name)


async def fake_episodes(arr_id):
    return [{"seasonNumber": 1, "episodeNumber": 3, "title": "Vergangenheit und Gegenwart"}] if arr_id == 9 else []


async def fake_seasons(tmdb_id, season):
    # English and German names of Money Heist's first season; "Pilot" is both
    if tmdb_id == 71446 and season == 1:
        return [{"seasonNumber": 1, "episodeNumber": 1, "title": "Episode 1"},
                {"seasonNumber": 1, "episodeNumber": 2, "title": "Pilot"},
                {"seasonNumber": 1, "episodeNumber": 1, "title": "Folge 1"},
                {"seasonNumber": 1, "episodeNumber": 2, "title": "Pilot"}]
    return []


ni.save_upload("h.csv", ('"Title","Date"\n"Haus des Geldes: Teil 1: Folge 1","20.10.26"\n'
                         '"Haus des Geldes: Teil 1: Pilot","19.10.26"\n').encode(), tmp)
assign = {"Anna": 2}                  # Ben and NetflixViewingHistory.csv stay on skip
pend_names = {f["name"]: f for f in ni.pending_files(tmp)}
h_name = next(n for n in pend_names if n.startswith("h-"))
assign[h_name] = 2
stats = asyncio.run(ni.run_import(assign,
                                  import_dir=tmp, find=fake_find, episodes=fake_episodes, seasons=fake_seasons))
db = S()
rows = {(r.series_title or r.title, r.season, r.episode): r for r in db.query(W).filter(W.source == "netflix")}
check("only the assigned profiles are imported (Ben and the skipped file are not)",
      stats["plays"] == 6 and stats["imported"] == 6 and stats["not_assigned"] == 3
      and {r.user_id for r in rows.values()} == {2})
inc_row = rows[("Inception", None, None)]
check("a library film takes the key its Plex plays use, and its TMDB id",
      inc_row.plex_item_id == "4711" and inc_row.tmdb_id == 27205 and inc_row.media_type == "movie")
check("...with the watched time as its played time and the position as its offset",
      inc_row.listened_ms == 140 * 60_000 and inc_row.view_offset_ms == 140 * 60_000
      and inc_row.completed and inc_row.player == "Samsung TV" and inc_row.source_ref == ni.title_ref("Inception"))
check("a library series: the library's title, the episode's own id",
      rows[("Dark", 1, 1)].plex_item_id == ni.title_ref("Dark: Staffel 1: Geheimnisse (Folge 1)")
      and rows[("Dark", 1, 1)].tmdb_id == 70523 and rows[("Dark", 1, 1)].media_type == "show")
check("an episode without a number gets it from Sonarr's list",
      ("Dark", 1, 3) in rows and rows[("Dark", 1, 3)].title == "Vergangenheit und Gegenwart")
check("a German title found through TMDB lands on the library's English one",
      ("Money Heist", 1, 1) in rows and rows[("Money Heist", 1, 1)].date_only
      and "Haus des Geldes" in searched and "Dark" not in searched)
check("...numbered from TMDB's German list of the season, a name both lists share counted once",
      ("Money Heist", 1, 2) in rows)
check("files are moved out of pending once imported", ni.pending_files(tmp) == [])
db.close()

# re-import: nothing twice
for f in (tmp / "imported").glob("*.csv"):
    shutil.copy(f, tmp / f.name)
stats2 = asyncio.run(ni.run_import({"Anna": 2, h_name: 2}, import_dir=tmp, find=fake_find, episodes=fake_episodes, seasons=fake_seasons))
check("importing the same files again adds nothing", stats2["imported"] == 0 and stats2["duplicates"] == 6)

# ── the readers count Netflix like Plex ─────────────────────────────────────
db = S()
for i, t in enumerate(["Ep A", "Ep B", "Ep C", "Ep D"]):     # no numbers at all
    db.add(W(user_id=2, plex_user_id="22", plex_item_id=f"netflix:x{i}", title=t, series_title="Severance",
             media_type="show", season=1, episode=None, viewed_at=datetime(2026, 10, 1, 20 + i),
             completed=True, source="netflix", listened_ms=50 * 60_000))
db.commit()
from src.services import pillars, watch_status  # noqa: E402
others = pillars._other_users_watch(db, 1, {"title": "Severance"}, "show")
check("a member's Netflix binge counts as distinct episodes, and protects the series",
      others and others[0]["distinct_episodes"] == 4 and pillars._household_claim(others, "show", 9) == "anna")
check("the owner line counts episodes the same way",
      pillars._owner_watch(db, 2, {"title": "Severance"}, "show")["episodes"] == 4)
db.close()
wl = watch_status.watched_lookup(2, ["Severance"])
check("watch status: 4 episodes, not 'watched 4x'", wl["Severance"]["episodes"] == 4)

from src.services import wrapup_facts as wf  # noqa: E402
db = S()
ms = wf.played_ms_sql(W)
from sqlalchemy import func  # noqa: E402
got = db.query(func.sum(ms)).filter(W.user_id == 2, W.series_title == "Severance").scalar()
est = db.query(func.sum(wf._estimated_sql(W))).filter(W.user_id == 2, W.series_title == "Severance").scalar()
check("the hours are Netflix's watched time, not an estimate", got == 4 * 50 * 60_000 and est == 0)
base = (W.user_id == 2, W.viewed_at >= datetime(2026, 10, 1), W.viewed_at < datetime(2026, 11, 1))
t = wf._tempo(db, W, ms, base, W.viewed_at, 1, 0, 1)
noon = sum(row[12] for row in t["clock_h"])
check("a day-only play keeps its day, not a clock hour",
      noon == 0 and t["by_day_h"].get("2026-10-20", 0) > 2.5)
reps = wf._comfort([{"series_title": "Severance", "title": x, "season": 1, "episode": None,
                     "completed": True, "media_type": "show"} for x in ("Ep A", "Ep B")], set())
check("two different Netflix episodes are no replay", reps == {})
db.close()

from src.services.viewing_sessions import episode_id, sittings  # noqa: E402
check("episode identity: the number, else the title, none for a film",
      episode_id(1, 3, "x", "S") == (1, 3) and episode_id(1, None, "Ep A", "S") == (1, "title", "Ep A")
      and episode_id(None, None, "Inception", None) is None)
sit = sittings([{"viewed_at": datetime(2026, 10, 25, 12), "date_only": True, "title": "a", "episode": None},
                {"viewed_at": datetime(2026, 10, 25, 12), "date_only": True, "title": "b", "episode": None}],
               now=datetime(2026, 10, 26))
check("day-only plays make no sitting", sit == [])

# ── the relink and the Plex writers ─────────────────────────────────────────
db = S()
db.add(W(user_id=2, plex_user_id="22", plex_item_id=ni.title_ref("Arrival"), title="Arrival", media_type="movie",
         viewed_at=datetime(2026, 10, 2), completed=True, source="netflix", tmdb_id=329865, source_ref="r"))
db.add(AES(service="radarr", arr_id=77, category="movie", title="Arrival", tmdb_id=329865))
db.commit()
n = nm.relink(db)
db.commit()
arr = db.query(W).filter(W.source_ref == "r").one()
check("a film that arrived in the library later is linked by its TMDB id",
      n >= 1 and arr.plex_item_id == "radarr:77")
db.close()
sync = (ROOT / "src/services/plex_sync.py").read_text(encoding="utf-8")
check("the Plex sync never resumes, advances or finishes a Netflix row",
      # resume an in-progress row, promote a stale one, finish a started one on a mark
      sync.count('func.coalesce(WatchHistoryEntry.source, "") != "netflix"') == 3)
check("...nor re-attributes or hides one",
      sync.count('notin_(("spotify", "netflix"))') == 2)
check("the player backfill and the dedupe script leave them alone",
      'notin_(("spotify", "netflix"))' in (ROOT / "src/services/plex_players.py").read_text(encoding="utf-8")
      and '!= "netflix"' in (ROOT / "scripts/dedupe_watch_history.py").read_text(encoding="utf-8"))
cust = (ROOT / "src/services/data_custodian.py").read_text(encoding="utf-8")
check("the custodian relinks daily", 'Task("netflix_relink"' in cust)

# ── the view ────────────────────────────────────────────────────────────────
JS = (ROOT / "frontend/js/netflix_import.js").read_text(encoding="utf-8")
check("no inline style in the import view", 'style="' not in JS and ".style." not in JS)
node = shutil.which("node")
if node:
    import re  # noqa: E402
    body = re.sub(r"^import .*$", "", JS, flags=re.M).replace("export async function", "async function") \
        .replace("export function", "function")
    harness = r"""
const esc = s => String(s ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
const act = n => `data-action="${n}"`; const actOn = () => ''; const EL = 0; const EVENT = 0; const _errMsg = String;
const api = () => ({}); const state = {}; const dropzoneLeave = () => {};
""" + body + """
_users = [{id: 2, plex_username: 'anna'}];
const html = _netflixPendingHtml({pending: [{kind: 'full', profiles: {Anna: 4, Ben: 1}, dropped: {'trailer or preview': 2}},
                                            {kind: 'simple', profiles: {'h.csv': 1}, dropped: {}}]});
const assign = _netflixAssign([{value: '2', dataset: {nfProfile: 'Anna'}}, {value: '', dataset: {nfProfile: 'Ben'}}]);
console.log(JSON.stringify({html, assign, empty: _netflixPendingHtml({pending: []})}));
"""
    hp = Path(tempfile.mkdtemp()) / "h.js"
    hp.write_text(harness, encoding="utf-8")
    rr = subprocess.run([node, str(hp)], capture_output=True, text=True, encoding="utf-8")
    if rr.returncode:
        print(rr.stderr[:1500])
    out = json.loads(rr.stdout.strip().splitlines()[-1]) if rr.returncode == 0 else {}
    h = out.get("html", "")
    check("one member picker per profile, the dates-only file said so, skip first",
          h.count("data-nf-profile=") == 3 and "(one profile, dates only)" in h
          and '<option value="">skip</option>' in h and "2 trailers, previews and short starts left out" in h)
    check("the run sends only the attached profiles", out.get("assign") == {"Anna": 2} and out.get("empty") == "")
else:
    print("  (node not installed - checked statically)")

check.done()
