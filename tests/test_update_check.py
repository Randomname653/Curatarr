"""The build under the sidebar and the "Update available" check (2026-10-23).

build_info knows which commit runs (the image's CURATARR_BUILD_SHA, or HEAD
of a git checkout read from .git). update_check asks GitHub whether upstream
has published something newer - the last successful docker run on main for
a container, the newest main commit for a checkout - and only a published
commit AHEAD of ours counts. Fakes only: GitHub is never called.

    python tests/test_update_check.py
"""
import asyncio
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.services import build_info as bi  # noqa: E402
from src.services import update_check as uc  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


A = "a" * 40
B = "b" * 40
C = "c" * 40

# ── build_info ──────────────────────────────────────────────────────────────
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    check("no .git and no build env: unknown", bi.build({}, root)["channel"] == "unknown"
          and bi.build({}, root)["sha"] is None)
    b = bi.build({"CURATARR_BUILD_SHA": A, "CURATARR_BUILD_DATE": "2026-10-23T10:00:00Z"}, root)
    check("the image's build env wins: container channel, short sha, date",
          b["channel"] == "container" and b["sha"] == A and b["short"] == "aaaaaaa"
          and b["built"] == "2026-10-23T10:00:00Z" and b["version"])

    git = root / ".git"
    (git / "refs/heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "refs/heads/main").write_text(B + "\n")
    b = bi.build({}, root)
    check("a checkout: HEAD -> loose ref", b["channel"] == "git" and b["sha"] == B)

    (git / "refs/heads/main").unlink()
    (git / "packed-refs").write_text(f"# pack-refs with: peeled\n{C} refs/heads/main\n")
    check("...or packed-refs", bi.git_head(root) == C)

    (git / "HEAD").write_text(A + "\n")
    check("...or a detached HEAD", bi.git_head(root) == A)

    # A worktree: .git is a file naming its dir; refs live in the common dir.
    wt = root / "wt"
    wtdir = git / "worktrees" / "wt"
    wtdir.mkdir(parents=True)
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {wtdir}\n")
    (wtdir / "HEAD").write_text("ref: refs/heads/feature\n")
    (wtdir / "commondir").write_text("../..\n")
    (git / "refs/heads/feature").write_text(B + "\n")
    check("a worktree: its HEAD, the common dir's refs", bi.git_head(wt) == B)

    (git / "HEAD").write_text("ref: refs/heads/gone\n")
    check("a ref that exists nowhere: None, no error", bi.git_head(root) is None)


# ── update_check.refresh, against a fake GitHub ─────────────────────────────
class Resp:
    def __init__(self, code, data):
        self.status_code, self._data = code, data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGitHub:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    async def get(self, url, headers=None):
        self.calls.append(url)
        for frag, (code, data) in self.routes.items():
            if frag in url:
                return Resp(code, data)
        return Resp(404, {})


def runs(sha):
    return (200, {"workflow_runs": [{"head_sha": sha, "updated_at": "2026-10-23T12:00:00Z"}]})


def refresh(build, routes):
    gh = FakeGitHub(routes)
    return asyncio.run(uc.refresh(build, client=gh)), gh


container = {"sha": A, "channel": "container"}
st, gh = refresh(container, {"/actions/workflows/docker.yml/runs": runs(A)})
check("container on the published build: no update, no compare call",
      st["update_available"] is False and st["for_sha"] == A and len(gh.calls) == 1
      and "branch=main" in gh.calls[0] and "status=success" in gh.calls[0])

st, gh = refresh(container, {"/actions/workflows/docker.yml/runs": runs(B),
                             f"/compare/{A}...{B}": (200, {"status": "ahead", "ahead_by": 4})})
check("a newer image published: update, 4 commits newer",
      st["update_available"] is True and st["behind_by"] == 4 and st["latest_sha"] == B)

st, _ = refresh({"sha": A, "channel": "git"},
                {"/commits/main": (200, {"sha": B, "commit": {"committer": {"date": "d"}}}),
                 f"/compare/{A}...{B}": (200, {"status": "behind", "ahead_by": 0})})
check("a checkout AHEAD of main (a dev branch) is not told to update",
      st["update_available"] is False)

st, _ = refresh({"sha": A, "channel": "git"},
                {"/commits/main": (200, {"sha": B}),
                 f"/compare/{A}...{B}": (200, {"status": "diverged", "ahead_by": 2})})
check("diverged (own commits, and upstream's missing): update", st["update_available"] is True)

st, _ = refresh(container, {"/actions/workflows/docker.yml/runs": runs(B)})
check("a build GitHub does not know: no answer, an error to retry",
      st["update_available"] is None and "unknown upstream" in st["error"])

st, _ = refresh(container, {"/actions/workflows/docker.yml/runs": (403, {"message": "rate limit"})})
check("rate limited: an error, never raises", "error" in st and "update_available" not in st)

st, gh = refresh({"sha": None, "channel": "unknown"}, {})
check("no commit to compare: GitHub is not asked", gh.calls == [] and "error" in st)

# ── staleness, summary, kick ────────────────────────────────────────────────
now = time.time()
check("a fresh answer is kept for an hour", not uc.is_stale({"checked_at": now - 3000}, now)
      and uc.is_stale({"checked_at": now - 3601}, now))
check("an error is retried after 30 min", uc.is_stale({"checked_at": now - 1801, "error": "x"}, now)
      and not uc.is_stale({"checked_at": now - 600, "error": "x"}, now))
check("never asked: stale", uc.is_stale({}, now))

s = uc.summarize({"for_sha": A, "update_available": True, "latest_sha": B, "behind_by": 3}, container)
check("summary for this build: latest, how to update", s["update_available"] is True
      and s["latest_short"] == "bbbbbbb" and "TrueNAS" in s["how"])
check("an answer about ANOTHER build (the image changed) does not count",
      uc.summarize({"for_sha": C, "update_available": True}, container) == {"update_available": None})
check("no how-to when nothing is newer",
      "how" not in uc.summarize({"for_sha": A, "update_available": False}, container))

started = []
saved = (uc._settings, uc._running["task"])
import src.services.bg_tasks as bg  # noqa: E402
saved_track = bg.track_task


class Done:
    def done(self):
        return False


def fake_track(coro, name=None):
    coro.close()
    started.append(name)
    return Done()


bg.track_task = fake_track
try:
    uc._settings = lambda: type("S", (), {"UPDATE_CHECK": False})()
    uc._running["task"] = None
    uc.kick(container, {})
    check("UPDATE_CHECK=false: never asks", started == [])
    uc._settings = lambda: type("S", (), {"UPDATE_CHECK": True})()
    uc.kick(container, {"checked_at": time.time()})
    check("a fresh answer: no refresh", started == [])
    uc.kick(container, {})
    uc.kick(container, {})
    check("stale: ONE background refresh, not one per request", started == ["update_check"])
finally:
    uc._settings, uc._running["task"] = saved
    bg.track_task = saved_track

# ── the endpoint ────────────────────────────────────────────────────────────
import src.main as main  # noqa: E402  (importing runs no lifespan)

saved = (uc.aload_state, uc.kick, bi._cache.get("value"))
kicked = []


async def fake_load():
    return {"for_sha": A, "update_available": True, "latest_sha": B, "behind_by": 1}


uc.aload_state = fake_load
uc.kick = lambda b, s: kicked.append(b["sha"])
bi._cache["value"] = dict(container, version="9.9", short="aaaaaaa", built=None)
try:
    member = asyncio.run(main.system_version(user=type("U", (), {"is_admin": False})()))
    check("a member sees the build, not the update status, and starts no check",
          member["short"] == "aaaaaaa" and "update" not in member and kicked == [])
    admin = asyncio.run(main.system_version(user=type("U", (), {"is_admin": True})()))
    check("an admin gets the update status, and a refresh is kicked",
          admin["update"]["update_available"] is True and kicked == [A])
finally:
    uc.aload_state, uc.kick = saved[0], saved[1]
    if saved[2] is None:
        bi._cache.pop("value", None)
    else:
        bi._cache["value"] = saved[2]

# ── wiring ──────────────────────────────────────────────────────────────────
docker = (ROOT / "Dockerfile").read_text(encoding="utf-8")
check("the image carries its commit, after the dependency layer (pip stays cached)",
      "ARG CURATARR_BUILD_SHA" in docker and "ENV CURATARR_BUILD_SHA=$CURATARR_BUILD_SHA" in docker
      and docker.index("pip install") < docker.index("ARG CURATARR_BUILD_SHA")
      and "org.opencontainers.image.revision=$CURATARR_BUILD_SHA" in docker)
wf = (ROOT / ".github/workflows/docker.yml").read_text(encoding="utf-8")
check("the docker workflow passes the commit (from the env, no ${{ }} in the script)",
      '--build-arg CURATARR_BUILD_SHA="$GITHUB_SHA"' in wf
      and 'printenv CURATARR_BUILD_SHA)" = "$GITHUB_SHA"' in wf)
compose = (ROOT / "deploy/docker-compose.yml").read_text(encoding="utf-8")
check("compose offers pull_policy: always, off by default (not every line is fast)",
      "# pull_policy: always" in compose
      and not any(l.strip() == "pull_policy: always" for l in compose.splitlines()))

sys.path.insert(0, str(ROOT / "tests"))
import frontend_files as ff  # noqa: E402
html, js = ff.markup(), ff.script()
check("the sidebar footer has the version and a hidden admin update button",
      'id="sb-version"' in html and 'id="sb-update"' in html
      and 'data-action="showUpdateInfo"' in html
      and html.index('class="sb-footer"') < html.index('id="sb-version"'))
check("...wired: the action is registered and setUser loads the version",
      "  showUpdateInfo,\n" in (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
      and "loadVersion();" in (ROOT / "frontend/js/auth.js").read_text(encoding="utf-8")
      and "/api/system/version" in js)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
