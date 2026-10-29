"""The new logo (2026-10-29): one set of brand files, every icon rendered
from it, the UI drawing the mark from the same paths.

    python tests/test_brand.py
"""
import json
import re
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PASS = FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}")


BRAND = ROOT / "assets" / "brand"
names = ["curatarr-mark", "curatarr-mark-dark", "curatarr-mark-mono", "curatarr-mark-classic",
         "curatarr-logo", "curatarr-logo-dark", "curatarr-logo-inline", "curatarr-logo-inline-dark",
         "curatarr-app-icon", "curatarr-app-icon-classic"]
check("every brand file is there as SVG and PNG master",
      all((BRAND / f"{n}.svg").is_file() and (BRAND / f"{n}.png").is_file() for n in names))
svgs = {n: (BRAND / f"{n}.svg").read_text(encoding="utf-8") for n in names}
check("the SVGs carry no style attribute (the app's CSP refuses inline styles)",
      not any("style=" in s for s in svgs.values()))
check("the marks are square tiles", all(
    (lambda v: abs(float(v[2]) - float(v[3])) < 0.5)(re.search(r'viewBox="([^"]+)"', svgs[n]).group(1).split())
    for n in names if n.startswith("curatarr-mark")))

manifest = json.loads((ROOT / "frontend/manifest.json").read_text(encoding="utf-8"))
icons = {i["purpose"]: i["src"] for i in manifest["icons"]}
check("the manifest names an any, a maskable and a monochrome icon, and each exists",
      {"any", "maskable", "monochrome"} <= set(icons)
      and all((ROOT / "frontend" / src.lstrip("/")).is_file() for src in icons.values()))
mask = Image.open(ROOT / "frontend/icons/icon-maskable-512.png")
check("the maskable icon is full bleed (no transparent corner)", mask.convert("RGBA").getpixel((2, 2))[3] == 255)
anyi = Image.open(ROOT / "frontend/icons/icon-512.png").convert("RGBA")
check("the 'any' icon is the rounded tile (transparent corner)", anyi.getpixel((2, 2))[3] == 0)
apple = Image.open(ROOT / "frontend/icons/apple-touch-icon.png")
check("the iOS icon is opaque and 180 px (iOS rounds it itself)", apple.mode == "RGB" and apple.size == (180, 180))

html = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
check("the favicon is the brand mark's file", '<link rel="icon" type="image/svg+xml" href="/icons/favicon.svg">' in html
      and (ROOT / "frontend/icons/favicon.svg").read_text(encoding="utf-8") == svgs["curatarr-mark"])
check("sidebar and login draw the wordmark, coloured by the brand tokens",
      html.count('class="brand-logo') >= 3 and 'id="brand-mark"' in html
      and 'class="brand-diamond"' in html and "fill=" not in html.split('id="logo"')[1][:5000])
css = (ROOT / "frontend/css/app.css").read_text(encoding="utf-8")
check("...and the tokens exist", all(t in css for t in ("--brand-amber:", "--brand-diamond:", "--brand-ruby:")))
cur = (ROOT / "frontend/js/curation.js").read_text(encoding="utf-8")
check("the shutdown screen clones the mark instead of drawing the old eye",
      "getElementById('brand-mark')" in cur and 'viewBox="0 0 64 64"' not in cur)

from src import tray_app  # noqa: E402

img = tray_app._brand_icon(64)
check("the tray icon is the mark, 64 px", img.size == (64, 64) and img.getbbox() is not None)
check("the README shows the wordmark and says the brand is not AGPL",
      "assets/brand/curatarr-logo.svg" in (readme := (ROOT / "README.md").read_text(encoding="utf-8"))
      and "not covered by the AGPL-3.0" in readme)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
