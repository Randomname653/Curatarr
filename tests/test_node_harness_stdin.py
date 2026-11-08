"""No suite hands node a built-up script on the command line.

Windows caps a command line at 32,767 characters; a harness that inlines a
frontend module (wrapup_story.js and friends) passed ``node -e <script>``
and failed there with WinError 206 once the module grew, while Linux CI
stayed green. Scripts go on stdin (``node -`` with ``input=``) or into a
file. A short literal ``-e`` one-liner is fine.

    python tests/test_node_harness_stdin.py
"""
from _harness import check  # noqa: I001 - first: temp stores

import re
import shutil
import subprocess
from pathlib import Path

TESTS = Path(__file__).resolve().parent
ARGV_SCRIPT = re.compile(r'\[\s*node\s*,\s*"-e"\s*,\s*[A-Za-z_]\w*\s*\]')

offenders = sorted(p.name for p in TESTS.glob("test_*.py")
                   if ARGV_SCRIPT.search(p.read_text(encoding="utf-8")))
check("no suite passes a built-up script to node -e", offenders == [], offenders)

node = shutil.which("node")
if node:
    big = "const s = " + repr("x" * 40_000) + ";\nconsole.log(s.length + ' ü');\n"
    r = subprocess.run([node, "-"], input=big, capture_output=True, text=True, encoding="utf-8")
    check("a script longer than a Windows command line runs from stdin, UTF-8 intact",
          r.returncode == 0 and r.stdout.strip() == "40000 ü", (r.returncode, r.stdout, r.stderr[:300]))

check.done()
