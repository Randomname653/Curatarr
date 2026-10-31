"""The desktop install updates itself (2026-10-23).

src/self_update fast-forwards the checkout to origin/main, and nothing
else: a branch, local changes or local commits stop it with the reason. The
tray offers "Check for updates" / "Update now"; the PC agent reports where
its checkout stands and takes POST /update (token, no parameters), so the
NAS's bell can say "the gaming PC runs an older Curatarr" and update it.

The git part runs real git against throwaway repositories; nothing touches
this checkout.

    python tests/test_self_update.py
"""
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import pc_agent  # noqa: E402
from src import self_update as su  # noqa: E402
from src.services import notifications as nt  # noqa: E402
from src.services import pc_host  # noqa: E402

from _checks import check  # noqa: E402


ID = ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false"]


def sh(cwd, *args):
    p = subprocess.run(["git", *ID, *args], cwd=str(cwd), capture_output=True, text=True)
    assert p.returncode == 0, (args, p.stdout, p.stderr)
    return p.stdout.strip()


def commit(repo, name, text="x"):
    (Path(repo) / name).write_text(text, encoding="utf-8")
    sh(repo, "add", name)
    sh(repo, "commit", "-q", "-m", f"add {name}")


# ── git, for real ───────────────────────────────────────────────────────────
if shutil.which("git") is None:
    print("  SKIP  git is not installed here")
else:
    tmp = Path(tempfile.mkdtemp(prefix="curatarr-selfupdate-"))
    try:
        origin, up, me = tmp / "origin.git", tmp / "upstream", tmp / "install"
        sh(tmp, "init", "-q", "--bare", "-b", "main", str(origin))
        sh(tmp, "clone", "-q", str(origin), str(up))
        sh(up, "checkout", "-q", "-b", "main")
        commit(up, "a.txt")
        sh(up, "push", "-q", "origin", "main")
        sh(tmp, "clone", "-q", "-b", "main", str(origin), str(me))

        st = su.check(root=me)
        check("current: nothing to do, nothing wrong",
              st["behind"] == 0 and st["ahead"] == 0 and not st["can_update"] and st["reason"] == "")
        r = su.apply(root=me)
        check("...apply says up to date", r["ok"] and not r["updated"])

        commit(up, "b.txt")
        commit(up, "c.txt")
        sh(up, "push", "-q", "origin", "main")
        st = su.check(root=me)
        check("two commits upstream: behind 2, can update", st["behind"] == 2 and st["can_update"])
        before = sh(me, "rev-parse", "HEAD")
        r = su.apply(root=me)
        after = sh(me, "rev-parse", "HEAD")
        check("apply fast-forwards to origin/main",
              r["ok"] and r["updated"] and r["from"] == before and r["to"] == after
              and after == sh(up, "rev-parse", "HEAD") and (me / "c.txt").exists())

        commit(up, "d.txt")
        sh(up, "push", "-q", "origin", "main")
        (me / "a.txt").write_text("my edit", encoding="utf-8")
        st = su.check(root=me)
        r = su.apply(root=me)
        check("local changes to tracked files: refused, the edit kept",
              not st["can_update"] and "local changes" in st["reason"] and not r["ok"]
              and (me / "a.txt").read_text(encoding="utf-8") == "my edit")
        sh(me, "checkout", "-q", "--", "a.txt")

        (me / "notes.txt").write_text("untracked", encoding="utf-8")
        check("an untracked file does not block", su.check(root=me)["can_update"])

        sh(me, "checkout", "-q", "-b", "feature")
        st = su.check(root=me)
        check("on another branch: refused", not st["can_update"] and "on feature" in st["reason"])
        sh(me, "checkout", "-q", "main")

        commit(me, "mine.txt")
        st = su.check(root=me)
        r = su.apply(root=me)
        check("a local commit main does not have: refused, nothing merged",
              not st["can_update"] and "1 local commit" in st["reason"] and not r["ok"]
              and st["ahead"] == 1 and st["behind"] == 1)

        check("not a checkout: said so", su.check(root=tmp)["reason"] == "not a git checkout")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def no_git(*a, **k):
    raise FileNotFoundError("git")


st = su.check(root=ROOT, run=no_git)
check("git missing: a reason, never an exception", "not installed" in st["reason"] and not st["can_update"])

# ── the checker the agent reports from ──────────────────────────────────────
calls = []


def fake_check():
    calls.append(1)
    return {"head": "a" * 40, "latest": "b" * 40, "behind": 3, "can_update": True, "reason": "",
            "checked_at": time.time(), "ahead": 0}


ck = su.Checker(fake_check, every=3600)
first = ck.report()
for _ in range(50):
    if ck._last is not None:
        break
    time.sleep(0.02)
second = ck.report()
check("the checker answers None at first and checks in the background",
      first is None and second["behind"] == 3 and len(calls) == 1 and "ahead" not in second)
ck.report()
check("...and does not fetch again within its interval", len(calls) == 1)

# ── the agent's /update ─────────────────────────────────────────────────────
TOKEN = "t" * 24
restarted = []
applied = []


def fake_apply():
    applied.append(1)
    return {"ok": True, "updated": True, "from": "a" * 40, "to": "c" * 40, "behind": 3, "reason": ""}


agent = pc_agent.Agent(TOKEN, processes=lambda: [], gpu=lambda: [1.0, 2.0, 3.0],
                       gpu_procs=lambda: [], updates=ck, apply_update=fake_apply,
                       on_updated=restarted.append)
