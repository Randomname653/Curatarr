"""Nodes have names; an Analyze waits for its node (2026-10-23).

- The second machine is a "node", named like a Tdarr node: it reports a name
  (PC_AGENT_NAME, else its computer name) and the owner can rename it. The UI
  and the log say that name, never "the gaming PC".
- "Run analysis" while the node is off, busy or playing is queued and starts
  by itself on the first poll that finds the node free; the stored proposals
  stay on screen meanwhile.
- A game started on the node DURING a deletion run stops it cleanly with the
  node's name, instead of a string of failed calls.

Fakes only: no agent, no database, no Ollama.

    python tests/test_node_names.py
"""
import asyncio
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src import pc_agent  # noqa: E402
from src.services import app_state  # noqa: E402
from src.services import deletion_queue as dq  # noqa: E402
from src.services import notifications as nt  # noqa: E402
from src.services import ollama_hosts as oh  # noqa: E402
from src.services import pc_host  # noqa: E402

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


store = {}


async def fake_aget(key):
    return store.get(key)


async def fake_aset(key, value):
    store[key] = value
    return True

saved = (app_state.aget_state, app_state.aset_state, pc_host.configured, oh.judge_ready,
         oh.split, dict(pc_host._names), dict(pc_host._state))
app_state.aget_state, app_state.aset_state = fake_aget, fake_aset


def reset_names():
    pc_host._names.update(custom="", reported="", loaded=False, dirty=False)


