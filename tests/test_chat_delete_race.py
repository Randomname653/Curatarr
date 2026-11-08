"""Delete & exit acts on its own discussion, not on the one open when the
arr answers.

2026-11-08: the owner pressed Delete & exit on one proposal, opened the
next proposal's discussion while the arr was still deleting, and argued
it there. The approve's answer then closed THAT thread, flushed its
memories and switched to free chat, so the next message lost the title.
The real deleteFromDiscussion runs in node against stubbed UI helpers:

- the pane moved on: the new discussion stays, the deletion is a toast,
  the deleted proposal's own thread gets its memories flushed;
- the pane still shows it: the thread closes as before;
- a failure after the pane moved on is a toast, not a bubble in the
  other title's thread.

    python tests/test_chat_delete_race.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import json
import re
import shutil
import subprocess
from pathlib import Path

CHAT = (Path(__file__).resolve().parents[1] / "frontend" / "js" / "chat.js").read_text(encoding="utf-8")


def _fn(name: str) -> str:
    m = re.search(r"^export (?:async )?function %s\(.*?^\}$" % re.escape(name), CHAT, re.M | re.S)
    if m is None:
        raise SystemExit(f"{name}() not found in chat.js")
    return m.group(0).removeprefix("export ")


STUBS = """
const state = { pendingDiscussContext: { kind: 'deletion_proposal', proposal_id: 7, title: 'Old Film' },
                _delProposalsAll: [{ id: 7 }, { id: 8 }] };
const calls = { msgs: [], toasts: [], flushed: [], shown: [], banner: [] };
let release = null, _stream = null;
const btn = { disabled: false, textContent: 'Delete & exit' };
const document = { getElementById: id => (id === 'discuss-delete-btn' ? btn : null) };
async function confirmDialog() { return { ok: true }; }
async function deleteFateLines() { return []; }
function _deleteBody() { return ''; }
function _flushThreadMemories(t) { calls.flushed.push(t); }
function addMsg(t) { calls.msgs.push(t); }
function toast(t, tone) { calls.toasts.push([t, tone]); }
function _setDiscussBanner(x) { calls.banner.push(x); }
async function _showThread(t) { calls.shown.push(t); return true; }
function api(url) {
  if (url.includes('/approve')) return new Promise(r => { release = r; });
  return Promise.resolve({});
}
"""

RUN = """
(async () => {
  const p = deleteFromDiscussion();
  while (!release) await new Promise(r => setTimeout(r, 0));
  if (SWITCH) state.pendingDiscussContext = { kind: 'deletion_proposal', proposal_id: 8, title: 'New Show' };
  release(ANSWER);
  await p;
  process.stdout.write(JSON.stringify({ ctx: state.pendingDiscussContext, calls,
                                        cache: state._delProposalsAll.map(x => x.id) }));
})();
"""


def run(switch: bool, answer: dict):
    node = shutil.which("node")
    if not node:
        return None
    js = (STUBS + _fn("_threadIdForContext") + "\n" + _fn("deleteFromDiscussion") + "\n"
          + RUN.replace("SWITCH", "true" if switch else "false").replace("ANSWER", json.dumps(answer)))
    # stdin, not -e: Windows caps a command line at 32k characters
    r = subprocess.run([node, "-"], input=js, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        raise SystemExit(r.stderr[:800])
    return json.loads(r.stdout)


moved = run(True, {"ok": True})
if moved is None:
    print("  (node not installed - skipped)")
else:
    check("the pane moved on: the new discussion stays open",
          moved["ctx"] == {"kind": "deletion_proposal", "proposal_id": 8, "title": "New Show"}
          and moved["calls"]["shown"] == [] and moved["calls"]["banner"] == [], moved)
    check("...the deletion is a toast, not a bubble in the other thread",
          moved["calls"]["msgs"] == [] and ['Deleted "Old Film".', "success"] in moved["calls"]["toasts"], moved)
    check("...and the deleted proposal's own thread gets its memories flushed",
          moved["calls"]["flushed"] == ["deletion_proposal:7"] and moved["cache"] == [8], moved)

    stayed = run(False, {"ok": True})
    check("the pane still shows it: the thread closes as before",
          stayed["ctx"] is None and stayed["calls"]["shown"] == ["general"]
          and stayed["calls"]["msgs"] == ['Deleted "Old Film". Discussion thread closed.']
          and stayed["calls"]["flushed"] == ["deletion_proposal:7"], stayed)

    failed = run(True, {"ok": False})
    check("a failure after the pane moved on is a toast in the open thread's place",
          failed["calls"]["msgs"] == [] and failed["ctx"]["proposal_id"] == 8
          and any(t.startswith('Delete failed for "Old Film"') for t, _ in failed["calls"]["toasts"]), failed)

    limbo = run(False, {"limbo": True, "error": "The arr is unreachable."})
    check("limbo while the pane still shows it: the bubble and the button come back",
          limbo["calls"]["msgs"] == ["The arr is unreachable."] and limbo["ctx"]["proposal_id"] == 7, limbo)

check.done()
