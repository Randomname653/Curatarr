"""What Keep and Delete tell the owner — and that it is true.

2026-10 UI audit: Keep said only "the proposal is closed" while the engine
holds a kept title out for 90 days and then judges it again; there was no
way to protect a title for good from the card. Every delete dialog said
"This cannot be undone" whether or not the arr's Recycle Bin would keep the
files. And a judge call that failed (timeout, malformed JSON) vanished into
the same silence as a title the model could not decide, so a run where the
model never answered looked like a clean library.

Runs against a throwaway in-memory SQLite DB; the arr is a fake client.

    python tests/test_keep_and_delete_feedback.py
"""
import asyncio
import re
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import src.routers.recommendations as recs
import src.services.recommendations_engine as eng
from src.config import settings
from src.database.models import Base, DeletionProposal, ProtectedMedia
from tests.frontend_files import everything

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
Base.metadata.create_all(engine)
db = sessionmaker(bind=engine)()
recs._latest_curator_stance_for_proposal = lambda db, uid, pid, fallback_pitch=None: ("pitch", "CONFIRMED")
USER = SimpleNamespace(id=1, is_admin=True)

for i, title in enumerate(["Plain Keep", "Protected Keep", "Judge Kept", "Already Kept"], start=1):
    db.add(DeletionProposal(id=i, user_id=1, title=title, service="radarr", media_id=str(i),
                            reason="pitch", confidence=0.6, storage_mb=900,
                            status="rejected" if title == "Already Kept" else "pending",
                            category="movie", user_comment="Keeping: my partner loves it" if i == 2 else None))
db.add(ProtectedMedia(user_id=1, identifier="Judge Kept", title="Judge Kept", category="movie",
                      reason="household pillar", source="judge", verdict="HARD_KEEP"))
db.commit()


def reject(pid, protect):
    return asyncio.run(recs.reject_deletion(pid, protect=protect, user=USER, db=db))


def protections(title):
    return db.query(ProtectedMedia).filter_by(user_id=1, identifier=title).all()


# ── Keep vs Protect ──────────────────────────────────────────────────────────

r = reject(1, False)
check("plain Keep closes the proposal", db.get(DeletionProposal, 1).status == "rejected")
check("plain Keep writes NO protection (it is a cooldown)", not protections("Plain Keep"))
check("plain Keep reports protected=False", r.get("protected") is False)

r = reject(2, True)
rows = protections("Protected Keep")
check("protect=true writes one manual protection", len(rows) == 1 and rows[0].source == "manual")
check("the protection carries the owner's note as its reason",
      rows and "partner loves it" in (rows[0].reason or ""))
check("protect=true reports protected=True", r.get("protected") is True)

reject(3, True)
rows = protections("Judge Kept")
check("an existing judge protection is not duplicated or overwritten",
      len(rows) == 1 and rows[0].source == "judge" and rows[0].reason == "household pillar")

r = reject(4, True)
check("protect on an already-kept proposal still protects",
      r.get("already_rejected") and len(protections("Already Kept")) == 1)
reject(4, True)
check("protecting twice leaves one row", len(protections("Already Kept")) == 1)

# ── The Keep copy states the engine's real cooldown ──────────────────────────

ui = everything()
keep_fn = ui.split("export async function rejectDelete")[1][:2000]
days = set(re.findall(r"(\d+) days", keep_fn))
check(f"Keep dialog and toast state the engine's {eng.KEEP_COOLDOWN_DAYS}-day cooldown",
      days == {str(eng.KEEP_COOLDOWN_DAYS)})
check("Keep dialog offers 'Protect permanently'", "Protect permanently" in keep_fn and "protect=true" in keep_fn)
check("the engine's keep filter reads the shared constant",
      "_REJECT_COOLDOWN_DAYS = KEEP_COOLDOWN_DAYS" in Path(eng.__file__).read_text(encoding="utf-8"))

# ── Where deleted files go (the arr's Recycle Bin) ───────────────────────────


class FakeResp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class FakeClient:
    def __init__(self, status=200, body=None, boom=False):
        self.status, self.body, self.boom, self.urls = status, body or {}, boom, []

    async def get(self, url, headers=None):
        self.urls.append(url)
        if self.boom:
            raise OSError("connection refused")
        return FakeResp(self.status, self.body)