srv = agent.serve(host="127.0.0.1", port=0)
port = srv.server_address[1]


def post(path, token=TOKEN):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=b"", method="POST",
                                 headers={pc_agent.TOKEN_HEADER: token} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


try:
    check("the report carries the checkout's update state",
          agent.snapshot()["build"]["behind"] == 3)
    code, _ = post("/update", token=None)
    check("/update without the token: 401, nothing applied", code == 401 and applied == [])
    code, body = post("/update")
    # The restart runs on a thread after the answer: wait for it, not 1.3 s.
    _deadline = time.monotonic() + 10
    while not restarted and time.monotonic() < _deadline:
        time.sleep(0.01)
    check("/update with the token: fast-forwarded, answered, then the restart",
          code == 200 and body["updated"] and applied == [1] and len(restarted) == 1)
    check("...and the cached state is dropped (the restarted agent re-checks)", ck._last is None)
    agent._update_lock.acquire()
    code, body = post("/update")
    agent._update_lock.release()
    check("one update at a time", code == 409 and "already running" in body["reason"])
finally:
    agent.stop()

old = pc_agent.Agent(TOKEN, processes=lambda: [], gpu=lambda: [1.0, 2.0, 3.0], gpu_procs=lambda: [])
check("an agent started without a restart hook cannot update (501)", old.update()[0] == 501)
check("...and reports no build", old.snapshot()["build"] is None)

# ── the NAS: bell and endpoint ──────────────────────────────────────────────
saved = (pc_host.configured, pc_host.snapshot, pc_host.available, pc_host.request_update)
build = {"head": "a" * 40, "latest": "b" * 40, "behind": 3, "can_update": True, "reason": ""}
pc_host.configured = lambda: True
pc_host.snapshot = lambda: {"build": dict(build)}
saved_prov = nt.PROVIDERS
nt.PROVIDERS = [(nt._pc_update_items, True)]
admin = type("U", (), {"is_admin": True})()
try:
    nt._pc_update_hidden["until"] = 0.0
    n = nt.system_notifications(admin)
    check("the bell: the PC is 3 commits behind, one click updates it",
          len(n["items"]) == 1 and n["items"][0]["action"]["key"] == "pc_update"
          and "3 commits behind" in n["items"][0]["text"])
    check("...answered in the bell, not counted for the Settings bubble",
          n["needs_action"] == 1 and n["settings_action"] == 0)
    pc_host.snapshot = lambda: {"build": dict(build, can_update=False,
                                              reason="tracked files have local changes")}
    item = nt._pc_update_items()[0]
    check("cannot fast-forward: the bell says why and how, no button",
          item["action"] is None and "local changes" in item["text"] and "git pull" in item["text"])
    pc_host.snapshot = lambda: {"build": dict(build, behind=0)}
    check("current: nothing in the bell", nt._pc_update_items() == [])
    pc_host.snapshot = lambda: {}
    check("an older agent (no build in its report): nothing in the bell", nt._pc_update_items() == [])

    from fastapi import HTTPException
    from src.routers import hosts

    async def fake_request(post=None):
        return 200, {"ok": True, "updated": True, "from": "a" * 40, "to": "c" * 40, "reason": ""}

    pc_host.available = lambda: True
    pc_host.request_update = fake_request
    pc_host.snapshot = lambda: {"build": dict(build)}
    res = asyncio.run(hosts.pc_update(user=admin))
    check("POST /api/hosts/pc/update asks the agent and reports the new commit",
          res["updated"] and res["to"] == "c" * 40)
    check("...and hides the bell item while the agent restarts", nt._pc_update_items() == [])
    nt._pc_update_hidden["until"] = 0.0

    async def too_old(post=None):
        return 501, {"ok": False, "reason": "this agent cannot update itself"}
    pc_host.request_update = too_old
    try:
        asyncio.run(hosts.pc_update(user=admin))
        got = None
    except HTTPException as e:
        got = e
    check("an agent too old to update itself: said so, with what to do",
          got is not None and got.status_code == 409 and "git pull" in got.detail)
    pc_host.available = lambda: False
    try:
        asyncio.run(hosts.pc_update(user=admin))
        got = None
    except HTTPException as e:
        got = e
    check("the PC unreachable: 409, the agent is not asked", got is not None and got.status_code == 409)
finally:
    pc_host.configured, pc_host.snapshot, pc_host.available, pc_host.request_update = saved
    nt.PROVIDERS = saved_prov
    nt._pc_update_hidden["until"] = 0.0

# ── wiring ──────────────────────────────────────────────────────────────────
tray = (ROOT / "src/tray_app.py").read_text(encoding="utf-8")
check("both tray menus offer Check for updates / Update now",
      tray.count("*_update_items(_restart_after_update)") == 2)
check("...both modes restart after an update, the mutex released first",
      tray.count("on_updated=_restart_after_update") == 2
      and tray.index("CloseHandle(mutex)   # BEFORE the re-exec") < tray.rindex("_reexec()"))
check("...the agent-only restart tells the NAS first (goodbye)",
      "restart.set()\n        _stop_for_good()" in tray)
notif_js = (ROOT / "frontend/js/notifications.js").read_text(encoding="utf-8")
app_js = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
check("the bell's button is a fixed handler, registered",
      "pc_update: () => act('updateNode')" in notif_js and "  updateNode,\n" in app_js)

check.done()