try:
    # ── the agent names itself ──────────────────────────────────────────────
    a = pc_agent.Agent("t" * 24, processes=lambda: [], gpu=lambda: [1.0, 2.0, 3.0],
                       gpu_procs=lambda: [], name="  Living   Room  ")
    check("the agent reports PC_AGENT_NAME", a.snapshot()["node"] == {"name": "Living   Room"})
    b = pc_agent.Agent("t" * 24, processes=lambda: [], gpu=lambda: [1.0, 2.0, 3.0], gpu_procs=lambda: [])
    check("...else its computer name", b.snapshot()["node"]["name"] == socket.gethostname()[:40])

    # ── the server's name for it ────────────────────────────────────────────
    reset_names()
    check("nothing reported yet: 'the node'",
          pc_host.node_name() == "the node" and pc_host.node_name(cap=True) == "The node")
    pc_host.record({"node": {"name": "Living   Room"}, "processes": [], "gpu": [1, 2, 3]})
    check("a report names it (cleaned: single spaces)", pc_host.node_name() == "Living Room"
          and pc_host._names["dirty"] is True)
    check("names are capped and printable",
          pc_host.clean_name("x" * 60 + "\x07") == "x" * 40 and pc_host.clean_name("   ") == "")
    name = asyncio.run(pc_host.set_custom_name("Big Rig"))
    check("the owner's name wins and is stored", name == "Big Rig" and store["node_name"] == "Big Rig"
          and pc_host.status()["name"] == "Big Rig" and pc_host.status()["name_reported"] == "Living Room")
    name = asyncio.run(pc_host.set_custom_name(""))
    check("an empty name goes back to the reported one", name == "Living Room" and store["node_name"] == "")
    reset_names()
    store.update(node_name="Big Rig", node_name_reported="Living Room")
    asyncio.run(pc_host.load_names())
    check("both names survive a restart (read once from app_state)",
          pc_host.node_name() == "Big Rig" and pc_host._names["loaded"])

    # ── what the UI says ────────────────────────────────────────────────────
    pc_host.configured = lambda: True
    saved_snapshot = pc_host.snapshot
    pc_host.snapshot = lambda: {"build": {"behind": 2, "can_update": True, "latest": "b" * 40}}
    nt._pc_update_hidden["until"] = 0.0
    item = nt._pc_update_items()[0]
    check("the bell names the node", item["title"] == "Big Rig runs an older Curatarr"
          and item["action"]["label"] == "Update Big Rig" and "this server" in item["text"])
    pc_host.snapshot = saved_snapshot
    src = "\n".join((ROOT / p).read_text(encoding="utf-8") for p in (
        "src/services/notifications.py", "src/routers/recommendations.py", "src/routers/chat.py",
        "src/routers/hosts.py", "src/services/ollama_hosts.py", "src/services/recommendations_engine.py",
        "src/services/model_autobuild.py", "frontend/js/nodes.js", "frontend/js/version.js"))
    check("no user-facing 'gaming PC' left", "gaming PC" not in src)

    # ── an Analyze that waits for the node ──────────────────────────────────
    dq._mem.update(loaded=False, req=None, task=None)
    store.pop(dq.STATE_KEY, None)
    asyncio.run(dq.queue(1, "anime", False, 7))
    req = asyncio.run(dq.pending())
    check("queued: kept, and the note says it starts by itself",
          req["category"] == "anime" and "starts by itself as soon as Big Rig is free" in dq.note(req))
    started = []

    async def fake_run(r):
        started.append(r["category"])

    saved_run = dq._run
    dq._run = fake_run
    oh.judge_ready = lambda: False
    check("the node still busy: nothing starts", asyncio.run(dq.kick()) is False and started == [])
    oh.judge_ready = lambda: True

    async def kick_and_wait():
        ok = await dq.kick()
        await asyncio.sleep(0)
        return ok
    check("the node free: the queued analysis starts, once",
          asyncio.run(kick_and_wait()) is True and started == ["anime"]
          and asyncio.run(dq.pending()) is None and store[dq.STATE_KEY] == "")
    dq._mem.update(loaded=False, req=None, task=None)
    store[dq.STATE_KEY] = '{"user_id": 1, "category": null, "at": %f}' % (time.time() - dq.MAX_AGE_S - 1)
    check("a week-old request expires unrun", asyncio.run(dq.pending()) is None)
    dq._run = saved_run

    rec = (ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8")
    check("Analyze queues instead of refusing, and shows the stored proposals meanwhile",
          "await deletion_queue.queue(user.id, category, recent_only, recent_days)" in rec
          and "refresh = False" in rec and '"queued_note"' in rec
          and rec.index("deletion_queue.queue(") < rec.index("if not refresh:"))
    host_src = (ROOT / "src/services/pc_host.py").read_text(encoding="utf-8")
    check("every poll that finds the node free kicks the queue",
          "await deletion_queue.kick()" in host_src)

    # ── a game on the node mid-run ──────────────────────────────────────────
    from src.services import recommendations_engine as eng
    oh.split = lambda: True
    oh.judge_ready = lambda: False
    pc_host._state.update(busy=True, busy_reason="Overwatch.exe running")
    ok, why = eng._judge_lane()
    check("mid-run, a busy node stops the run with its name and the reason",
          ok is False and why == "Big Rig is not free (Overwatch.exe running)")
finally:
    (app_state.aget_state, app_state.aset_state, pc_host.configured, oh.judge_ready,
     oh.split, names, st) = saved
    pc_host._names.clear()
    pc_host._names.update(names)
    pc_host._state.clear()
    pc_host._state.update(st)

# ── wiring ──────────────────────────────────────────────────────────────────
html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
app_js = (ROOT / "frontend/js/app.js").read_text(encoding="utf-8")
settings_js = (ROOT / "frontend/js/settings.js").read_text(encoding="utf-8")
dele_js = (ROOT / "frontend/js/deletions.js").read_text(encoding="utf-8")
check("Settings -> Maintenance shows the compute nodes and can rename the node",
      'id="maint-nodes"' in html and "loadNodes();" in settings_js
      and all(f"  {n},\n" in app_js for n in ("loadNodes", "saveNodeName", "updateNode")))
check("the deletions view shows the queued note", dele_js.count("_renderQueuedBanner(r.queued_note);") == 2)

# ── a run that did not judge everything replaces only what it judged ────────
from src.services import recommendations_engine as eng2  # noqa: E402
saved_runs = dict(eng2.DELETION_RUN_SUMMARY)
eng2.DELETION_RUN_SUMMARY.clear()
eng2._record_deletion_run("music", judged=40, flagged=12)
eng2._record_deletion_run("movie", stopped="waiting for Big Rig")
check("a category judged to the end may replace its proposals; a deferred one may not",
      eng2.run_judged_everything("music") and not eng2.run_judged_everything("movie")
      and not eng2.run_judged_everything("anime"))
eng2.DELETION_RUN_SUMMARY.clear()
eng2.DELETION_RUN_SUMMARY.update(saved_runs)
eng_src = (ROOT / "src/services/recommendations_engine.py").read_text(encoding="utf-8")
rec_src = (ROOT / "src/routers/recommendations.py").read_text(encoding="utf-8")
sch_src = (ROOT / "src/services/scheduler.py").read_text(encoding="utf-8")
check("a run that waits for its node records itself as stopped",
      '_record_deletion_run(category, stopped=f"waiting for {node_name()}")' in eng_src)
check("Analyze (all categories) supersedes per category, by what each run judged",
      "if run_judged_everything(_c):" in rec_src and "DeletionProposal.media_id.in_(ids)" in rec_src
      and "old_q.filter(or_(*conds) if conds else false())" in rec_src)
check("...and so does the scheduled scan", "run_judged_everything(cat)" in sch_src)

# ── music credits do not trip the outage breaker ────────────────────────────
from src.routers.enrichment import _looks_like_credit  # noqa: E402
credits = ["Armin van Buuren & Vini Vici", "50 Cent Feat. Ne-Yo", "Carbon Community vs. Burufunk",
           "Benny Benassi presents The Bizz", "21 Savage, Offset & Metro Boomin", "Coone and B-Front",
           "Adaro X Dr. Rude", "Billain / Allied", "00", ".dev", "17"]
artists = ["Atilla", "Dej Loaf", "B.I.G", "9 Inch Dix", "2Deep", "Buffalo&Wallace"]
check("credit lines and stray tags are recognised; single names are not",
      all(_looks_like_credit(n) for n in credits) and not any(_looks_like_credit(n) for n in artists))
en_src = (ROOT / "src/routers/enrichment.py").read_text(encoding="utf-8")
check("a missed credit keeps its attempt and does not count toward the abort ratio",
      'credit = pcat == "music" and _looks_like_credit(canonical)' in en_src
      and "if counted and not credit:" in en_src
      and '_rolling[pcat].append("credit" if credit else "not_found")' in en_src)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
