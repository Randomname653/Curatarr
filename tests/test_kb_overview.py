#!/usr/bin/env python3
"""Invariant test for the KB overview (the numbers drift-test): every state
count derives from one denominator and the three truth sources join cleanly.
Run against the LIVE data — a violated invariant is a red test, not a
confusing display.

    python tests/test_kb_overview.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from src.services.kb_overview import build_overview, _STATES


def main() -> int:
    # On a fresh clone (CI, a new install) no database exists yet — the app
    # creates its schema at startup, so the test does the same. Locally this
    # is an idempotent no-op and the invariants run against the LIVE data as
    # designed; on CI they run against an empty schema, where every
    # denominator is zero and the joins must still hold. This exact line was
    # missing while the local battery said 55/55 and CI said red: the test
    # silently depended on the developer's own data/ directory.
    from src.database.connection import init_db
    init_db()
    p = asyncio.run(build_overview())
    failures = []
    print(f"{'cat':7} {'total':>6} {'dl':>6} " +
          " ".join(f"{s[:9]:>9}" for s in _STATES) +
          f" {'vec':>6} {'wiki':>5} {'omdb':>5}")
    for cat, c in sorted(p["categories"].items()):
        d = c["denominator"]
        s = c["states"]
        ssum = sum(s.values())
        line_ok = True
        if ssum != d["downloaded"]:
            failures.append(f"{cat}: states sum {ssum} != downloaded {d['downloaded']}")
            line_ok = False
        if any(v < 0 for v in s.values()):
            failures.append(f"{cat}: negative state")
            line_ok = False
        if c["vectors"]["indexed"] > d["downloaded"]:
            failures.append(f"{cat}: vectors {c['vectors']['indexed']} > downloaded")
            line_ok = False
        print(f"{cat:7} {d['arr_total']:>6} {d['downloaded']:>6} " +
              " ".join(f"{s[k]:>9}" for k in _STATES) +
              f" {c['vectors']['indexed']:>6}"
              f" {c['wikipedia']['significance_cached']:>5}"
              f" {c['omdb']['covered']:>5}" + ("" if line_ok else "   <-- FAIL"))
    print("\nwatch-history-only:", p["watch_history_only"])
    print("music pipeline    :", p["music_pipeline"])
    print("storage (MB)      :", p["storage"])
    if failures:
        print("\nINVARIANT FAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nPASS — one denominator, clean joins.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
