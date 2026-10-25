"""The README's screenshots exist and the capture script makes every one
(2026-10-25). The Preview used to point at frames nobody had captured,
behind a comment, while three pre-redesign images stood in for them.

    python tests/test_readme_screenshots.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


readme = (ROOT / "README.md").read_text(encoding="utf-8")
refs = sorted(set(re.findall(r"docs/screenshots/([\w/.-]+\.png)", readme)))
check("the README shows the new desktop and phone frames", len(refs) >= 12
      and any(r.startswith("desktop/") for r in refs) and any(r.startswith("mobile/") for r in refs))
missing = [r for r in refs if not (ROOT / "docs/screenshots" / r).exists()]
check(f"every image it shows is in the repo {missing or ''}", not missing)
check("no image of the pre-redesign UI is left",
      not any((ROOT / "docs/screenshots" / n).exists() for n in ("chat.png", "bell.png", "knowledge-base.png")))

import capture_screenshots as cs  # noqa: E402
made = {f"{cs.OUT_DIR[vp]}/{name}.png" for name, vp, *_ in cs.SHOTS}
check("the capture script makes every image the README shows", set(refs) <= made)
check("phone frames are stored at 1.5x", cs.PHONE_STORED_WIDTH == 585)

guide = (ROOT / "docs/screenshots/README.md").read_text(encoding="utf-8")
check("the guide lists every shot", all(f"`{m}`" in guide for m in made))

css = (ROOT / "frontend/css/app.css").read_text(encoding="utf-8")
check("on a phone a poster card is a grid: the actions under the title line, the text full width",
      "grid-template-columns: 72px minmax(0, 1fr)" in css
      and ".poster-card > .grow, .poster-card > .grow > .panel-item-head { display: contents; }" in css)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