settings.RADARR_URL, settings.RADARR_API_KEY = "http://radarr.test", "k"
settings.LIDARR_URL, settings.LIDARR_API_KEY = "http://lidarr.test", "k"
settings.SONARR_URL, settings.SONARR_API_KEY = "", ""

c = FakeClient(body={"recycleBin": "/data/recycle", "recycleBinCleanupDays": 7})
got = asyncio.run(recs._arr_recycle_bin("radarr", client=c))
check("a recycle-bin path means the files are kept, with the cleanup window",
      got == {"recycle_bin": True, "cleanup_days": 7})
check("Radarr is read on its v3 media-management config", c.urls and c.urls[0].endswith("/api/v3/config/mediamanagement"))

c = FakeClient(body={"recycleBin": "", "recycleBinCleanupDays": 7})
check("an empty recycle-bin path means a permanent delete",
      asyncio.run(recs._arr_recycle_bin("lidarr", client=c))["recycle_bin"] is False)
check("Lidarr is read on v1", c.urls and "/api/v1/config/mediamanagement" in c.urls[0])

check("a refused request is unknown, never a guess",
      asyncio.run(recs._arr_recycle_bin("radarr", client=FakeClient(status=401)))["recycle_bin"] is None)
check("a dead arr is unknown, never a guess",
      asyncio.run(recs._arr_recycle_bin("radarr", client=FakeClient(boom=True)))["recycle_bin"] is None)
check("an unconfigured arr is unknown without a request",
      asyncio.run(recs._arr_recycle_bin("sonarr", client=FakeClient()))["recycle_bin"] is None)
check("a Plex delete has no recycle bin",
      asyncio.run(recs._arr_recycle_bin("plex"))["recycle_bin"] is False)

check("no delete dialog claims 'cannot be undone' unconditionally any more",
      "cannot be undone" not in ui)
fate = ui.split("export async function deleteFateLines")[1][:1600]
check("the dialog names the unknown case instead of guessing",
      "Couldn't check whether" in fate and "Recycle Bin" in fate)
check("the dialog mentions the import-list exclusion every arr delete adds",
      "import lists" in fate)
bulk = ui.split("export async function bulkDelete")[1][:1500]
check("bulk delete lists every selected title", "boxes.map(b => b.closest('.card')?.dataset.title" in bulk)

# ── A failed judge call is counted, not silent ───────────────────────────────

src = Path(eng.__file__).read_text(encoding="utf-8")
loop = src.split("judge_failed = 0")[1].split("return final_proposals")[0]
check("an exception in the judge loop counts as a failure",
      re.search(r"pillar judge failed for %r: %s\",\s*item\.get\(\"title\"\), e\)\s*judge_failed \+= 1", loop) is not None)
check("adjudicate's own EVALUATE-with-_error fallback counts as a failure",
      'get("_error")' in loop and loop.index('get("_error")') < loop.index('if v in ("CUT", "STAGNANT")'))
check("the run summary records failures per category",
      '"failed": judge_failed' in loop and "DELETION_RUN_SUMMARY[category]" in loop)

eng.DELETION_RUN_SUMMARY.clear()
check("no run yet → no summary", eng.deletion_run_summary() is None)
eng.DELETION_RUN_SUMMARY["movie"] = {"at": "2026-10-10T08:00:00", "judged": 20, "flagged": 4, "deferred": 2, "failed": 3}
eng.DELETION_RUN_SUMMARY["show"] = {"at": "2026-10-10T09:00:00", "judged": 10, "flagged": 1, "deferred": 0, "failed": 0}
check("one category reads its own run", eng.deletion_run_summary("show")["flagged"] == 1)
allr = eng.deletion_run_summary()
check("the All tab sums every category and takes the latest time",
      allr == {"judged": 30, "flagged": 5, "deferred": 2, "failed": 3, "at": "2026-10-10T09:00:00"})
check("the Deletions view renders the run banner from last_run",
      "_renderRunBanner(r.last_run)" in ui and "couldn't be judged" in ui)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
