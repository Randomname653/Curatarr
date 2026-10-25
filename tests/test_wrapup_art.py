"""Wrap-Up artwork (2026-10-25, seen on the real server).

- Chapters about a number (hours, a streak, a day) named no title and kept an
  empty 200 px picture column; now each shows the next of the period's top
  titles no other chapter shows, and with none left the column goes.
- An artist's picture was Deezer's most popular artist of that NAME; it is
  looked up by the MusicBrainz id of the plays (or the raw profile) first.

    python tests/test_wrapup_art.py
"""
import json
import re
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


JS = (ROOT / "frontend/js/wrapup.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/css/app.css").read_text(encoding="utf-8")

# ── the chapters' pictures, rendered in node ────────────────────────────────
node = shutil.which("node")
start = JS.index("function _story(f, d) {")
end = JS.index("\n}\n", start) + 3
story_fn = JS[start:end]
if node:
    harness = r"""
const esc = s => String(s ?? '');
const act = (name, ...a) => `data-action="${name}"`;
const _poster = (f, t) => (f.posters || {})[t] || null;
const _posterImg = (url) => `<img src="${url}">`;
const _isMusic = t => t === 'music';
""" + story_fn + """
const f = {top: [{title: 'ASP', media_type: 'music'}, {title: 'Elfen Lied', media_type: 'anime'},
                 {title: 'No Art', media_type: 'anime'}, {title: 'Immoral Guild', media_type: 'anime'}],
           posters: {'ASP': 'asp.jpg', 'Elfen Lied': 'el.jpg', 'Immoral Guild': 'ig.jpg'}};
const d = {story: {refs: {I04: {title: 'ASP', media_type: 'music'}}, era_chapters: [
  {title: 'Hours'}, {title: 'Streak'}, {title: 'Top', item_refs: ['I04']}, {title: 'Spike'}]}};
const html = _story(f, d);
console.log(JSON.stringify(html.split('<article').slice(1)));
"""
    r = subprocess.run([node, "-e", harness], capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stderr[:1200])
    cards = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else []
    check("a chapter with its own titles shows them",
          len(cards) == 4 and "asp.jpg" in cards[2])
    check("chapters about a number take the next top titles no chapter shows, one each",
          "el.jpg" in cards[0] and "ig.jpg" in cards[1] and "asp.jpg" not in cards[0])
    check("...a title without a picture is skipped, and with none left the column goes",
          "<img" not in cards[3] and "wu-chapter-bare" in cards[3] and 'class="wu-fan"' not in cards[3])
else:
    print("  (node not installed - checked statically)")
check("a bare chapter is one column", ".wu-chapter.wu-chapter-bare { grid-template-columns: 1fr; }" in CSS)

# ── an artist's picture by its id ───────────────────────────────────────────
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base, WatchHistoryEntry as W  # noqa: E402
from src.routers import wrapup as wr  # noqa: E402
import src.cache.metadata_cache as mcm  # noqa: E402
from datetime import datetime  # noqa: E402

engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
S = sessionmaker(bind=engine)


@contextmanager
def fake_session():
    db = S()
    try:
        yield db
    finally:
        db.close()


conn_mod.get_db_session = fake_session
db = S()
for i, mbid in enumerate(["asp-mbid", "asp-mbid", "other"]):
    db.add(W(user_id=1, plex_user_id="1", plex_item_id=f"t{i}", title="Song", media_type="music",
             series_title="ASP", viewed_at=datetime(2026, 10, i + 1), artist_mbid=mbid, completed=True))
db.commit()
db.close()


class FakeCache:
    def get_cache(self, key):
        return {"response": {"mbid": "avatar-mbid"}} if key == "raw:music:Avatar" else None

    def close(self):
        pass


mcm.MetadataCache = FakeCache
got = wr.artist_mbids(["ASP", "Avatar", "Nobody"])
check("an artist's id is the one its plays carry most, else its raw profile's; none is none",
      got == {"ASP": "asp-mbid", "Avatar": "avatar-mbid"})
src = (ROOT / "src/routers/wrapup.py").read_text(encoding="utf-8")
check("the posters pass the id to the picture lookup",
      "mbid=mbids.get(title)" in src and "await run_read(artist_mbids" in src)

from src.services import wrapup_facts as wf  # noqa: E402

check("stored wrap-ups are rebuilt with the new pictures", wf.FACTS_VERSION >= 8)
check("no inline style in the chapters", not re.search(r'style="', story_fn))

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
