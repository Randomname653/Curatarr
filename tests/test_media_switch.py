"""The media server switch from the web page (switch.py, 2026-10-31).

The rebind used to be a script that wanted Curatarr stopped. Now Settings ->
Media server runs it in the live process, both ways:

- the check builds the plan in the background and shows its report; a
  switch needs a check towards the same server first;
- the switch builds the plan again with the page's picks (users, libraries,
  title suggestions) and refuses while a user is still without a match;
- it refuses while an enrichment, the music pipeline or a deletion run
  holds its lock - they write server keys from lists read before;
- while it runs, the Jellyfin session poller is stopped and the Plex sync's
  lock is held; afterwards the lock is free again and, on Jellyfin, the
  poller runs and the delta poll starts over;
- the watched state goes to the new server unless the page said no;
- one at a time.

    python tests/test_media_switch.py
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_TMP = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{(_TMP / 'curatarr.db').as_posix()}"

import src.database.connection as conn_mod  # noqa: E402
from src.database.models import Base  # noqa: E402
Base.metadata.create_all(conn_mod.engine)

from src.services.app_state import get_state, set_state  # noqa: E402
from src.services.media_server import jellyfin_history as jh, rebind, switch  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


calls = {"build": [], "apply": [], "sync": [], "reset": [], "poller": []}
blocking = {"on": False}
seen_during = {}


async def fake_build(source, target, tgt, *, source_adapter=None, map_user=None, skip_users=(),
                     accept_suggestions=False, map_library=None, walks=None):
    calls["build"].append({"source": source, "target": target, "map_user": map_user, "skip_users": list(skip_users),
                           "accept_suggestions": accept_suggestions, "map_library": map_library,
                           "walked": walks is None})
    plan = rebind.Plan(source=source, target=target)
    plan.walks = walks or {"target": ([], []), "source": ([], [])}
    plan.items = {"1001": "a" * 32}
    plan.users = {1: ("12345678", "b" * 32, "name")}
    if blocking["on"]:
        plan.users_unmatched = [(2, "Sam")]
    return plan


async def fake_apply(plan, *, target_adapter, settings_writer=None, **kw):
    seen_during["plex_lock"] = await asyncio.to_thread(get_state, "plex_sync_running")
    seen_during["poller"] = jh._poller
    calls["apply"].append(plan.target)
    m = _TMP / "manifest.json"
    m.write_text('{"tables": {"watch_history.plex_item_id": {"moved": 1, "collisions": 0}}, "chroma": [], '
                 '"proposals": [], "status": "completed"}', encoding="utf-8")
    switch._state["written_type"] = plan.target          # stands in for the settings writer
    from src.config import settings
    settings.MEDIA_SERVER_TYPE = "" if plan.target == "plex" else plan.target
    return m


async def fake_sync(plan, adapter, **kw):
    calls["sync"].append(plan.target)
    return {"marked_played": 1, "failed": 0}


async def no_after():
    return None


rebind.build_plan, rebind.apply_plan, rebind.sync_watched_state = fake_build, fake_apply, fake_sync
rebind.server_adapters = lambda source, target, settings=None: (object(), object())
switch._after_switch = no_after
switch._reset_jellyfin_poll = lambda uids: calls["reset"].append(list(uids)) or 0
jh.start_session_poller = lambda: calls["poller"].append("start")


async def settle():
    for _ in range(200):
        if switch._state.get("phase") not in ("checking", "switching"):
            return
        await asyncio.sleep(0.01)


async def main():
    from src.config import settings
    settings.MEDIA_SERVER_TYPE = None

    try:
        await switch.start_switch("jellyfin", {})
        refused = False
    except RuntimeError as e:
        refused = "check" in str(e)
    check("a switch needs a check towards that server first", refused)

    await switch.start_check("jellyfin", {})
    try:
        await switch.start_check("jellyfin", {})
        twice = False
    except RuntimeError:
        twice = True
    await settle()
    st = switch.status()
    check("the check runs in the background and leaves its report", st["phase"] == "ready"
          and st["report"]["items"]["matched"] == 1 and st["active"] == "plex")
    check("...one at a time", twice)
    await switch.start_check("jellyfin", {"skip_users": ["Sam"]})
    await settle()
    check("a second check with other picks reuses the fresh library walks",
          [b["walked"] for b in calls["build"]] == [True, False])

    blocking["on"] = True
    await switch.start_switch("jellyfin", {"map_user": {}})
    await settle()
    st = switch.status()
    check("a user still without a match: nothing moves, the page shows why",
          st["phase"] == "ready" and "Sam" in (st.get("error") or "") and not calls["apply"])
    blocking["on"] = False

    await asyncio.to_thread(set_state, "enrichment_running", "1")
    await switch.start_switch("jellyfin", {"skip_users": ["Sam"]})
    await settle()
    st = switch.status()
    check("an enrichment run holding its lock refuses the switch", st["phase"] == "failed"
          and "enrichment" in st["error"] and not calls["apply"])
    await asyncio.to_thread(set_state, "enrichment_running", "0")

    await switch.start_check("jellyfin", {})
    await settle()
    await switch.start_switch("jellyfin", {"map_user": {"sam ": "Sam_J"}, "skip_users": ["carol"],
                                     "map_library": {"9": "c" * 32}, "accept_suggestions": True})
    await settle()
    st = switch.status()
    last = calls["build"][-1]
    check("the switch builds the plan again with the page's picks, on the check's walks",
          last["map_user"] == {"sam": "Sam_J"} and last["skip_users"] == ["carol"]
          and last["map_library"] == {"9": "c" * 32} and last["accept_suggestions"] is True
          and last["walked"] is False)
    check("...holds the Plex sync's lock and has no session poller while it moves the keys",
          seen_during.get("plex_lock") == "1" and seen_during.get("poller") is None)
    check("...frees the lock afterwards, starts the poller on Jellyfin and resets its delta poll",
          (await asyncio.to_thread(get_state, "plex_sync_running")) in (None, "", "0") and calls["poller"] == ["start"]
          and calls["reset"] == [[1]])
    check("...marks the watched state on the new server and reports done",
          st["phase"] == "done" and calls["sync"] == ["jellyfin"] and st["result"]["items"] == 1
          and st["active"] == "jellyfin")

    switch.forget()
    await switch.start_check("plex", {})
    await settle()
    check("after a switch the next check walks afresh (every key changed)", calls["build"][-1]["walked"] is True)
    await switch.start_switch("plex", {"sync_state": False})
    await settle()
    st = switch.status()
    check("the way back is the same switch; without 'mark as played' nothing is marked",
          st["phase"] == "done" and calls["apply"][-1] == "plex" and calls["sync"] == ["jellyfin"]
          and st["active"] == "plex")
    check("...and on Plex no poller is started again, no delta poll reset",
          calls["poller"] == ["start"] and calls["reset"] == [[1]])


asyncio.run(main())
print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
