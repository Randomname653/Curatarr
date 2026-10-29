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
      "brandMark('sd-logo')" in cur and 'viewBox="0 0 64 64"' not in cur)

from src import tray_app  # noqa: E402

img = tray_app._brand_icon(64)
check("the tray icon is the mark, 64 px", img.size == (64, 64) and img.getbbox() is not None)
check("the README shows the wordmark and says the brand is not AGPL",
      "assets/brand/curatarr-logo.svg" in (readme := (ROOT / "README.md").read_text(encoding="utf-8"))
      and "not covered by the AGPL-3.0" in readme)

# ── stage 2: classic, the thinking ring, the busy pulse, the watermark ──────
brand_js = (ROOT / "frontend/js/brand.js").read_text(encoding="utf-8")
check("the classic wordmark and ring are templates, the favicon has a classic twin",
      'id="brand-logo-classic"' in html and 'id="brand-ring"' in html
      and (ROOT / "frontend/icons/favicon-classic.svg").read_text(encoding="utf-8")
      == (BRAND / "curatarr-mark-classic.svg").read_text(encoding="utf-8"))
ring = html[html.index('id="brand-ring"'):]
ring = ring[:ring.index("</template>")]
check("the ring's four segments are numbered clockwise, once each",
      all(ring.count(f"brand-ring-{i}\"") == 1 for i in range(1, 5)))
check("Settings -> This device offers Modern / Classic, saved per browser",
      'name="icon-style"' in html and 'data-on-change="setIconStyle"' in html
      and "localStorage.setItem(ICON_KEY, v)" in brand_js and "/icons/favicon-classic.svg" in brand_js)
check("the logo slots switch, and the page applies the choice at start",
      html.count("data-brand-slot") == 2
      and "applyIconStyle();" in (ROOT / "frontend/js/app.js").read_text(encoding="utf-8"))
chat_js = (ROOT / "frontend/js/chat.js").read_text(encoding="utf-8")
check("a streaming reply marks a model at work, and its end clears it (finally)",
      "modelWorkStarted();" in chat_js
      and chat_js.index("} finally {\n    modelWorkEnded();".replace("\n", "\r\n") if "\r\n" in chat_js
                        else "} finally {\n    modelWorkEnded();") > chat_js.index("modelWorkStarted();"))
check("...and the server's status says so too",
      "if (!statusR._err) setServerModelBusy(statusR.model_busy);" in (ROOT / "frontend/js/game.js").read_text(encoding="utf-8")
      and '"model_busy": model_busy' in (ROOT / "src/routers/process_monitor.py").read_text(encoding="utf-8"))
check("the pulse and the ring stand still under reduced motion",
      "body.model-busy #logo .brand-diamond" in css and "@keyframes brand-ring-step" in css
      and ".brand-ring-mark .brand-ring, .brand-ring-mark .brand-diamond { opacity: 1; }" in css)
check("the wrap-up carries the mark: the summary card and every story slide",
      "brandMarkHtml('wu-mark')" in (ROOT / "frontend/js/wrapup.js").read_text(encoding="utf-8")
      and "brandMark('wus-mark')" in (ROOT / "frontend/js/wrapup_story.js").read_text(encoding="utf-8"))

import tempfile  # noqa: E402

with tempfile.TemporaryDirectory() as tmp:
    tray_app._TRAY_PREFS = Path(tmp) / "tray.json"
    plain = tray_app._brand_icon(64).tobytes()

    class _Icon:
        icon = None
    ic = _Icon()
    tray_app._toggle_classic_icon(ic, None)
    check("the tray menu's switch saves the choice and swaps the icon",
          tray_app._classic_icon() is True and ic.icon is not None and ic.icon.tobytes() != plain)
    tray_app._toggle_classic_icon(ic, None)
    check("...and back", tray_app._classic_icon() is False and ic.icon.tobytes() == plain)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
