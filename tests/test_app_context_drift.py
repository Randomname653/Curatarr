#!/usr/bin/env python3
"""Drift test for app_context.py: every UI label the prompt blocks reference
must exist VERBATIM in frontend/index.html — renaming a button without updating
the prompt becomes a red test instead of the curator confidently describing UI
that no longer exists. Also fails on orphaned registry entries (label listed
but no block mentions it).

    python tests/test_app_context_drift.py
"""
import _harness  # noqa: F401,I001 - first: temp stores

import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.services import app_context


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    from tests.frontend_files import everything
    html = everything()

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

    # The UI names the templates take from the dictionary (⟦key⟧): in both
    # dictionaries, and drawn by the frontend - data-i18n*="key" in the
    # markup or t('key') in a module - so the curator names what the user sees.
    import json
    import re
    i18n = os.path.join(root, "frontend", "i18n")
    dicts = {lang: json.load(open(os.path.join(i18n, f"{lang}.json"), encoding="utf-8")) for lang in ("en", "de")}
    print(f"\nchecking {len(app_context.REFERENCED_UI_KEYS)} dictionary keys")
    for key in app_context.REFERENCED_UI_KEYS:
        missing = [lang for lang, d in dicts.items() if key not in d]
        drawn = re.search(r'data-i18n(?:-[\w-]+)?="%s"|\bth?\(\s*[\'"]%s[\'"]' % (re.escape(key), re.escape(key)), html)
        status = "OK  " if not missing and drawn else "FAIL"
        detail = ([f"missing in {', '.join(missing)}.json"] if missing else []) + ([] if drawn else ["not drawn by the UI"])
        if detail:
            failures.append(key)
        print(f"  {status} {key}" + (f"  — {'; '.join(detail)}" if detail else ""))
    templates = {name: val for name, val in vars(app_context).items() if name.endswith("_TEMPLATE")}
    for lang in ("en", "de"):
        for name, val in templates.items():
            if "⟦" in app_context.render(val, lang):
                failures.append(f"{name}:{lang}")
                print(f"  FAIL {name} keeps a marker in {lang}")

    print()
    if failures:
        print(f"DRIFT: {len(failures)} problem(s) — sync app_context.py with the UI.")
        return 1
    print("PASS — prompt blocks and frontend UI are in sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
