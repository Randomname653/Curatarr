#!/usr/bin/env python3
"""Drift test for app_context.py: every UI label the prompt blocks reference
must exist VERBATIM in frontend/index.html — renaming a button without updating
the prompt becomes a red test instead of the curator confidently describing UI
that no longer exists. Also fails on orphaned registry entries (label listed
but no block mentions it).

    python tests/test_app_context_drift.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.services import app_context


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    html = open(os.path.join(root, "frontend", "index.html"), encoding="utf-8").read()

    # Every prompt block in the module (auto-discovered so a new *_BLOCK is
    # covered without touching this test).
    blocks = {name: val for name, val in vars(app_context).items()
              if name.endswith("_BLOCK") and isinstance(val, str)}
    blob = "\n".join(blocks.values())
    print(f"checking {len(app_context.REFERENCED_UI_LABELS)} labels against "
          f"index.html + {len(blocks)} prompt block(s)\n")

    failures = []
    for label in app_context.REFERENCED_UI_LABELS:
        # index.html may carry the label HTML-escaped ("Delete &amp; exit")
        in_html = label in html or label.replace("&", "&amp;") in html
        in_blocks = label in blob
        status = "OK  " if (in_html and in_blocks) else "FAIL"
        detail = []
        if not in_html:
            detail.append("NOT in index.html (stale prompt?)")
            failures.append(label)
        if not in_blocks:
            detail.append("in registry but no block mentions it (orphan)")
            failures.append(label)
        print(f"  {status} {label}" + (f"  — {'; '.join(detail)}" if detail else ""))

    print()
    if failures:
        print(f"DRIFT: {len(failures)} problem(s) — sync app_context.py with the UI.")
        return 1
    print("PASS — prompt blocks and frontend UI are in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
