"""The app starts also when its modules finish after the page's load event.

2026-11-13: after the deploy of the UI language the owner got the Setup
wizard instead of the app. i18n.js awaits its dictionaries at import, so
app.js (which imports it) finished evaluating after the load event; its
``window.onload = …`` was assigned too late and never ran, and the page sat
on the Setup overlay it shows before the boot code hides it. It depended on
timing: the dictionaries and the load event ended within milliseconds.

The boot now goes through ``ui.onPageLoad``, which runs at once when the
page has loaded and on the load event otherwise. The real helper runs in
node against a stubbed document and window.

    python tests/test_boot_after_load.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import json
import re
import shutil
import subprocess
from pathlib import Path

UI = (Path(__file__).resolve().parents[1] / "frontend" / "js" / "ui.js").read_text(encoding="utf-8")
m = re.search(r"^export function onPageLoad\(.*?^\}$", UI, re.M | re.S)
check("ui.js has onPageLoad", m is not None)

RIG = """
const out = {};
function run(readyState) {
  const listeners = [];
  globalThis.document = { readyState };
  globalThis.window = { addEventListener: (ev, fn, opts) => listeners.push([ev, fn, opts]) };
  let calls = 0;
  onPageLoad(() => { calls += 1; });
  const before = calls;
  for (const [ev, fn] of listeners) if (ev === 'load') fn();
  return { before, after: calls, listeners: listeners.map(([ev, , opts]) => [ev, !!(opts && opts.once)]) };
}
out.complete = run('complete');
out.loading = run('loading');
out.interactive = run('interactive');
console.log(JSON.stringify(out));
"""

node = shutil.which("node")
check("node is available", node is not None)
if m is not None and node:
    src = m.group(0).removeprefix("export ") + "\n" + RIG
    r = subprocess.run([node, "-"], input=src, capture_output=True, text=True, timeout=30)
    res = json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else {}
    check("the rig ran", r.returncode == 0, r.stderr[-800:])
    c = res.get("complete", {})
    check("a page that has loaded runs the boot at once, without waiting for an event that already fired",
          c.get("before") == 1 and c.get("listeners") == [], c)
    lo = res.get("loading", {})
    check("a page still loading runs it on the load event, once",
          lo.get("before") == 0 and lo.get("after") == 1 and lo.get("listeners") == [["load", True]], lo)
    it = res.get("interactive", {})
    check("...also while the DOM is parsed but the load event is still ahead",
          it.get("before") == 0 and it.get("after") == 1, it)

check.done()
